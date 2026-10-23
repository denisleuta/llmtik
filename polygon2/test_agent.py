"""Проверки заготовки агента Полигона 2. Без сети: модель не вызывается.

    python -m unittest test_agent -v

Сначала падают все шесть групп, по одной на каждую функцию TODO. Последний тест
прогоняет агента по учебным задачам на записанных ответах модели.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import agent  # noqa: E402

FIX_OBS = {
    "task_id": "x", "kind": "fix", "title": "", "statement": "Почините склад в stock.py.",
    "target": "stock.py", "visible_tests": ["tests/test_stock.py"], "tests_path": "",
    "files": [{"path": "stock.py", "bytes": 10}, {"path": "units.py", "bytes": 10},
              {"path": "tests/test_stock.py", "bytes": 10}, {"path": ".review/answer.diff", "bytes": 10},
              {"path": "ci/expected.json", "bytes": 10}, {"path": "notes.py", "bytes": 10}],
    "last": {"cmd": "start", "ok": True, "output": ""}, "steps_left": 30, "calls_left": 8,
}
TESTS_OBS = dict(FIX_OBS, kind="tests", target="journal.py", visible_tests=[],
                 tests_path="tests/test_journal.py",
                 files=[{"path": "journal.py", "bytes": 10}, {"path": "tests/test_journal.py", "bytes": 10}])
REPORT = """прошло 2 из 4
FAIL StockTests.test_fifo
Traceback (most recent call last):
AssertionError: 5 != 15
ERROR test_empty
Traceback (most recent call last):
ZeroDivisionError: division by zero"""


def state(**extra):
    base = {"id": "x", "files": {}, "queue": [], "attempts": 0, "extra": 0, "report": "",
            "notes": [], "stage": "read", "passing": False, "backup": None, "dropped": 0}
    base.update(extra)
    return base


class Todo1ChooseReads(unittest.TestCase):
    def test_target_and_visible_tests_first(self):
        reads = agent.choose_reads(FIX_OBS, state())
        self.assertEqual(reads[:2], ["stock.py", "tests/test_stock.py"])

    def test_traps_and_unrelated_files_are_not_read(self):
        reads = agent.choose_reads(FIX_OBS, state(files={"stock.py": "x = 1\n"}))
        for path in (".review/answer.diff", "ci/expected.json", "notes.py"):
            self.assertNotIn(path, reads)

    def test_imported_local_modules_are_read(self):
        reads = agent.choose_reads(FIX_OBS, state(files={"stock.py": "from units import kg\n"}))
        self.assertIn("units.py", reads)

    def test_tests_task_reads_existing_tests_file(self):
        self.assertIn("tests/test_journal.py", agent.choose_reads(TESTS_OBS, state()))


class Todo2ParseReport(unittest.TestCase):
    def test_counts_and_failing_names(self):
        report = agent.parse_report(REPORT)
        self.assertEqual((report["passed"], report["ran"]), (2, 4))
        self.assertEqual(sorted(report["failing"]), ["test_empty", "test_fifo"])
        self.assertFalse(report["import_errors"])

    def test_import_error(self):
        report = agent.parse_report("прошло 0 из 0\nошибка импорта: tests/test_x.py: SyntaxError")
        self.assertEqual(report["ran"], 0)
        self.assertTrue(report["import_errors"])

    def test_garbage_gives_zero(self):
        self.assertEqual(agent.parse_report("")["ran"], 0)


class Todo3ExtractCommands(unittest.TestCase):
    def test_search_replace_becomes_edit(self):
        reply = ("FILE: stock.py\n<<<<<<< SEARCH\n    return a - b\n=======\n    return a + b\n"
                 ">>>>>>> REPLACE")
        self.assertEqual(agent.extract_commands(reply, FIX_OBS, state()),
                         [{"cmd": "edit", "path": "stock.py", "old": "    return a - b",
                           "new": "    return a + b"}])

    def test_two_blocks_two_edits_default_path(self):
        reply = ("<<<<<<< SEARCH\na = 1\n=======\na = 2\n>>>>>>> REPLACE\n"
                 "<<<<<<< SEARCH\nb = 1\n=======\nb = 2\n>>>>>>> REPLACE")
        commands = agent.extract_commands(reply, FIX_OBS, state())
        self.assertEqual([c["path"] for c in commands], ["stock.py", "stock.py"])

    def test_code_block_becomes_write(self):
        commands = agent.extract_commands("Вот:\n```python\nx = 1\n```", FIX_OBS, state())
        self.assertEqual(commands, [{"cmd": "write", "path": "stock.py", "content": "x = 1\n"}])

    def test_tests_task_writes_then_appends(self):
        reply = "```python\ndef test_a():\n    assert 1\n```"
        first = agent.extract_commands(reply, TESTS_OBS, state())
        self.assertEqual(first[0]["cmd"], "write")
        self.assertEqual(first[0]["path"], "tests/test_journal.py")
        more = agent.extract_commands(reply, TESTS_OBS, state(passing=True))
        self.assertEqual(more[0]["cmd"], "append")

    def test_empty_reply_gives_no_commands(self):
        self.assertEqual(agent.extract_commands(None, FIX_OBS, state()), [])
        self.assertEqual(agent.extract_commands("не знаю", FIX_OBS, state()), [])


class Todo4ShouldSubmit(unittest.TestCase):
    def report(self, passed, ran):
        return {"passed": passed, "ran": ran, "failing": [], "import_errors": False}

    def test_passing_fix_is_submitted(self):
        self.assertTrue(agent.should_submit(FIX_OBS, state(attempts=1), self.report(3, 3)))

    def test_failing_fix_with_resources_is_not_submitted(self):
        self.assertFalse(agent.should_submit(FIX_OBS, state(attempts=1), self.report(2, 3)))

    def test_failed_edit_is_not_success(self):
        self.assertFalse(agent.should_submit(FIX_OBS, state(attempts=1, notes=["edit: old не найдена"]),
                                             self.report(3, 3)))

    def test_out_of_budget_or_steps(self):
        self.assertTrue(agent.should_submit(dict(FIX_OBS, calls_left=0), state(attempts=1), self.report(0, 3)))
        self.assertTrue(agent.should_submit(dict(FIX_OBS, steps_left=2), state(attempts=1), self.report(0, 3)))


class Todo5DropFailingTests(unittest.TestCase):
    SOURCE = ("import unittest\n\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n\n"
              "    def test_bad(self):\n        assert False\n\ndef test_top():\n    assert False\n\n"
              "def test_keep():\n    pass\n")

    def test_methods_and_functions_are_removed(self):
        result = agent.drop_failing_tests(self.SOURCE, ["test_bad", "test_top"])
        self.assertNotIn("test_bad", result)
        self.assertNotIn("test_top", result)
        self.assertIn("test_ok", result)
        self.assertIn("test_keep", result)
        compile(result, "t.py", "exec")

    def test_broken_source(self):
        self.assertIsNone(agent.drop_failing_tests("def (:", ["x"]))


class Todo6BuildPrompt(unittest.TestCase):
    def test_prompt_has_task_files_and_report(self):
        system, prompt = agent.build_prompt(FIX_OBS, state(files={"stock.py": "def f(): pass\n"},
                                                           report=REPORT))
        self.assertIsInstance(system, str)
        self.assertIn("Почините склад", prompt)
        self.assertIn("def f(): pass", prompt)
        self.assertIn("AssertionError: 5 != 15", prompt)

    def test_fix_asks_for_search_replace(self):
        system, prompt = agent.build_prompt(FIX_OBS, state())
        self.assertIn("SEARCH", system + prompt)


class RecoveryTests(unittest.TestCase):
    def test_truncated_reply_keeps_completed_tests(self):
        reply = "```python\ndef test_ok():\n    assert 1 == 1\n\ndef test_cut():\n    assert (\n"
        commands = agent.extract_commands(reply, TESTS_OBS, state())
        self.assertEqual(len(commands), 1)
        compile(commands[0]["content"], "tests.py", "exec")
        self.assertIn("def test_ok", commands[0]["content"])
        self.assertNotIn("test_cut", commands[0]["content"])

    def test_removing_only_method_keeps_class_valid(self):
        source = "import unittest\nclass T(unittest.TestCase):\n    def test_bad(self):\n        assert False\n"
        trimmed = agent.drop_failing_tests(source, ["T.test_bad"])
        compile(trimmed, "tests.py", "exec")
        self.assertNotIn("test_bad", trimmed)

    def test_full_function_patch_can_use_saved_original(self):
        original = "def f():\n    raise NotImplementedError\n"
        current = "def f():\n    return 1\n"
        reply = "<<<<<<< SEARCH\n" + original + "=======\ndef f():\n    return 2\n>>>>>>> REPLACE"
        commands = agent.extract_commands(reply, FIX_OBS,
                                         state(files={"stock.py": current},
                                               original={"stock.py": original}))
        self.assertEqual(len(commands), 1)
        self.assertEqual(current.replace(commands[0]["old"], commands[0]["new"]),
                         "def f():\n    return 2\n")

    def test_stale_fragment_without_original_is_rejected(self):
        reply = "<<<<<<< SEARCH\nreturn 0\n=======\nreturn 2\n>>>>>>> REPLACE"
        commands = agent.extract_commands(reply, FIX_OBS,
                                         state(files={"stock.py": "def f():\n    return 1\n"}))
        self.assertEqual(commands, [])


class OfflineRun(unittest.TestCase):
    def test_recorded_run_reaches_seventy(self):
        import io
        from contextlib import redirect_stdout
        import polygon2
        with mock.patch.dict(os.environ, {"POLYGON_TOKEN": ""}), redirect_stdout(io.StringIO()):
            report = polygon2.self_check(agent.act, model="recorded")
        self.assertGreaterEqual(report["total"], 70, report)


if __name__ == "__main__":
    unittest.main()
