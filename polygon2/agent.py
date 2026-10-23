"""
Кодовый агент для «Полигона 2».

Агент решает задачи командами среды: read, write, append, edit, run, submit.
Готово: обращение к модели (`ask`) и цикл `act`, который хранит состояние
задачи в memory и по очереди: читает нужные файлы, спрашивает модель,
применяет правку, запускает видимые тесты, решает, сдавать ли задачу.

Вам нужно дописать шесть функций с пометкой TODO:
  1. choose_reads: выбрать файлы для чтения и не открывать лишние;
  2. parse_report: разобрать вывод команды run;
  3. extract_commands: превратить ответ модели в команды среды;
  4. should_submit: решить, когда сдавать задачу;
  5. drop_failing_tests: убрать тесты, падающие на правильном модуле;
  6. build_prompt: составить запрос к модели.

На стенде модуль перезагружается на каждом шаге: всё, что нужно помнить,
храните в memory (только JSON, не больше 64 КиБ).
"""
from __future__ import annotations

import ast
import difflib
import os
import re
import sys
import time

PUBLIC_POLYGON_URL = "https://lectures.ai.dscs.pro"
MODEL = "deepseek-flash"
FILE_LIMIT = 12000
REPORT_LIMIT = 2500
DIFF_LIMIT = 2000
MAX_ATTEMPTS = 6
EXTRA_TEST_ROUNDS = 3
MAX_TESTS = 20


# ------------------------------------------------------------- готово: модель

def ask(system: str, prompt: str, max_tokens: int = 1024) -> str | None:
    """Один запрос к модели курса; None, если нет токена, исчерпан бюджет или случилась ошибка."""
    token = (os.environ.get("POLYGON_TOKEN") or os.environ.get("LLM_API_KEY") or "").strip()
    url = (os.environ.get("POLYGON_URL") or os.environ.get("LLM_BASE_URL") or PUBLIC_POLYGON_URL).rstrip("/")
    if not token:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    client = anthropic.Anthropic(api_key=token, base_url=url, timeout=90.0, max_retries=0)
    for attempt in range(2):
        try:
            response = client.messages.create(
                model=MODEL, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}],
                thinking={"type": "disabled"}, extra_body={"temperature": 0.0})
            return "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
        except anthropic.RateLimitError:
            if attempt == 0:
                time.sleep(10)
                continue
            return None
        except (anthropic.APIError, OSError, ValueError):
            return None
    return None


# ---------------------------------------------------------- 1. чтение файлов

def _imports(source: str) -> list[str]:
    """Имена модулей, которые импортирует файл; разбор молча сдаётся на ошибке."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


def _import_paths(path: str, source: str) -> list[str]:
    """Пути локальных модулей, которые может импортировать файл.

    Учитывает относительные импорты (`from .helpers import x`): без них в
    задаче из нескольких модулей помощник, где лежит ошибка, остаётся невидимым.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    here = path.rsplit("/", 1)[0] if "/" in path else ""
    found: list[str] = []

    def add(module: str, base: str = "") -> None:
        stem = module.replace(".", "/").strip("/")
        if base:
            stem = f"{base}/{stem}"
        if stem:
            found.extend((stem + ".py", stem + "/__init__.py"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = here
                for _ in range(node.level - 1):
                    base = base.rsplit("/", 1)[0] if "/" in base else ""
                if node.module:
                    add(node.module, base)
                    for alias in node.names:
                        add(f"{node.module}.{alias.name}", base)
                else:
                    for alias in node.names:
                        add(alias.name, base)
            elif node.module:
                add(node.module)
                for alias in node.names:
                    add(f"{node.module}.{alias.name}")
    return found


def choose_reads(observation: dict, state: dict) -> list[str]:
    """Пути файлов, которые нужно прочитать, в порядке чтения.

    Целевой модуль, видимые тесты, файл тестов (в задаче tests) и локальные
    модули, которые импортируют уже прочитанные файлы. Больше ничего: в каталоге
    бывают файлы, которые выдают себя за ответы или скрытые тесты, и их чтение
    обнуляет задачу.
    """
    present = {item["path"] for item in observation.get("files", [])}
    wanted: list[str] = []

    target = observation.get("target")
    if target:
        wanted.append(target)
    if observation.get("kind") == "tests":
        if observation.get("tests_path"):
            wanted.append(observation["tests_path"])
    else:
        wanted.extend(observation.get("visible_tests", []))

    # Локальные модули, которые импортируют уже прочитанные файлы: без них
    # модель не увидит контракт, на который опирается целевой модуль.
    for path, content in (state.get("files") or {}).items():
        for candidate in _import_paths(path, content):
            if candidate in present:
                wanted.append(candidate)

    result: list[str] = []
    for path in wanted:
        if path in present and path not in result:
            result.append(path)
    return result


# ------------------------------------------------------ 2. разбор вывода run

def parse_report(text: str) -> dict:
    """Из вывода run: сколько прошло, сколько всего, какие тесты упали, были ли ошибки импорта."""
    result = {"passed": 0, "ran": 0, "failing": [], "failing_full": [], "import_errors": False}
    if not isinstance(text, str) or not text.strip():
        return result

    match = re.search(r"прошло\s+(\d+)\s+из\s+(\d+)", text)
    if match:
        result["passed"], result["ran"] = int(match.group(1)), int(match.group(2))
    result["import_errors"] = "ошибка импорта" in text

    failing: list[str] = []
    failing_full: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^(?:FAIL|ERROR)\s+([A-Za-z_][\w.]*)", line.strip())
        if match:
            full = match.group(1)
            name = full.split(".")[-1]
            if name not in failing:
                failing.append(name)
            if full not in failing_full:
                failing_full.append(full)
    result["failing"] = failing
    result["failing_full"] = failing_full
    return result


# ------------------------------------------------- 3. ответ модели → команды

BLOCK = re.compile(r"(?:FILE:\s*(?P<path>[^\n]+)\n)?<{5,9} ?SEARCH\n(?P<old>.*?)\n?={5,9}\n"
                   r"(?P<new>.*?)\n?>{5,9} ?REPLACE", re.S)
CODE = re.compile(r"```(?:python|py)?\n(?P<code>.*?)```", re.S)


def _compiles(source: str) -> bool:
    try:
        compile(source, "task.py", "exec")
    except (SyntaxError, ValueError):
        return False
    return True


def _top_names(source: str) -> set[str]:
    """Имена функций и классов верхнего уровня: по ним видно, файл целиком или только дополнение."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    return {node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _test_count(source: str) -> int:
    """Сколько тестов найдёт исполнитель: функции test_* и методы test_* в классах."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return 0
    count = 0
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            count += 1
        elif isinstance(node, ast.ClassDef):
            count += sum(1 for child in node.body
                         if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                         and child.name.startswith("test"))
    return count


def _has_stub(source: str) -> bool:
    """Осталась ли в файле заглушка raise NotImplementedError."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and node.exc is not None:
            exc = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(exc, ast.Name) and exc.id == "NotImplementedError":
                return True
    return False


def _source_of(source: str, node) -> str:
    """Исходный текст узла вместе с декораторами."""
    lines = source.splitlines(keepends=True)
    start = node.lineno
    for decorator in getattr(node, "decorator_list", []):
        start = min(start, decorator.lineno)
    return "".join(lines[start - 1:node.end_lineno])


def _body_key(node) -> str:
    """Тело теста без имени: одинаковые тела не добавляем дважды."""
    return ast.dump(ast.Module(body=node.body, type_ignores=[]), include_attributes=False)


def _merge_tests(existing: str, addition: str) -> str | None:
    """Дописать новые тесты к уже прошедшим, не теряя прежние.

    Модель часто возвращает весь файл заново. Если просто записать его поверх,
    сильные тесты прошлого раунда пропадают. Здесь из ответа берутся только
    новые тесты: совпадающие имена переименовываются, а тесты с тем же телом
    пропускаются.
    """
    try:
        old_tree = ast.parse(existing)
        new_tree = ast.parse(addition)
    except (SyntaxError, ValueError):
        return None

    used = {node.name for node in old_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    known = {_body_key(node) for node in ast.walk(old_tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name.startswith("test")}
    imports = {ast.unparse(node).strip() for node in old_tree.body
               if isinstance(node, (ast.Import, ast.ImportFrom))}
    added: list[str] = []

    def unique(name: str) -> str:
        new, index = f"{name}_r2", 2
        while new in used:
            index += 1
            new = f"{name}_r{index}"
        used.add(new)
        return new

    for node in new_tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            text = ast.unparse(node).strip()
            if text not in imports:
                imports.add(text)
                added.append(text)
            continue
        if isinstance(node, ast.ClassDef):
            methods = [child for child in node.body
                       if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and child.name.startswith("test")]
            text = _source_of(addition, node).rstrip()
            dupes = sorted({method.name for method in methods
                            if _body_key(method) in known})
            if dupes:
                trimmed = drop_failing_tests(text, dupes)
                if trimmed is None:
                    continue
                text = trimmed.rstrip()
            new_methods = [method for method in methods if method.name not in dupes]
            if methods and not new_methods:
                continue
            name = node.name
            if name in used:
                name = unique(name)
                text = re.sub(rf"\b{re.escape(node.name)}\b", name, text)
            else:
                used.add(name)
            known.update(_body_key(method) for method in new_methods)
            added.append(text)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_test = node.name.startswith("test")
            if is_test and _body_key(node) in known:
                continue
            name, text = node.name, _source_of(addition, node).rstrip()
            if name in used:
                if not is_test:
                    continue
                name = unique(name)
                text = re.sub(rf"(async\s+def|def)\s+{re.escape(node.name)}\b",
                              lambda match: f"{match.group(1)} {name}", text, count=1)
            else:
                used.add(name)
            if is_test:
                known.add(_body_key(node))
            added.append(text)
            continue
        if isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names & used:
                continue
            used |= names
            added.append(_source_of(addition, node).rstrip())

    if not added:
        return existing
    return existing.rstrip() + "\n\n\n" + "\n\n".join(added) + "\n"


def _checked(commands: list[dict], state: dict) -> list[dict]:
    """Оставить команды, после которых файл остаётся синтаксически целым.

    Модель иногда обрывается на лимите токенов или присылает незакрытую скобку.
    Записывать такой файл бессмысленно: проверка всё равно его отвергнет, а
    попытка и шаги уже потрачены. Лучше вернуть замечание и спросить снова.
    """
    shadow = dict(state.get("files") or {})
    kept: list[dict] = []
    for command in commands:
        path = command.get("path")
        content = shadow.get(path, "")
        if command.get("cmd") == "edit":
            if path not in shadow:
                kept.append(command)          # файл не прочитан — проверить нечем
                continue
            count = content.count(command["old"])
            if count == 0:
                # Полная замена функции иногда ссылается на её исходную версию.
                # Переносим её только при точном совпадении старого блока с
                # сохранённым оригиналом и уникальном имени функции.
                original = (state.get("original") or {}).get(path, "")
                try:
                    old_tree = ast.parse(command["old"])
                    new_tree = ast.parse(command["new"])
                    if (original.count(command["old"]) == 1
                            and len(old_tree.body) == len(new_tree.body) == 1
                            and isinstance(old_tree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef))
                            and type(old_tree.body[0]) is type(new_tree.body[0])
                            and old_tree.body[0].name == new_tree.body[0].name):
                        matches = [node for node in ast.parse(content).body
                                   if isinstance(node, type(old_tree.body[0]))
                                   and node.name == old_tree.body[0].name]
                        if len(matches) == 1:
                            command = dict(command, old=_source_of(content, matches[0]).rstrip("\n"))
                            count = content.count(command["old"])
                except (SyntaxError, ValueError):
                    pass
            if count != 1:
                state["last_error"] = (
                    f"фрагмент SEARCH для {path} встречается в текущем файле {count} раз; "
                    "скопируй его дословно из файла выше и оставь ровно один раз"
                )
                continue
            updated = content.replace(command["old"], command["new"], 1)
        elif command.get("cmd") == "write":
            updated = command["content"]
        elif command.get("cmd") == "append":
            updated = content + command["content"]
        else:
            kept.append(command)
            continue
        if _compiles(updated):
            kept.append(command)
            shadow[path] = updated
        else:
            state["last_error"] = (
                f"код для {path} не компилируется: верни полный синтаксически "
                "верный Python, не обрывай блок"
            )
    return kept


def extract_commands(reply: str, observation: dict, state: dict) -> list[dict]:
    """Команды среды из ответа модели.

    Блоки SEARCH/REPLACE → команды edit. Иначе самый длинный блок ```python```:
    в задаче tests это файл тестов (write, а если тесты уже проходят, то append),
    в остальных это весь целевой модуль (write). Если ответ не разобран, вернуть [].
    """
    if not isinstance(reply, str) or not reply.strip():
        return []

    target = observation.get("target") or ""
    commands: list[dict] = []
    for match in BLOCK.finditer(reply):
        old = match.group("old")
        if not old:
            continue
        path = (match.group("path") or "").strip() or target
        if not path:
            continue
        commands.append({"cmd": "edit", "path": path,
                         "old": old, "new": match.group("new")})
    if commands:
        return _checked(commands, state)

    blocks = CODE.findall(reply)
    if not blocks:
        # Ответ модели мог оборваться на лимите токенов: закрывающего ``` нет.
        opened = re.search(r"```(?:python|py)?\n(?P<code>.*)\Z", reply, re.S)
        if not opened:
            return []
        blocks = [opened.group("code")]
    code = max(blocks, key=len)

    if observation.get("kind") == "tests":
        # Сохраняем завершённые тесты даже при обрыве последнего на лимите.
        if not _compiles(code):
            lines = code.splitlines(keepends=True)
            for end in range(len(lines) - 1, 0, -1):
                prefix = "".join(lines[:end])
                if _compiles(prefix) and _test_count(prefix):
                    code = prefix
                    break
        path = observation.get("tests_path") or ""
        if not path:
            return []
        if not state.get("passing"):
            return _checked([{"cmd": "write", "path": path, "content": code}], state)
        existing = (state.get("files") or {}).get(path, "")
        if not existing:
            return _checked([{"cmd": "append", "path": path, "content": code}], state)
        merged = _merge_tests(existing, code)
        if merged is None:
            state["last_error"] = (
                "прошлый файл тестов не разобрать; верни полный синтаксически верный файл"
            )
            return []
        if merged == existing:
            state["last_error"] = (
                "в ответе не было новых тестов: добавь тесты на правила, которые ещё "
                "не покрыты, не повторяя прежние"
            )
            return []
        return _checked([{"cmd": "write", "path": path, "content": merged}], state)
    if not target:
        return []
    return _checked([{"cmd": "write", "path": target, "content": code}], state)


# ----------------------------------------------------- 4. когда сдавать задачу

def should_submit(observation: dict, state: dict, report: dict) -> bool:
    """Сдавать, если видимые тесты проходят и правка применилась, или если ресурсы кончаются.

    В задаче tests проходящие тесты сдаются после нескольких раундов
    дополнительных тестов или когда тестов уже достаточно. Ресурсы: попытки
    (MAX_ATTEMPTS), обращения к модели (calls_left), шаги (оставьте запас на
    применение правки и прогон).
    """
    if observation.get("calls_left", 1) <= 0:
        return True
    if observation.get("steps_left", 10) <= 3:
        return True
    if state.get("attempts", 0) >= MAX_ATTEMPTS:
        return True
    if state.get("notes"):
        # Правка не применилась: видимые тесты могли пройти и на старом коде.
        return False
    passed = report.get("ran", 0) > 0 and report.get("passed", 0) == report.get("ran", 0)
    if not passed:
        return False
    if observation.get("kind") == "tests":
        current = (state.get("files") or {}).get(observation.get("tests_path") or "", "")
        return state.get("extra", 0) >= EXTRA_TEST_ROUNDS or _test_count(current) >= MAX_TESTS
    return True


# ------------------------------------------------ 5. убрать неверные тесты

def drop_failing_tests(source: str, names: list[str]) -> str | None:
    """Убрать из файла тестов функции и методы с именами names; None, если файл не разбирается.

    Модуль в задаче tests правильный, поэтому тест, который на нём падает,
    проверяет неверное ожидание: его надо убрать, а не чинить модуль.
    """
    if not isinstance(source, str):
        return None
    if not names:
        return source
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None

    wanted = set(names)
    bare: set[str] = set()
    exact: set[str] = set()
    for name in wanted:
        parts = name.split(".")
        if len(parts) >= 3:
            # module.Class.method: убираем только этот метод, а не одноимённые
            # методы в других классах.
            exact.add(".".join(parts[-2:]))
        else:
            bare.add(parts[-1])
    doomed: set[int] = set()

    def mark(node) -> None:
        start = node.lineno
        for decorator in node.decorator_list:
            start = min(start, decorator.lineno)
        doomed.update(range(start, node.end_lineno + 1))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in bare:
                mark(node)
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if child.name in bare or f"{node.name}.{child.name}" in exact:
                    mark(child)
    if not doomed:
        return source
    lines = source.splitlines(keepends=True)
    # После удаления последнего метода класс должен остаться валидным.
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.body:
            if all(child.lineno in doomed for child in node.body):
                first = node.body[0].lineno
                indent = lines[first - 1][:len(lines[first - 1]) - len(lines[first - 1].lstrip())]
                lines[first - 1] = indent + "pass\n"
                doomed.discard(first)
    return "".join(line for number, line in enumerate(lines, 1) if number not in doomed)


# ------------------------------------------------------- 6. запрос к модели

SYSTEM_EDIT = (
    "Ты правишь код Python в чужом проекте. Отвечай только блоками правки, без пояснений.\n"
    "Формат блока:\nFILE: путь\n<<<<<<< SEARCH\nточный фрагмент из файла\n=======\n"
    "новый фрагмент\n>>>>>>> REPLACE\n"
    "SEARCH копирует строки файла дословно и встречается в файле один раз. Не меняй тесты. "
    "Выполни все правила из условия, включая ошибки ввода и краевые случаи; не меняй поведение, "
    "о котором условие молчит."
)
SYSTEM_TESTS = (
    "Ты пишешь тесты Python (unittest или функции test_*) к правильному модулю. Каждый тест "
    "проверяет одно правило из документации и условия: значения, границы, ошибки ввода, "
    "неизменность аргументов, порядок. В каждом тесте есть assert с конкретным ожидаемым "
    "значением; заглушки и проверки вида assert callable(...) бесполезны. Тест обязан "
    "проходить на этом модуле. Не копируй код модуля. Не используй pytest, parametrize и "
    "фикстуры. Бюджет ответа 1024 токена: пиши компактные функции test_* с обычным assert, "
    "без классов и длинных имён. Ответь одним блоком ```python```: 5–8 тестов, до 45 строк. "
    "Проверяй границы до/ровно/после, разные порядки входа и сохранность аргументов."
)


def _diff(path: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(before.splitlines(), after.splitlines(),
                                 fromfile=f"{path} (до правки)", tofile=path, lineterm="")
    return "\n".join(lines)


def build_prompt(observation: dict, state: dict) -> tuple[str, str]:
    """(system, prompt): условие, прочитанные файлы, вывод тестов и ошибки прошлой попытки."""
    kind = observation.get("kind")
    system = SYSTEM_TESTS if kind == "tests" else SYSTEM_EDIT

    parts = [f"Задача {observation.get('task_id')} ({kind}): {observation.get('title', '')}",
             "Условие:\n" + observation.get("statement", "")]

    listing = [f"{item['path']} ({item['bytes']} Б)" for item in observation.get("files", [])]
    if listing:
        parts.append("Файлы рабочего каталога:\n" + "\n".join(listing))

    files = state.get("files") or {}
    order: list[str] = []
    for path in (observation.get("target"), observation.get("tests_path"),
                 *observation.get("visible_tests", [])):
        if path and path in files and path not in order:
            order.append(path)
    for path in sorted(files):
        if path not in order:
            order.append(path)
    if order:
        parts.append("Файлы проекта:")
        parts.extend(f"--- {path} ---\n{files[path]}" for path in order)

    originals = state.get("original") or {}
    diffs: list[str] = []
    for path in order:
        before, after = originals.get(path), files.get(path)
        if before is None or after is None or before == after:
            continue
        diff = _diff(path, before, after)
        if diff:
            diffs.append(diff[:DIFF_LIMIT])
    if diffs:
        parts.append("Что уже изменено относительно исходного файла:\n" + "\n\n".join(diffs))

    report = (state.get("report") or "").strip()
    if report:
        parts.append("Вывод последнего запуска видимых тестов:\n" + report)

    notes = [str(note) for note in state.get("notes") or []]
    if notes:
        parts.append("Что не получилось в прошлой попытке:\n" + "\n".join(notes))

    last_error = state.pop("last_error", None)
    if last_error:
        parts.append("Замечание к прошлому ответу:\n" + str(last_error))

    if kind == "tests":
        tests_path = observation.get("tests_path")
        if report.startswith("прошло 0 из 0") or "ошибка импорта" in report:
            parts.append(
                "Прошлый файл тестов не запустился. Верни его целиком заново, начиная с "
                "импортов: проверь по документации имя модуля и имена функций."
            )
        if state.get("passing"):
            parts.append(
                f"Тесты в {tests_path} уже проходят. Добавь новые тесты на ещё не "
                "покрытые правила из документации модуля; существующие не повторяй. "
                "Ответь одним блоком ```python``` — только новые тесты."
            )
        else:
            parts.append(
                f"Напиши тесты в {tests_path}: 5–8 коротких тестов и до 45 строк, каждый "
                "проверяет одно правило из документации. Оформляй их как unittest.TestCase "
                "с методами test_* или как функции test_* верхнего уровня. Тесты обязаны "
                "проходить на прочитанном модуле и не повторять его код. Ответь одним "
                "блоком ```python```."
            )
    elif state.get("review"):
        parts.append(
            "Видимые тесты проходят. Сверь правку с условием пункт за пунктом: все ли "
            "правила, границы, ошибки ввода и краевые случаи реализованы; проверь пустые "
            "значения, дубликаты, порядок и исключения. Сравни изменённые строки с diff "
            "выше: всё, что не требуется условием, верни к исходному виду — скрытые "
            "проверки смотрят и на прежнее поведение. Если чего-то не хватает, верни "
            "минимальную правку в формате SEARCH/REPLACE. Если всё уже верно, ответь "
            "одним словом ГОТОВО, без блока кода."
        )
    else:
        parts.append(
            f"Исправь ошибку в {observation.get('target')} блоками FILE + SEARCH/REPLACE. "
            "Причина может быть не в целевом файле, а в импортируемом им модуле — тогда "
            "правь его. SEARCH копирует строки файла дословно и встречается в нём ровно "
            "один раз. Правка минимальная: меняй только то, что требует условие, остальные "
            "функции и ветки оставь дословно — скрытые проверки смотрят и на прежнее "
            "поведение. Видимые тесты проверяют только часть требований: выполни все "
            "правила условия целиком, включая краевые случаи и ошибки ввода, и не "
            "оставляй raise NotImplementedError. Тесты не меняй."
        )
    return system, "\n\n".join(parts)


# ------------------------------------------------------------- готово: цикл

def _new_state(observation: dict) -> dict:
    return {"id": observation["task_id"], "files": {}, "original": {}, "queue": [],
            "attempts": 0, "extra": 0, "report": "", "notes": [], "stage": "read",
            "passing": False, "backup": None, "dropped": 0, "reviewed": False,
            "review": False}


def _absorb(observation: dict, state: dict) -> None:
    """Учесть результат прошлой команды."""
    last = observation["last"]
    if last["cmd"] == "read" and last.get("ok"):
        text = last["output"][:FILE_LIMIT]
        state["files"][last["path"]] = text
        original = state.setdefault("original", {})
        original.setdefault(last["path"], text)
    elif last["cmd"] in ("edit", "write", "append") and not last.get("ok"):
        state["notes"].append(f"{last['cmd']} {last.get('path')}: {last['output']}")
        state["files"].pop(last.get("path"), None)        # перечитать перед следующей попыткой
        state["queue"] = []
    elif last["cmd"] == "run":
        state["report"] = last["output"][:REPORT_LIMIT]
        state["stage"] = "judge"


def _issue(state: dict) -> dict:
    """Выдать следующую подготовленную команду и обновить свою копию файла."""
    command = state["queue"].pop(0)
    path, files = command.get("path"), state["files"]
    if command["cmd"] == "edit" and path in files:
        files[path] = files[path].replace(command["old"], command["new"], 1)
    elif command["cmd"] == "write":
        files[path] = command["content"][:FILE_LIMIT]
    elif command["cmd"] == "append":
        files[path] = (files.get(path, "") + command["content"])[:FILE_LIMIT]
    state["stage"] = "applied" if not state["queue"] else "apply"
    return command


def _submit(observation: dict, state: dict) -> dict:
    """Сдать задачу; в tests вернуть лучшую прошедшую версию тестов.

    Сгенерированный файл, который падает на правильном модуле, стоит ноль.
    Если прошедшей версии нет, исходные видимые тесты задачи заведомо проходят
    на эталоне и могут отличить хотя бы часть мутантов. Прошедшая версия копится
    слиянием раундов, поэтому сдаём текущую.
    """
    if observation.get("kind") == "tests":
        path = observation.get("tests_path") or ""
        current = (state.get("files") or {}).get(path)
        if path and current is not None and not state.get("passing"):
            original = (state.get("original") or {}).get(path)
            if original is not None and current != original:
                state["submit_next"] = True
                state["queue"] = [{"cmd": "write", "path": path, "content": original}]
                return _issue(state)
    return {"cmd": "submit"}


def _decide(observation: dict, state: dict) -> dict:
    if state.pop("submit_next", False):
        return {"cmd": "submit"}
    present = {item["path"] for item in observation["files"]}
    for path in choose_reads(observation, state):
        if path in present and path not in state["files"]:
            return {"cmd": "read", "path": path}
    if state["queue"]:
        return _issue(state)
    if state["stage"] == "applied":
        state["stage"] = "running"
        return {"cmd": "run"}
    if state["stage"] == "judge":
        report = parse_report(state["report"])
        kind = observation["kind"]
        target = observation.get("target") or ""
        ok = report["ran"] > 0 and report["passed"] == report["ran"] and not state["notes"]
        stub = (kind == "implement" and target
                and _has_stub(state["files"].get(target, "")))
        if stub:
            # Видимые тесты могли не дойти до заглушки; скрытые проверки дойдут.
            ok = False
            note = "в файле остался raise NotImplementedError — реализуй заглушку целиком"
            if note not in state["notes"]:
                state["notes"].append(note)
            state["last_error"] = (
                "в файле остался raise NotImplementedError: реализуй эту функцию "
                "целиком по условию, не оставляй заглушку"
            )
        review_now = False
        if kind == "tests":
            tests_path = observation["tests_path"]
            if ok and not state["passing"]:
                state["passing"], state["backup"] = True, state["files"].get(tests_path)
            elif ok:
                state["backup"] = state["files"].get(tests_path)
            if not ok:
                names = report.get("failing_full") or report["failing"]
                if names and tests_path in state["files"] and state["dropped"] < 4:
                    trimmed = drop_failing_tests(state["files"][tests_path], names)
                    if trimmed:
                        state["dropped"] += 1
                        state["queue"] = [{"cmd": "write", "path": tests_path, "content": trimmed}]
                        return _issue(state)
                if state.get("backup"):
                    state["extra"] = EXTRA_TEST_ROUNDS
                    state["notes"] = []
                    state["queue"] = [{"cmd": "write", "path": tests_path, "content": state.pop("backup")}]
                    return _issue(state)
        elif ok and not state.get("reviewed"):
            # Видимые тесты прошли, но скрытые строже: один проход проверки по условию.
            state["reviewed"] = True
            state["review"] = True
            state["backup"] = state["files"].get(observation.get("target"))
            review_now = True
        if not ok and not review_now and not stub and kind != "tests" \
                and state.get("reviewed") and state.get("backup") and target:
            # Проверка всё сломала: возвращаем прошедшую видимые тесты версию.
            state["notes"] = []
            state["queue"] = [{"cmd": "write", "path": target,
                               "content": state.pop("backup")}]
            state["review"] = False
            return _issue(state)
        if not review_now and should_submit(observation, state, report):
            return _submit(observation, state)
        if ok and kind == "tests":
            state["extra"] += 1
        state["stage"] = "ask"
    if state["attempts"] >= MAX_ATTEMPTS or observation.get("calls_left", 1) <= 0 \
            or observation["steps_left"] <= 3:
        return _submit(observation, state)
    state["attempts"] += 1
    system, prompt = build_prompt(observation, state)
    commands = extract_commands(ask(system, prompt), observation, state)
    state["notes"], state["report"] = [], ""
    if not commands:
        if state.get("review"):
            state["review"] = False
            return _submit(observation, state)
        state["notes"].append("модель не вернула правку, которую можно разобрать")
        return {"cmd": "run"}
    state["review"] = False
    state["queue"] = commands
    return _issue(state)


def act(observation, memory):
    state = memory.get("task")
    if not state or state.get("id") != observation["task_id"]:
        state = memory["task"] = _new_state(observation)
    _absorb(observation, state)
    try:
        return _decide(observation, state)
    except NotImplementedError as todo:
        # Пока функции не написаны, задача сдаётся как есть и получает ноль баллов.
        print(f"не готово: {todo}", file=sys.stderr)
        return {"cmd": "submit"}


if __name__ == "__main__":
    from polygon2 import self_check
    self_check(act, model="recorded")
