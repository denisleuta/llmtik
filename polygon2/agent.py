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
import os
import re
import sys
import time

PUBLIC_POLYGON_URL = "https://lectures.ai.dscs.pro"
MODEL = "deepseek-flash"
FILE_LIMIT = 12000
REPORT_LIMIT = 2500
MAX_ATTEMPTS = 4


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
    for content in (state.get("files") or {}).values():
        for name in _imports(content):
            base = name.replace(".", "/")
            for candidate in (base + ".py", base + "/__init__.py"):
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
    result = {"passed": 0, "ran": 0, "failing": [], "import_errors": False}
    if not isinstance(text, str) or not text.strip():
        return result

    match = re.search(r"прошло\s+(\d+)\s+из\s+(\d+)", text)
    if match:
        result["passed"], result["ran"] = int(match.group(1)), int(match.group(2))
    result["import_errors"] = "ошибка импорта" in text

    failing: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^(?:FAIL|ERROR)\s+([A-Za-z_][\w.]*)", line.strip())
        if match:
            name = match.group(1).split(".")[-1]
            if name not in failing:
                failing.append(name)
    result["failing"] = failing
    return result


# ------------------------------------------------- 3. ответ модели → команды

BLOCK = re.compile(r"(?:FILE:\s*(?P<path>[^\n]+)\n)?<{5,9} ?SEARCH\n(?P<old>.*?)\n?={5,9}\n"
                   r"(?P<new>.*?)\n?>{5,9} ?REPLACE", re.S)
CODE = re.compile(r"```(?:python|py)?\n(?P<code>.*?)```", re.S)


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
        return commands

    blocks = CODE.findall(reply)
    if not blocks:
        # Ответ модели мог оборваться на лимите токенов: закрывающего ``` нет.
        opened = re.search(r"```(?:python|py)?\n(?P<code>.*)\Z", reply, re.S)
        if not opened:
            return []
        blocks = [opened.group("code")]
    code = max(blocks, key=len)

    if observation.get("kind") == "tests":
        path = observation.get("tests_path") or ""
        if not path:
            return []
        command = "append" if state.get("passing") else "write"
        return [{"cmd": command, "path": path, "content": code}]
    if not target:
        return []
    return [{"cmd": "write", "path": target, "content": code}]


# ----------------------------------------------------- 4. когда сдавать задачу

def should_submit(observation: dict, state: dict, report: dict) -> bool:
    """Сдавать, если видимые тесты проходят и правка применилась, или если ресурсы кончаются.

    В задаче tests проходящие тесты сдаются после одного раунда дополнительных
    тестов. Ресурсы: попытки (MAX_ATTEMPTS), обращения к модели (calls_left),
    шаги (оставьте запас на применение правки и прогон).
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
        return state.get("extra", 0) >= 1
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
    doomed: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            start = node.lineno
            for decorator in node.decorator_list:
                start = min(start, decorator.lineno)
            doomed.update(range(start, node.end_lineno + 1))
    if not doomed:
        return source
    lines = source.splitlines(keepends=True)
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
    "неизменность аргументов, порядок. Тест обязан проходить на этом модуле. Не копируй код "
    "модуля. Ответь одним блоком ```python```: не больше 12 тестов и 70 строк."
)


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

    report = (state.get("report") or "").strip()
    if report:
        parts.append("Вывод последнего запуска видимых тестов:\n" + report)

    notes = [str(note) for note in state.get("notes") or []]
    if notes:
        parts.append("Что не получилось в прошлой попытке:\n" + "\n".join(notes))

    if kind == "tests":
        tests_path = observation.get("tests_path")
        if state.get("passing"):
            parts.append(
                f"Тесты в {tests_path} уже проходят. Добавь новые тесты на ещё не "
                "покрытые правила из документации модуля; существующие не повторяй. "
                "Ответь одним блоком ```python``` — только новые тесты."
            )
        else:
            parts.append(
                f"Напиши тесты в {tests_path}: не больше 12 тестов и 70 строк, каждый "
                "проверяет одно правило из документации. Тесты обязаны проходить на "
                "прочитанном модуле и не повторять его код. Ответь одним блоком ```python```."
            )
    else:
        parts.append(
            f"Исправь {observation.get('target')} блоками FILE + SEARCH/REPLACE. "
            "SEARCH копирует строки файла дословно и встречается в нём ровно один раз. "
            "Выполни все правила условия; тесты не меняй."
        )
    return system, "\n\n".join(parts)


# ------------------------------------------------------------- готово: цикл

def _new_state(observation: dict) -> dict:
    return {"id": observation["task_id"], "files": {}, "queue": [], "attempts": 0, "extra": 0,
            "report": "", "notes": [], "stage": "read", "passing": False, "backup": None, "dropped": 0}


def _absorb(observation: dict, state: dict) -> None:
    """Учесть результат прошлой команды."""
    last = observation["last"]
    if last["cmd"] == "read" and last.get("ok"):
        state["files"][last["path"]] = last["output"][:FILE_LIMIT]
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


def _decide(observation: dict, state: dict) -> dict:
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
        ok = report["ran"] > 0 and report["passed"] == report["ran"] and not state["notes"]
        if observation["kind"] == "tests":
            tests_path = observation["tests_path"]
            if ok and not state["passing"]:
                state["passing"], state["backup"] = True, state["files"].get(tests_path)
            elif ok:
                state["backup"] = state["files"].get(tests_path)
            elif report["failing"] and tests_path in state["files"] and state["dropped"] < 2:
                trimmed = drop_failing_tests(state["files"][tests_path], report["failing"])
                if trimmed:
                    state["dropped"] += 1
                    state["queue"] = [{"cmd": "write", "path": tests_path, "content": trimmed}]
                    return _issue(state)
            elif state.get("backup"):
                state["extra"] = 1
                state["queue"] = [{"cmd": "write", "path": tests_path, "content": state.pop("backup")}]
                return _issue(state)
        if should_submit(observation, state, report):
            return {"cmd": "submit"}
        if ok and observation["kind"] == "tests":
            state["extra"] += 1
        state["stage"] = "ask"
    if state["attempts"] >= MAX_ATTEMPTS or observation.get("calls_left", 1) <= 0 \
            or observation["steps_left"] <= 3:
        return {"cmd": "submit"}
    state["attempts"] += 1
    system, prompt = build_prompt(observation, state)
    commands = extract_commands(ask(system, prompt), observation, state)
    state["notes"], state["report"] = [], ""
    if not commands:
        state["notes"].append("модель не вернула правку, которую можно разобрать")
        return {"cmd": "run"}
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
