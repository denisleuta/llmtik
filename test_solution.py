"""Автономные проверки c4 v3.

python -m unittest test_solution.ReadyCodeTests -v
python -m unittest test_solution.ExerciseTests -v

Для проверки другого файла:
SOLUTION_PATH=/absolute/path/solution.py python -m unittest test_solution -v
"""
from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

PATH = Path(os.environ.get("SOLUTION_PATH", Path(__file__).with_name("solution.py")))
spec = importlib.util.spec_from_file_location("c4_v2_solution", PATH)
solution = importlib.util.module_from_spec(spec)
spec.loader.exec_module(solution)

NEWS = "[рынок] Объявление рост_спроса, версия 1: с дня 11 спрос вырастет в 4 раза на 4 дня."
CREATE = {
    "event_id": "рост_спроса",
    "revision": 1,
    "operation": "create",
    "issued_day": 8,
    "fields": {
        "direction": "up",
        "start": 11,
        "length": 4,
        "factor": 4.0,
    },
}
RAW = json.dumps(CREATE, ensure_ascii=False)


class FakeAPIError(Exception):
    pass


class FakeRateLimitError(FakeAPIError):
    def __init__(self, headers):
        super().__init__("слишком много запросов")
        self.response = SimpleNamespace(headers=headers)


@contextmanager
def sdk_response(blocks=None, error=None):
    """Подменить официальный SDK без сетевых обращений."""
    create = Mock()
    if error is not None:
        create.side_effect = error
    else:
        if blocks is None:
            blocks = [SimpleNamespace(type="text", text=RAW)]
        create.return_value = SimpleNamespace(content=blocks)
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    constructor = Mock(return_value=client)
    module = SimpleNamespace(
        Anthropic=constructor,
        APIError=FakeAPIError,
        RateLimitError=FakeRateLimitError,
    )
    with patch.dict(sys.modules, {"anthropic": module}):
        yield constructor, create


def observation(day=8, texts=None, **changes):
    result = {
        "day": day,
        "cash": 50_000.0,
        "raw": 120,
        "finished": 100,
        "incoming": [],
        "capacity": 220,
        "production_cost": 90.0,
        "last_sales": 100,
        "last_lost_sales": 0,
        "last_revenue": 0.0,
        "texts": [] if texts is None else texts,
    }
    result.update(changes)
    return result


def event_patch(event_id, revision, operation, issued_day, fields):
    return {
        "event_id": event_id,
        "revision": revision,
        "operation": operation,
        "issued_day": issued_day,
        "fields": fields,
    }


class OfflineTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        local_env = patch.object(solution, "_load_local_env", create=True)
        local_env.start()
        self.addCleanup(local_env.stop)
        network = patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("тест попытался обратиться к сети"),
        )
        network.start()
        self.addCleanup(network.stop)


class ReadyCodeTests(OfflineTests):
    """Эти проверки проходят до заполнения TODO."""

    def test_action_contract_without_key(self):
        memory = {}
        result = solution.act(observation(texts=[NEWS]), memory)
        self.assertEqual(set(result), {"order_raw", "produce", "price"})
        self.assertIs(type(result["order_raw"]), int)
        self.assertIs(type(result["produce"]), int)
        self.assertGreaterEqual(result["order_raw"], 0)
        self.assertGreaterEqual(result["produce"], 0)
        self.assertTrue(40 <= result["price"] <= 500)
        self.assertEqual(memory["patch_journal"], [])
        json.dumps(memory, ensure_ascii=False)

    def test_no_text_keeps_numeric_decision(self):
        state = observation()
        expected_memory = {"unit": 45.0, "prev_cash": state["cash"], "last_order": 0}
        expected = solution.decide(state, expected_memory, [])
        memory = {}
        self.assertEqual(solution.act(state, memory), expected)
        self.assertEqual(memory["prev_cash"], state["cash"])
        self.assertEqual(memory["last_order"], expected["order_raw"])
        self.assertEqual(memory["ema"], expected_memory["ema"])

    def test_unit_accounting_remains_ready(self):
        memory = {}
        first = solution.act(observation(), memory)
        paid = first["order_raw"] * 35.0
        solution.act(observation(day=9, cash=50_000.0 - paid), memory)
        self.assertAlmostEqual(memory["unit"], 40.0)

    def test_no_key_does_not_request_model(self):
        with patch.object(
            solution,
            "ask_model",
            side_effect=AssertionError("вызов модели без ключа"),
        ):
            memory = {}
            solution.act(observation(texts=[NEWS]), memory)
        self.assertEqual(memory["patch_journal"], [])
        self.assertTrue(memory["errors"])

    def test_transport_without_key(self):
        self.assertIsNone(solution.ask_model("запрос"))

    def test_transport_uses_messages_api(self):
        cases = (
            (
                {
                    "POLYGON_TOKEN": "new-test-key",
                    "POLYGON_URL": "https://example.invalid/",
                    "LLM_BASE_URL": "https://wrong.invalid",
                    "LLM_API_KEY": "wrong-test-key",
                },
                "https://example.invalid",
                "new-test-key",
            ),
            (
                {
                    "LLM_BASE_URL": "https://server-proxy.invalid/",
                    "LLM_API_KEY": "server-test-token",
                },
                "https://server-proxy.invalid",
                "server-test-token",
            ),
        )
        blocks = [
            SimpleNamespace(type="thinking", thinking="не выдаётся клиенту"),
            SimpleNamespace(type="text", text=RAW),
        ]
        for env, expected_url, expected_token in cases:
            with self.subTest(expected_url=expected_url):
                with patch.dict(os.environ, env, clear=True):
                    with sdk_response(blocks=blocks) as (constructor, create):
                        self.assertEqual(solution.ask_model("учебный запрос"), RAW)
                constructor.assert_called_once_with(
                    api_key=expected_token,
                    base_url=expected_url,
                    timeout=60.0,
                    max_retries=0,
                )
                create.assert_called_once_with(
                    model="deepseek-flash",
                    max_tokens=300,
                    messages=[{"role": "user", "content": "учебный запрос"}],
                    thinking={"type": "disabled"},
                    extra_body={"temperature": 0.0},
                )

    def test_transport_failures_do_not_leak_token(self):
        with patch.dict(os.environ, {"POLYGON_TOKEN": "new-test-key"}, clear=True):
            with sdk_response(error=FakeAPIError("нет связи")):
                self.assertIsNone(solution.ask_model("запрос"))

        limited = FakeRateLimitError({"retry-after": "12"})
        stderr, stdout = io.StringIO(), io.StringIO()
        with patch.dict(
            os.environ,
            {
                "POLYGON_TOKEN": "new-test-key",
                "POLYGON_URL": "https://example.invalid",
            },
            clear=True,
        ):
            with sdk_response(error=limited):
                with redirect_stderr(stderr), redirect_stdout(stdout):
                    with self.assertRaises(solution.PolygonRateLimitError):
                        solution.ask_model("запрос")
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("HTTP 429", stderr.getvalue())
        self.assertIn("Retry-After: 12", stderr.getvalue())
        self.assertNotIn("new-test-key", stderr.getvalue())

    def test_transport_rejects_missing_text_and_large_body(self):
        only_thinking = [SimpleNamespace(type="thinking", thinking="скрыто")]
        with patch.dict(os.environ, {"POLYGON_TOKEN": "new-test-key"}, clear=True):
            with sdk_response(blocks=only_thinking):
                self.assertIsNone(solution.ask_model("запрос"))

        stderr = io.StringIO()
        with patch.dict(os.environ, {"POLYGON_TOKEN": "new-test-key"}, clear=True):
            with sdk_response() as (constructor, _):
                with redirect_stderr(stderr):
                    self.assertIsNone(solution.ask_model("x" * 70_000))
                constructor.assert_not_called()
        self.assertIn("64 КиБ", stderr.getvalue())

    def test_expected_interval(self):
        event = {"start": 11, "length": 4, "factor": 4.0}
        for day, expected in ((10, 100), (11, 400), (14, 400), (15, 100)):
            with self.subTest(day=day):
                self.assertEqual(
                    solution.expected({"ema": 100}, [event], day, 0), expected
                )

    def test_public_file_does_not_import_private_reference(self):
        source = PATH.read_text(encoding="utf-8")
        self.assertNotIn("проверка/c4_v2", source)
        self.assertNotIn("from reference", source)
        self.assertNotIn("import reference", source)


class ExerciseTests(OfflineTests):
    """Эти проверки проходят после заполнения шести функций."""

    def test_prompt_contains_contract_context_and_message(self):
        context = {
            "day": 7,
            "events": [{"event_id": "ярмарка", "fields": {"start": 10}}],
            "recent_messages": [],
        }
        text = "[рынок] Версия 2: перенести ярмарку на два дня позже."
        prompt = solution.build_prompt(text, context)
        self.assertIsInstance(prompt, str)
        self.assertIn(text, prompt)
        self.assertIn("ярмарка", prompt)
        for name in (
            "event_id",
            "revision",
            "operation",
            "issued_day",
            "fields",
            "direction",
            "start",
            "length",
            "factor",
        ):
            self.assertIn(name, prompt)
        self.assertIn("абсолют", prompt.lower())
        self.assertIn("factor больше 1", prompt.lower())
        self.assertIn("null", prompt.lower())
        self.assertNotIn("ASSUMED_", prompt)

    def test_parse_whole_json_only(self):
        self.assertEqual(solution.parse_response("  " + RAW + "\n"), CREATE)
        for raw in (
            "",
            None,
            "{сломано",
            "Ответ: " + RAW,
            "```json\n" + RAW + "\n```",
            RAW + RAW,
        ):
            with self.subTest(raw=raw):
                self.assertIsNone(solution.parse_response(raw))
        self.assertIsNone(solution.parse_response("null"))

    def test_validate_valid_patch_forms(self):
        cases = (
            event_patch("событие", 1, "create", 0, {}),
            event_patch("событие", 1, "create", 0, {"start": 0}),
            event_patch("событие", 2, "update", 1, {"length": 1}),
            event_patch("событие", 2, "update", 1, {"factor": 2}),
            event_patch("событие", 2, "cancel", 1, {}),
        )
        for item in cases:
            with self.subTest(item=item):
                actual = solution.validate_patch(item)
                self.assertIsInstance(actual, dict)
                self.assertEqual(set(actual), solution.PATCH_KEYS)
        self.assertEqual(
            solution.validate_patch(cases[3])["fields"]["factor"], 2.0
        )

    def test_validate_rejects_wrong_shape_and_revisions(self):
        valid = event_patch("событие", 1, "create", 0, {})
        cases = [
            None,
            [],
            {key: value for key, value in valid.items() if key != "fields"},
            {**valid, "extra": 1},
            {**valid, "event_id": ""},
            {**valid, "revision": True},
            {**valid, "revision": 0},
            {**valid, "revision": 2},
            event_patch("событие", 1, "update", 0, {}),
            event_patch("событие", 1, "cancel", 0, {}),
            event_patch("событие", 2, "cancel", 0, {"start": 3}),
            event_patch("событие", 2, "change", 0, {}),
            event_patch("событие", 2, "update", True, {}),
            event_patch("событие", 2, "update", -1, {}),
            event_patch("событие", 2, "update", 0, {"unknown": 1}),
        ]
        for item in cases:
            with self.subTest(item=item):
                self.assertIsNone(solution.validate_patch(item))

    def test_validate_rejects_bad_field_values(self):
        bad_fields = (
            {"direction": "UP"},
            {"direction": None},
            {"start": True},
            {"start": -1},
            {"start": 2.0},
            {"length": False},
            {"length": 0},
            {"length": 2.0},
            {"factor": True},
            {"factor": 0},
            {"factor": -0.1},
            {"factor": "2"},
            {"factor": math.nan},
            {"factor": math.inf},
            {"direction": "up", "factor": 0.5},
            {"direction": "down", "factor": 2.0},
            {"direction": "up", "factor": 1.0},
        )
        for fields in bad_fields:
            with self.subTest(fields=fields):
                patch_data = event_patch("событие", 2, "update", 0, fields)
                self.assertIsNone(solution.validate_patch(patch_data))

    def test_validate_does_not_change_input(self):
        data = event_patch(
            "событие", 1, "create", 0,
            {"direction": "up", "start": 2, "length": 3, "factor": 2},
        )
        before = copy.deepcopy(data)
        solution.validate_patch(data)
        self.assertEqual(data, before)

    def test_out_of_order_replay_and_future_event(self):
        memory = {"current_day": 8}
        create = event_patch("ярмарка", 1, "create", 4, {"direction": "up"})
        update_2 = event_patch(
            "ярмарка", 2, "update", 5, {"start": 10, "factor": 2.5}
        )
        update_3 = event_patch("ярмарка", 3, "update", 6, {"length": 3})

        self.assertTrue(solution.apply_patch(update_3, memory))
        self.assertTrue(solution.apply_patch(update_2, memory))
        self.assertEqual(solution.active_events(memory, 8), [])
        self.assertTrue(solution.apply_patch(create, memory))

        self.assertEqual(
            [item["revision"] for item in memory["patch_journal"]], [1, 2, 3]
        )
        self.assertEqual(
            solution.active_events(memory, 8),
            [{
                "event_id": "ярмарка",
                "start": 10,
                "length": 3,
                "factor": 2.5,
                "direction": "up",
            }],
        )
        self.assertEqual(solution.active_events(memory, 13), [])

    def test_direction_is_optional_for_activation(self):
        memory = {}
        create = event_patch(
            "без_направления",
            1,
            "create",
            0,
            {"start": 4, "length": 2, "factor": 1.7},
        )
        self.assertTrue(solution.apply_patch(create, memory))
        self.assertEqual(
            solution.active_events(memory, 0),
            [{
                "event_id": "без_направления",
                "start": 4,
                "length": 2,
                "factor": 1.7,
            }],
        )

    def test_cross_revision_direction_must_match_factor(self):
        memory = {}
        self.assertTrue(solution.apply_patch(event_patch(
            "смена", 1, "create", 0,
            {"direction": "up", "start": 4, "length": 3},
        ), memory))
        self.assertTrue(solution.apply_patch(event_patch(
            "смена", 2, "update", 1, {"factor": 0.4},
        ), memory))
        self.assertEqual(solution.active_events(memory, 1), [])
        self.assertTrue(solution.apply_patch(event_patch(
            "смена", 3, "update", 2, {"factor": 2.0},
        ), memory))
        self.assertEqual(len(solution.active_events(memory, 1)), 1)

    def test_duplicate_is_idempotent_and_conflict_is_rejected(self):
        memory = {"current_day": 3}
        first = event_patch("выставка", 1, "create", 2, {"start": 7})
        self.assertTrue(solution.apply_patch(first, memory))
        self.assertTrue(solution.apply_patch(copy.deepcopy(first), memory))
        self.assertEqual(len(memory["patch_journal"]), 1)
        self.assertFalse(solution.apply_patch(
            event_patch("выставка", 1, "create", 2, {"start": 8}), memory
        ))
        self.assertEqual(len(memory["patch_journal"]), 1)
        self.assertIn("конфликт", memory["errors"][-1]["reason"])

    def test_cancel_is_terminal_and_late_patches_do_not_revive(self):
        memory = {}
        create = event_patch(
            "акция", 1, "create", 1,
            {"start": 5, "length": 5, "factor": 3.0, "direction": "up"},
        )
        cancel = event_patch("акция", 3, "cancel", 3, {})
        older_update = event_patch("акция", 2, "update", 2, {"start": 6})
        later_update = event_patch("акция", 4, "update", 4, {"start": 7})
        for item in (cancel, later_update, create, older_update):
            self.assertTrue(solution.apply_patch(item, memory))
        self.assertEqual(solution.active_events(memory, 4), [])
        self.assertTrue(memory["event_state"]["акция"]["cancelled"])
        self.assertEqual(memory["event_state"]["акция"]["last_revision"], 3)
        self.assertEqual(memory["event_state"]["акция"]["fields"]["start"], 6)

    def test_partial_event_and_json_round_trip(self):
        memory = {}
        solution.apply_patch(
            event_patch("поставка", 1, "create", 0, {"start": 3}), memory
        )
        self.assertEqual(solution.active_events(memory, 0), [])
        restored = json.loads(json.dumps(memory, ensure_ascii=False))
        solution.apply_patch(
            event_patch(
                "поставка", 2, "update", 1,
                {"length": 4, "factor": 0.3, "direction": "down"},
            ),
            restored,
        )
        self.assertEqual(len(solution.active_events(restored, 0)), 1)
        json.dumps(restored, ensure_ascii=False, allow_nan=False)

    def test_context_is_bounded_and_contains_canonical_dates(self):
        memory = {"current_day": 5, "news_history": []}
        for number in range(10):
            event_id = f"событие_{number}"
            solution.apply_patch(event_patch(
                event_id,
                1,
                "create",
                number,
                {"start": 10 + number, "length": 2, "factor": 1.5},
            ), memory)
            memory["news_history"].append({
                "day": number,
                "text": "[рынок] " + ("сообщение " * 100),
                "patch": event_patch(event_id, 1, "create", number, {}),
            })
        context = solution.build_context(memory, "[рынок] перенос на два дня")
        self.assertLessEqual(len(context["events"]), solution.MAX_CONTEXT_EVENTS)
        self.assertLessEqual(
            len(context["recent_messages"]), solution.MAX_CONTEXT_MESSAGES
        )
        encoded = json.dumps(context, ensure_ascii=False)
        self.assertIn("событие_9", encoded)
        self.assertIn('"start": 19', encoded)
        self.assertLess(len(encoded), 8_000)

    def test_ambiguous_reply_is_not_cached(self):
        valid = json.dumps(event_patch(
            "ясное", 1, "create", 5,
            {"start": 8, "length": 2, "factor": 2.0},
        ), ensure_ascii=False)
        memory = {"current_day": 5}
        with patch.dict(os.environ, {"POLYGON_TOKEN": "test-key"}, clear=True):
            with patch.object(solution, "ask_model", side_effect=["null", valid]) as model:
                solution.read_news(NEWS, 5, memory)
                self.assertEqual(memory["responses"], {})
                solution.read_news(NEWS, 5, memory)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(len(memory["patch_journal"]), 1)

    def test_response_key_changes_with_memory_summary(self):
        memory = {"current_day": 8}
        with patch.dict(os.environ, {"POLYGON_TOKEN": "test-key"}, clear=True):
            with patch.object(solution, "ask_model", return_value=RAW) as model:
                solution.read_news(NEWS, 8, memory)
                solution.read_news(NEWS, 8, memory)
                solution.read_news(NEWS, 8, memory)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(len(memory["patch_journal"]), 1)
        self.assertEqual(len(memory["news_history"]), 1)

    def test_act_uses_replayed_future_event(self):
        state = observation(texts=[NEWS])
        expected_memory = {"unit": 45.0}
        baseline = solution.decide(state, expected_memory, [])
        memory = {}
        with patch.dict(os.environ, {"POLYGON_TOKEN": "test-key"}, clear=True):
            with patch.object(solution, "ask_model", return_value=RAW) as model:
                actual = solution.act(state, memory)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(len(memory["patch_journal"]), 1)
        self.assertGreater(actual["order_raw"], baseline["order_raw"])
        self.assertEqual(set(actual), {"order_raw", "produce", "price"})
        json.dumps(memory, ensure_ascii=False, allow_nan=False)

    def test_invalid_reply_keeps_numeric_decision(self):
        state = observation(texts=[NEWS])
        expected = solution.decide(state, {"unit": 45.0}, [])
        memory = {}
        with patch.dict(os.environ, {"POLYGON_TOKEN": "test-key"}, clear=True):
            with patch.object(solution, "ask_model", return_value="{сломано"):
                actual = solution.act(state, memory)
        self.assertEqual(actual, expected)
        self.assertEqual(memory["patch_journal"], [])
        self.assertEqual(memory["responses"], {})
        self.assertTrue(memory["errors"])

    def test_supplier_message_is_not_sent_to_model(self):
        with patch.dict(os.environ, {"POLYGON_TOKEN": "test-key"}, clear=True):
            with patch.object(
                solution,
                "ask_model",
                side_effect=AssertionError("письмо поставщика ушло в модель"),
            ):
                solution.act(
                    observation(texts=["[поставщик] Доставка через три дня."]), {}
                )

    def test_memories_do_not_share_state(self):
        first, second = {}, {}
        solution.apply_patch(CREATE, first)
        self.assertEqual(len(solution.active_events(first, 8)), 1)
        self.assertEqual(solution.active_events(second, 8), [])


if __name__ == "__main__":
    unittest.main()
