"""
Запуск исполнителя отдельным процессом и прогон агента по набору задач.

На стенде исполнитель запускается командой модуля стенда под Landlock, у
студента как файл harness.py рядом. Агент у студента вызывается прямо в
процессе; память между шагами проходит через JSON, как на стенде, поэтому
несериализуемое в ней сразу даёт ошибку, а не сюрприз при отправке.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

from .checks import evaluate
from .workspace import Task, Workspace

HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "harness.py")
MAX_MEMORY_BYTES = 64 * 1024


class SubprocessHarness:
    """harness(mode, files, args, stdin) → ответ исполнителя (dict)."""

    def __init__(self, command=None, cwd=None, env=None, timeout: float = 60.0):
        self.command = list(command or [sys.executable, "-B", HARNESS])
        self.cwd = cwd
        self.env = env
        self.timeout = timeout
        self.calls = 0

    def __call__(self, mode: str, files: dict, args: list, stdin: str | None) -> dict:
        self.calls += 1
        with tempfile.TemporaryDirectory(prefix="ws_") as tmp:
            for path, content in files.items():
                full = os.path.join(tmp, *path.split("/"))
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w", encoding="utf-8") as f:
                    f.write(content)
            try:
                proc = subprocess.run(self.command + [mode, tmp, *args], input=stdin or "",
                                      capture_output=True, text=True, timeout=self.timeout,
                                      cwd=self.cwd, env=self.env)
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": "исполнитель не уложился во время"}
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        try:
            report = json.loads(lines[-1])
        except (IndexError, json.JSONDecodeError):
            tail = proc.stderr.strip().splitlines()[-1:] if proc.stderr else []
            return {"ok": False, "error": "исполнитель не ответил" + (f": {tail[0][:200]}" if tail else "")}
        return report if isinstance(report, dict) else {"ok": False, "error": "неверный ответ исполнителя"}


def roundtrip_memory(memory) -> dict:
    """Память как на стенде: только JSON, не больше 64 КиБ."""
    text = json.dumps(memory, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(text.encode("utf-8")) > MAX_MEMORY_BYTES:
        raise ValueError(f"memory больше {MAX_MEMORY_BYTES // 1024} КиБ")
    restored = json.loads(text)
    if type(restored) is not dict:
        raise ValueError("memory должна быть словарём")
    return restored


def run_task(act, task: Task, harness, memory: dict, extra=None, verbose: bool = False) -> tuple[dict, dict]:
    """Одна задача: шаги агента до сдачи, затем оценка."""
    ws = Workspace(task, harness)
    started = time.perf_counter()
    while not ws.done:
        observation = ws.observation(extra() if callable(extra) else extra)
        action = act(observation, memory)
        memory = roundtrip_memory(memory)
        if verbose:
            shown = {k: (v if k != "content" else f"<{len(v)} символов>") for k, v in
                     (action.items() if isinstance(action, dict) else [])}
            print(f"  шаг {task.step_limit - ws.steps_left + 1}: {shown}")
        ws.apply(action if isinstance(action, dict) else {})
    result = evaluate(ws)
    result["seconds"] = round(time.perf_counter() - started, 1)
    return result, memory


def run_tasks(act, tasks: list[Task], harness=None, extra=None, verbose: bool = False) -> dict:
    harness = harness or SubprocessHarness()
    memory: dict = {}
    results = []
    for index, task in enumerate(tasks, 1):
        info = (lambda: dict(extra() if callable(extra) else (extra or {}),
                             task_index=index, tasks_total=len(tasks)))
        result, memory = run_task(act, task, harness, memory, info, verbose)
        results.append(result)
    total = round(100 * sum(r["score"] for r in results) / len(results), 2) if results else 0.0
    return {"total": total, "tasks": results}


def print_report(report: dict) -> None:
    print(f"{'задача':12} {'вид':10} {'балл':>5} {'шаги':>5}  подробности")
    for r in report["tasks"]:
        flags = ("; " + "; ".join(r["flags"])) if r["flags"] else ""
        print(f"{r['task_id']:12} {r['kind']:10} {r['score']:5.2f} {r['steps']:5d}  {r['detail']}{flags}")
    print(f"итого: {report['total']:.2f} из 100")
