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
    def test_submit_restores_last_passing_tests(self):
        path = TESTS_OBS["tests_path"]
        good = "def test_good():\n    assert True\n"
        broken = good + "def test_bad():\n    assert False\n"
        task = agent._new_state(TESTS_OBS)
        task.update(files={path: broken}, original={path: "# initial\n"},
                    backup=good, passing=False)
        command = agent._submit(TESTS_OBS, task)
        self.assertEqual(command["content"], good)
        self.assertEqual(agent._decide(TESTS_OBS, task), {"cmd": "submit"})

    def test_all_failures_survive_report_truncation(self):
        text = "прошло 0 из 2\nFAIL test_first\n" + "x" * 3000 + "\nFAIL test_last\n"
        obs = dict(TESTS_OBS, last={"cmd": "run", "ok": True, "output": text})
        task = agent._new_state(obs)
        agent._absorb(obs, task)
        self.assertLessEqual(len(task["report"]), agent.REPORT_LIMIT)
        self.assertEqual(task["report_info"]["failing"], ["test_first", "test_last"])

    def test_new_tests_are_unverified_until_run(self):
        path = TESTS_OBS["tests_path"]
        task = agent._new_state(TESTS_OBS)
        task.update(files={"journal.py": "", path: "def test_a():\n    assert True\n"},
                    passing=True, stage="ask")
        with mock.patch.object(agent, "ask", return_value="```python\ndef test_b():\n    assert 2 == 2\n```" ):
            command = agent._decide(TESTS_OBS, task)
        self.assertEqual(command["cmd"], "write")
        self.assertFalse(task["passing"])

    def test_failed_review_restores_all_changed_modules(self):
        task = agent._new_state(FIX_OBS)
        task.update(files={"stock.py": "bad = 1\n", "units.py": "bad = 2\n",
                           "tests/test_stock.py": ""}, stage="judge", reviewed=True,
                    review_rounds=1, backup="good = 1\n",
                    backup_files=agent.base64.b64encode(agent.zlib.compress(agent.json.dumps(
                        {"stock.py": "good = 1\n", "units.py": "good = 2\n"}
                    ).encode())).decode(),
                    report="прошло 0 из 1\nFAIL test_regression\n")
        first = agent._decide(FIX_OBS, task)
        second = agent._decide(FIX_OBS, task)
        self.assertEqual({first["path"], second["path"]}, {"stock.py", "units.py"})
        self.assertIn("good", first["content"])
        self.assertIn("good", second["content"])

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


class GeneralizationTests(unittest.TestCase):
    def test_multimodule_task_gets_a_plan_before_editing(self):
        task = agent._new_state(FIX_OBS)
        task["files"] = {"stock.py": "from units import kg\n", "units.py": "kg = 1\n",
                         "tests/test_stock.py": ""}
        reply = "FILE: units.py\n<<<<<<< SEARCH\nkg = 1\n=======\nkg = 2\n>>>>>>> REPLACE"
        with mock.patch.object(agent, "ask", side_effect=["Причина в units.py; сохранить API.", reply]) as ask:
            command = agent._decide(FIX_OBS, task)
        self.assertEqual(ask.call_count, 2)
        self.assertIn("Причина в units.py", ask.call_args_list[1].args[1])
        self.assertEqual(command["path"], "units.py")
        self.assertEqual(task["attempts"], 2)

    def test_multiple_edits_keep_the_helpers_file_path(self):
        reply = ("FILE: units.py\n<<<<<<< SEARCH\na = 1\n=======\na = 3\n>>>>>>> REPLACE\n"
                 "<<<<<<< SEARCH\nb = 2\n=======\nb = 4\n>>>>>>> REPLACE")
        commands = agent.extract_commands(reply, FIX_OBS,
                                         state(files={"units.py": "a = 1\nb = 2\n"}))
        self.assertEqual([command["path"] for command in commands], ["units.py", "units.py"])

    def test_function_reply_preserves_the_rest_of_the_module(self):
        original = "OFFSET = 7\ndef f(x):\n    return x - OFFSET\ndef keep():\n    return OFFSET\n"
        reply = "```python\ndef f(x):\n    return x + OFFSET\n```"
        commands = agent.extract_commands(reply, FIX_OBS, state(files={"stock.py": original}))
        namespace = {}
        exec(commands[0]["content"], namespace)
        self.assertEqual(namespace["f"](2), 9)
        self.assertEqual(namespace["keep"](), 7)

    def test_partial_module_can_add_imports_after_future(self):
        original = '"""doc"""\nfrom __future__ import annotations\ndef f(x):\n    return x\ndef keep():\n    return 9\n'
        proposed = "from decimal import Decimal\ndef f(x):\n    return Decimal(x)\n"
        merged = agent._preserve_module(original, proposed)
        namespace = {}
        exec(merged, namespace)
        self.assertEqual(str(namespace["f"]("1.25")), "1.25")
        self.assertEqual(namespace["keep"](), 9)

    def test_protected_tests_cannot_be_changed_by_reply(self):
        reply = "FILE: tests/../tests/test_stock.py\n<<<<<<< SEARCH\nassert False\n=======\nassert True\n>>>>>>> REPLACE"
        commands = agent.extract_commands(reply, FIX_OBS, state())
        self.assertEqual(commands, [])

    def test_tests_task_cannot_change_the_target(self):
        reply = "FILE: journal.py\n<<<<<<< SEARCH\nraise ValueError()\n=======\npass\n>>>>>>> REPLACE"
        self.assertEqual(agent.extract_commands(reply, TESTS_OBS, state()), [])

    def test_failed_method_does_not_delete_other_class_method(self):
        source = "class A:\n    def test_same(self):\n        assert False\nclass B:\n    def test_same(self):\n        assert True\n"
        result = agent.drop_failing_tests(source, ["A.test_same"])
        namespace = {}
        exec(result, namespace)
        self.assertFalse(hasattr(namespace["A"], "test_same"))
        self.assertTrue(hasattr(namespace["B"], "test_same"))

    def test_conflicting_helper_is_not_silently_reused(self):
        old = "def make():\n    return 1\ndef test_old():\n    assert make() == 1\n"
        new = "def make():\n    return 2\ndef test_new():\n    assert make() == 2\n"
        self.assertIsNone(agent._merge_tests(old, new))
        addition = new.replace("make", "r2_make")
        merged = agent._merge_tests(old, addition)
        namespace = {}
        exec(merged, namespace)
        namespace["test_old"]()
        namespace["test_new"]()

    def test_conflicting_constant_does_not_break_old_tests(self):
        old = "VALUE = 1\ndef test_a():\n    assert VALUE == 1\n"
        new = "VALUE = 2\ndef test_b():\n    assert VALUE == 2\n"
        self.assertIsNone(agent._merge_tests(old, new))

    def test_rejected_addition_can_be_retried_without_losing_backup(self):
        path = TESTS_OBS["tests_path"]
        original = "def make():\n    return 1\ndef test_old():\n    assert make() == 1\n"
        conflict = "```python\ndef make():\n    return 2\ndef test_new():\n    assert make() == 2\n```"
        task = agent._new_state(TESTS_OBS)
        task.update(files={"journal.py": "", path: original}, backup=original,
                    passing=True, stage="ask")
        with mock.patch.object(agent, "ask", return_value=conflict):
            self.assertEqual(agent._decide(TESTS_OBS, task)["cmd"], "run")
        obs = dict(TESTS_OBS, last={"cmd": "run", "ok": True, "output": "прошло 1 из 1"})
        agent._absorb(obs, task)
        with mock.patch.object(agent, "ask", return_value=conflict.replace("make", "r2_make")) as ask:
            command = agent._decide(obs, task)
        ask.assert_called_once()
        self.assertEqual(command["cmd"], "write")
        self.assertIn("r2_make", command["content"])
        self.assertEqual(task["backup"], original)

    def test_package_initializers_are_read(self):
        obs = dict(FIX_OBS, target="pkg/api.py", visible_tests=[], files=[
            {"path": path, "bytes": 10} for path in
            ("pkg/api.py", "pkg/nested/core.py", "pkg/__init__.py", "pkg/nested/__init__.py")])
        reads = agent.choose_reads(obs, state(files={"pkg/api.py": "from pkg.nested.core import f\n"}))
        self.assertIn("pkg/__init__.py", reads)
        self.assertIn("pkg/nested/__init__.py", reads)
        self.assertIn("pkg/nested/core.py", reads)

    def test_timeout_failure_is_identified(self):
        report = agent.parse_report("прошло 0 из 1\nTIMEOUT T.test_large\n")
        self.assertEqual(report["failing_full"], ["T.test_large"])

    def test_memory_can_reload_large_source_without_truncation(self):
        import json
        memory = {}
        source = "# длинный исходник\n" * 1400
        obs = dict(FIX_OBS, last={"cmd": "read", "ok": True, "path": "stock.py", "output": source})
        command = agent.act(obs, memory)
        self.assertEqual(command["path"], "tests/test_stock.py")
        self.assertLess(len(json.dumps(memory, ensure_ascii=False).encode()), 65536)
        self.assertIn("packed", memory["task"])
        memory = json.loads(json.dumps(memory))
        obs = dict(FIX_OBS, last={"cmd": "read", "ok": True,
                                 "path": "tests/test_stock.py", "output": ""})
        with mock.patch.object(agent, "ask", return_value=None):
            agent.act(obs, memory)
        restored = agent.json.loads(agent.zlib.decompress(agent.base64.b64decode(memory["task"]["packed"])))
        self.assertEqual(restored["files"]["stock.py"], source)

    def test_incomplete_read_allows_edit_but_forbids_overwrite(self):
        task = state(files={"stock.py": "value = 1\ndef unfinished(\n"}, truncated=["stock.py"])
        edit = agent._checked([{"cmd": "edit", "path": "stock.py", "old": "value = 1", "new": "value = 2"}], task)
        self.assertEqual(len(edit), 1)
        write = agent._checked([{"cmd": "write", "path": "stock.py", "content": "value = 2\n"}], task)
        self.assertEqual(write, [])


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
