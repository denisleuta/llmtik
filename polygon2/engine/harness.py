"""
Исполнитель кода из рабочего каталога агента: видимые тесты и скрытые проверки.

Запускается отдельным процессом на каждый прогон. Ничего не знает о том, чей
код перед ним: эталон, мутант или правка агента. Все прогоны выглядят
одинаково, каталог каждый раз новый. Ожидаемые значения сюда не передаются:
режим checks только вызывает функции и возвращает результаты в каноническом
виде, сравнение делает вызывающая сторона. Поэтому объект, у которого `__eq__`
всегда даёт истину, чтение кадров стека или `sys.exit(0)` баллов не приносят.

Два режима:
  tests  WORKDIR FILE...  прогнать тесты (unittest и функции test_*),
                            вернуть число прошедших и хвосты ошибок;
  checks WORKDIR          со стандартного ввода прочитать проверки (JSON),
                            выполнить шаги и вернуть канонические результаты.

Ответ печатается одной строкой JSON в исходный стандартный вывод. Вывод самого кода
уходит в поток ошибок. Только стандартная библиотека.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import decimal
import importlib
import json
import math
import os
import resource
import signal
import sys
import traceback
import types
import unittest

MAX_TRACE = 1500
CHECK_SECONDS = 3
MAX_DEPTH = 30
MAX_ITEMS = 2000


class _Timeout(BaseException):
    """Отдельный класс, чтобы код агента не перехватил его как Exception."""


def _alarm(signum, frame):
    raise _Timeout()


def _limits(cpu_seconds: int) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1 << 20, 1 << 20))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (1 << 30, 1 << 30))
    except (ValueError, OSError):
        pass          # на macOS предел адресного пространства не ставится


def _confine(workdir: str) -> None:
    """Landlock на сервере; у студента локально, если ядро его поддерживает."""
    mode = os.environ.get("POLYGON_SANDBOX_LANDLOCK", "")
    if mode == "off":
        return
    try:
        from app.landlock import confine
    except ImportError:
        try:
            from landlock import confine          # копия в наборе студента
        except ImportError:
            if mode == "required":
                raise
            return
    confine(read_only=list(dict.fromkeys([sys.base_prefix, sys.prefix, "/usr", "/lib", "/lib64",
                                          "/etc", "/proc/self"])),
            read_write=[workdir], devices_rw=["/dev/null"], devices_ro=["/dev/urandom"],
            connect_ports=[], required=(mode == "required"))


def canon(value, depth: int = 0):
    """Результат в виде, который нельзя подделать переопределением методов.

    Типы проверяются точным `type(...) is`, а не isinstance: подкласс int со
    своим `__eq__` превращается в ["object", имя] и ни с чем не совпадёт.
    """
    if depth > MAX_DEPTH:
        return ["deep"]
    kind = type(value)
    if value is None:
        return ["none"]
    if kind is bool:
        return ["bool", value]
    if kind is int:
        return ["int", value] if abs(value) < 10 ** 30 else ["bigint", str(value)]
    if kind is float:
        return ["float", value] if math.isfinite(value) else ["float", repr(value)]
    if kind is str:
        return ["str", value[:20000]]
    if kind is decimal.Decimal:
        return ["decimal", str(value)]
    if kind is _dt.date:
        return ["date", value.isoformat()]
    if kind is _dt.datetime:
        return ["datetime", value.isoformat()]
    if kind in (list, tuple):
        items = [canon(item, depth + 1) for item in value[:MAX_ITEMS]]
        return ["list" if kind is list else "tuple", items]
    if kind is dict:
        pairs = [[canon(k, depth + 1), canon(v, depth + 1)] for k, v in dict.items(value)][:MAX_ITEMS]
        pairs.sort(key=lambda pair: json.dumps(pair, sort_keys=True, default=str))
        return ["dict", pairs]
    if kind in (set, frozenset):
        items = [canon(item, depth + 1) for item in value][:MAX_ITEMS]
        items.sort(key=lambda item: json.dumps(item, sort_keys=True, default=str))
        return ["set", items]
    return ["object", getattr(kind, "__name__", "?")]


def exception_name(exc: BaseException) -> str:
    """Полное имя класса исключения: свой класс с именем ValueError не подменит встроенный."""
    kind = type(exc)
    return f"{kind.__module__}.{kind.__qualname__}"


def _literal(value, saved):
    """Аргументы проверок: JSON плюс ссылки и несколько неJSON-типов."""
    if isinstance(value, list):
        return [_literal(v, saved) for v in value]
    if isinstance(value, dict):
        if len(value) == 1:
            (key, inner), = value.items()
            if key == "$ref":
                return saved[inner]
            if key == "$tuple":
                return tuple(_literal(v, saved) for v in inner)
            if key == "$set":
                return set(_literal(v, saved) for v in inner)
            if key == "$decimal":
                return decimal.Decimal(inner)
            if key == "$date":
                return _dt.date.fromisoformat(inner)
        return {k: _literal(v, saved) for k, v in value.items()}
    return value


def _resolve(dotted: str, saved):
    head, _, rest = dotted.partition(".")
    if head in saved:
        target = saved[head]
    else:
        target = importlib.import_module(head)
    for part in rest.split(".") if rest else []:
        target = getattr(target, part)
    return target


def _step(step: dict, saved: dict):
    if "show" in step:
        return canon(saved[step["show"]])
    if "get" in step:
        return canon(_resolve(step["get"], saved))
    name = step.get("call") or step.get("new")
    fn = _resolve(name, saved)
    args = _literal(step.get("args", []), saved)
    kwargs = _literal(step.get("kwargs", {}), saved)
    result = fn(*args, **kwargs)
    if "save" in step:
        saved[step["save"]] = result
    return None if step.get("new") or step.get("quiet") else canon(result)


def run_checks(checks: list) -> list:
    """Каждая проверка состоит из шагов; после исключения проверка прерывается."""
    out = []
    signal.signal(signal.SIGALRM, _alarm)
    for check in checks:
        saved, results = {}, []
        for step in check["steps"]:
            signal.alarm(CHECK_SECONDS)
            try:
                if "let" in step:
                    saved[step["let"]] = _literal(step["value"], saved)
                    results.append(None)
                    continue
                results.append(_step(step, saved))
            except _Timeout:
                results.append(["timeout"])
                break
            except BaseException as exc:          # noqa: BLE001 (сюда же попадает SystemExit)
                results.append(["raise", exception_name(exc)])
                break
            finally:
                signal.alarm(0)
        out.append({"id": check["id"], "results": results})
    return out


def _collect(module) -> list[tuple[str, object]]:
    tests = []
    loader = unittest.TestLoader()
    for name in sorted(vars(module)):
        item = getattr(module, name)
        if isinstance(item, type) and issubclass(item, unittest.TestCase) \
                and item.__module__ == module.__name__:
            for case in loader.loadTestsFromTestCase(item):
                tests.append((case.id().split(".", 1)[-1], case))
        elif name.startswith("test") and isinstance(item, types.FunctionType) \
                and item.__module__ == module.__name__:
            tests.append((name, item))
    return tests


def run_tests(files: list[str]) -> dict:
    report = {"ran": 0, "passed": 0, "failures": [], "import_errors": []}
    signal.signal(signal.SIGALRM, _alarm)
    for path in files:
        module_name = os.path.splitext(path)[0].replace(os.sep, ".").replace("/", ".")
        try:
            signal.alarm(CHECK_SECONDS * 3)
            module = importlib.import_module(module_name)
        except BaseException as exc:              # noqa: BLE001
            report["import_errors"].append(f"{path}: {type(exc).__name__}: {exc}"[:MAX_TRACE])
            continue
        finally:
            signal.alarm(0)
        for name, test in _collect(module):
            report["ran"] += 1
            signal.alarm(CHECK_SECONDS)
            try:
                if isinstance(test, unittest.TestCase):
                    result = unittest.TestResult()
                    test.run(result)
                    problems = result.errors + result.failures
                    if result.skipped:
                        problems = problems or [(test, "тест пропущен (skip) — не считается прошедшим")]
                    if problems:
                        kind = "error" if result.errors else "fail"
                        report["failures"].append({"test": name, "kind": kind,
                                                   "trace": str(problems[0][1])[-MAX_TRACE:]})
                        continue
                else:
                    test()
                report["passed"] += 1
            except _Timeout:
                report["failures"].append({"test": name, "kind": "timeout", "trace": "тест не уложился во время"})
            except BaseException as exc:          # noqa: BLE001
                kind = "fail" if isinstance(exc, AssertionError) else "error"
                report["failures"].append({"test": name, "kind": kind,
                                           "trace": traceback.format_exc()[-MAX_TRACE:]})
            finally:
                signal.alarm(0)
    return report


def main(argv: list[str]) -> int:
    mode, workdir = argv[1], os.path.abspath(argv[2])
    # Ответ уходит в копию исходного вывода; сам дескриптор 1 смотрит в /dev/null,
    # чтобы печать кода агента не смешивалась с ответом.
    real_stdout = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    _limits(60)
    payload = sys.stdin.read() if mode == "checks" else ""
    _confine(workdir)
    sys.path[:] = [workdir] + [p for p in sys.path if p.startswith((sys.base_prefix, sys.prefix))
                               and p not in ("", os.getcwd())]
    os.chdir(workdir)
    sys.dont_write_bytecode = True
    with contextlib.redirect_stdout(sys.stderr):
        try:
            if mode == "tests":
                result = {"ok": True, "tests": run_tests(argv[3:])}
            elif mode == "checks":
                result = {"ok": True, "checks": run_checks(json.loads(payload))}
            else:
                result = {"ok": False, "error": f"неизвестный режим {mode}"}
        except BaseException as exc:              # noqa: BLE001
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:MAX_TRACE]}
    os.write(real_stdout, (json.dumps(result, ensure_ascii=False, default=str) + "\n").encode())
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
