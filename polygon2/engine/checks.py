"""
Оценка сданной задачи. Ожидаемые значения есть только здесь, у вызывающей
стороны; исполнитель возвращает канонические результаты, сравниваем мы.

В задачах fix и implement скрытые проверки делятся на FAIL_TO_PASS (до правки падали)
и PASS_TO_PASS (проходили и должны остаться). Задача решена, если прошли все.

В задачах tests тесты агента запускаются на эталоне и на скрытых мутантах. Если тесты
падают на эталоне, балл ноль: проверка с t < 1 отвергает правильный код. Иначе
балл равен доле обнаруженных мутантов без эквивалентных. Эталон и мутанты перед прогоном
переписываются так, чтобы поведение не изменилось, а байт-код и исходник
отличались от показанных агенту: тест «исходник совпадает с тем, что я видел»
падает на эталоне.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import math
import random
import tokenize

from .workspace import Task, Workspace

FLOAT_TOLERANCE = 1e-9
COPY_THRESHOLD = 0.5
NGRAM = 8


# ------------------------------------------------------------------ сравнение

def same(expected, actual) -> bool:
    """Канонические значения равны; числа с плавающей точкой сравниваются с допуском."""
    if type(expected) is not list or type(actual) is not list or not expected or not actual:
        return expected == actual
    if expected[0] != actual[0] or len(expected) != len(actual):
        return False
    tag = expected[0]
    if tag == "float" and isinstance(expected[1], float) and isinstance(actual[1], float):
        return math.isclose(expected[1], actual[1], rel_tol=FLOAT_TOLERANCE, abs_tol=FLOAT_TOLERANCE)
    if tag in ("list", "tuple", "set"):
        return len(expected[1]) == len(actual[1]) and all(
            same(e, a) for e, a in zip(expected[1], actual[1]))
    if tag == "dict":
        return len(expected[1]) == len(actual[1]) and all(
            same(ek, ak) and same(ev, av) for (ek, ev), (ak, av) in zip(expected[1], actual[1]))
    return expected == actual


def results_match(expected: list, actual: list) -> bool:
    return len(expected) == len(actual) and all(same(e, a) for e, a in zip(expected, actual))


# ------------------------------------------------------- переписывание эталона

class _Noop(ast.NodeTransformer):
    """Вставить в начало каждой функции присваивание неиспользуемой переменной."""

    def __init__(self, rng: random.Random):
        self.rng = rng

    def _insert(self, node):
        self.generic_visit(node)
        stmt = ast.Assign(targets=[ast.Name(id=f"_poly{self.rng.randrange(10 ** 6)}", ctx=ast.Store())],
                          value=ast.Constant(self.rng.randrange(10 ** 6, 10 ** 7)))
        body = node.body
        at = 1 if body and isinstance(body[0], ast.Expr) and isinstance(
            getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str) else 0
        node.body = body[:at] + [stmt] + body[at:]
        return node

    visit_FunctionDef = _insert
    visit_AsyncFunctionDef = _insert


def disguise(source: str, seed: str) -> str:
    """Тот же модуль с другим исходником и байт-кодом. Детерминирован по seed."""
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))
    tree = _Noop(rng).visit(ast.parse(source))
    return ast.unparse(ast.fix_missing_locations(tree)) + "\n"


# --------------------------------------------- копия реализации вместо тестов

def _grams(source: str) -> set[tuple]:
    try:
        tokens = [t.string for t in tokenize.generate_tokens(io.StringIO(source).readline)
                  if t.type in (tokenize.NAME, tokenize.OP, tokenize.NUMBER)]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return set()
    return {tuple(tokens[i:i + NGRAM]) for i in range(max(0, len(tokens) - NGRAM + 1))}


def copied_share(tests_source: str, implementation: str) -> float:
    """Какая доля кусков реализации дословно повторена в тестах."""
    reference = _grams(implementation)
    if not reference:
        return 0.0
    return len(reference & _grams(tests_source)) / len(reference)


# ------------------------------------------------------------------- оценка

def _passed_all(report: dict) -> bool:
    if not report.get("ok"):
        return False
    tests = report["tests"]
    return tests["ran"] > 0 and tests["passed"] == tests["ran"] and not tests["import_errors"]


def evaluate(ws: Workspace) -> dict:
    """Итог одной задачи: балл от 0 до 1, подробности и отметки."""
    task = ws.task
    result = {"task_id": task.id, "kind": task.kind, "score": 0.0, "flags": list(ws.flags),
              "steps": task.step_limit - ws.steps_left, "runs": ws.runs, "detail": ""}
    if task.kind == "tests":
        outcome = evaluate_tests(task, ws.files, ws.harness)
    else:
        outcome = evaluate_checks(task, ws.files, ws.harness)
    result["flags"] += outcome.pop("extra_flags", [])
    result.update(outcome)
    if any(flag.startswith("обход проверки") for flag in result["flags"]):
        result["score"] = 0.0
    elif any(flag.startswith("правка видимых тестов") for flag in result["flags"]):
        result["score"] = round(result["score"] * 0.5, 4)
    return result


def evaluate_checks(task: Task, files: dict, harness) -> dict:
    files = dict(files)
    for path in task.protected:                 # видимые тесты остаются исходными
        if path in task.files:
            files[path] = task.files[path]
    inputs = [{"id": c["id"], "steps": c["steps"]} for c in task.checks]
    report = harness("checks", files, [], json.dumps(inputs, ensure_ascii=False))
    actual = {c["id"]: c["results"] for c in report.get("checks", [])} if report.get("ok") else {}
    groups = {"fail_to_pass": [0, 0], "pass_to_pass": [0, 0]}
    for check in task.checks:
        group = groups.setdefault(check.get("group", "fail_to_pass"), [0, 0])
        group[1] += 1
        if check["id"] in actual and results_match(check["expected"], actual[check["id"]]):
            group[0] += 1
    solved = all(done == total for done, total in groups.values())
    detail = " · ".join(f"{name.upper()} {done}/{total}" for name, (done, total) in groups.items() if total)
    if not report.get("ok"):
        detail = f"проверки не выполнились: {report.get('error', '')[:200]}"
    return {"score": 1.0 if solved and task.checks else 0.0, "solved": solved, "detail": detail,
            "groups": groups}


def evaluate_tests(task: Task, files: dict, harness) -> dict:
    tests_source = files.get(task.tests_path, "")
    out = {"score": 0.0, "solved": False, "killed": 0, "mutants": 0}
    if not tests_source.strip():
        return dict(out, detail="тесты не написаны")
    share = copied_share(tests_source, task.reference.get(task.target, ""))
    if share > COPY_THRESHOLD:
        return dict(out, detail=f"тесты повторяют реализацию ({share:.0%})",
                    extra_flags=["обход проверки: тесты копируют реализацию"])
    support = {p: c for p, c in task.reference.items() if p != task.target}
    base = dict(support)
    base[task.tests_path] = tests_source
    seed = f"{task.id}:{task.target}"
    base[task.target] = disguise(task.reference[task.target], seed)
    reference_run = harness("tests", base, [task.tests_path], None)
    if not _passed_all(reference_run):
        tests = reference_run.get("tests", {})
        return dict(out, detail=f"тесты падают на эталоне (прошло {tests.get('passed', 0)} "
                                f"из {tests.get('ran', 0)}) — балл 0")
    counted = [m for m in task.mutants if not m["equivalent"]]
    killed = []
    for mutant in counted:
        files_m = dict(base)
        files_m[task.target] = disguise(mutant["source"], seed)
        if not _passed_all(harness("tests", files_m, [task.tests_path], None)):
            killed.append(mutant["id"])
    score = len(killed) / len(counted) if counted else 0.0
    return dict(out, score=round(score, 4), solved=score == 1.0, killed=len(killed),
                mutants=len(counted), detail=f"убито мутантов {len(killed)} из {len(counted)}")
