"""Автономные проверки клиента: python -m unittest test_client -v.

HTTP и SDK подменяются, файлы агента временные. Настоящий .env не читается.
"""
from contextlib import redirect_stdout
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import polygon
from market_v2 import Market
from protocol_v2 import validate_patch
from memory_policy import MAX_FRAME_BYTES, MAX_STATE_BYTES, RAM_LIMIT_MB
from memory_policy import MemoryPolicyError, memory_penalty, memory_size, score_with_memory


class ClientTests(unittest.TestCase):
    def setUp(self):
        for guard in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(polygon, "_load_local_env"),
            patch("socket.socket.connect", side_effect=AssertionError("сеть запрещена")),
            patch("socket.create_connection", side_effect=AssertionError("сеть запрещена")),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.submitted = self.root / "solution.py"
        self.submitted.write_text("def act(observation, memory): return {}\n", encoding="utf-8")
        self.previous_module = sys.modules.get("polygon_restarted_solution")
        self.addCleanup(self.restore_module)

    def restore_module(self):
        sys.modules.pop("polygon_restarted_solution", None)
        if self.previous_module is not None:
            sys.modules["polygon_restarted_solution"] = self.previous_module

    def http(self, versions):
        status = Mock(status_code=200)
        status.json.return_value = {"rules_versions": versions}
        response = Mock(status_code=200)
        response.json.return_value = {"job_id": 7}
        return SimpleNamespace(get=Mock(return_value=status), post=Mock(return_value=response))

    def submit_with(self, http, **kwargs):
        output = io.StringIO()
        with patch.dict(sys.modules, {"requests": http}), redirect_stdout(output):
            result = polygon.submit(str(self.submitted), token="fake-test-token", **kwargs)
        self.assertNotIn("fake-test-token", output.getvalue())
        return result

    def assert_submission(self, challenge="c4", version=3, **kwargs):
        http = self.http({challenge: version})
        self.assertEqual(self.submit_with(http, **kwargs), {"job_id": 7})
        http.get.assert_called_once_with(polygon.PUBLIC_POLYGON_URL + "/health", timeout=15)
        http.get.return_value.raise_for_status.assert_called_once_with()
        http.post.assert_called_once()
        self.assertEqual(http.post.call_args.args, (polygon.PUBLIC_POLYGON_URL + "/submit",))
        sent = http.post.call_args.kwargs
        self.assertEqual(sent["data"], {"challenge": challenge, "rules_version": str(version)})
        self.assertEqual(sent["headers"], {"Authorization": "Bearer fake-test-token"})
        filename, stream, mime = sent["files"]["solution"]
        self.assertEqual((filename, mime), ("solution.py", "text/x-python"))
        self.assertTrue(stream.closed)

    def test_protocol_rejects_empty_event_id(self):
        invalid = {"event_id": "", "revision": 1, "operation": "create",
                   "issued_day": 0, "fields": {}}
        with self.assertRaisesRegex(ValueError, "event_id"):
            validate_patch(invalid)
        self.assertIsNone(validate_patch({**invalid, "event_id": "событие"}))

    def test_default_submission_includes_v3(self):
        self.assert_submission()

    def test_explicit_submission_includes_v3(self):
        self.assert_submission(challenge="c4", rules_version=3)

    def test_explicit_submission_keeps_old_versions(self):
        for version in (1, 2):
            with self.subTest(version=version):
                self.assert_submission(version=version, rules_version=version)

    def test_c5_c6_submission_defaults_stay_v1(self):
        for challenge in ("c5", "c6"):
            with self.subTest(challenge=challenge):
                http = self.http({challenge: 1})
                self.submit_with(http, challenge=challenge)
                self.assertEqual(http.post.call_args.kwargs["data"], {
                    "challenge": challenge, "rules_version": "1",
                })

    def test_version_mismatch_never_posts(self):
        cases = (({"c4": 1}, {}), ({"c4": 2}, {}), ({}, {}),
                 ({"c4": "3"}, {}), ({"c4": 3}, {"rules_version": 1}),
                 ({"c4": 3}, {"rules_version": 2}))
        for versions, kwargs in cases:
            with self.subTest(versions=versions, kwargs=kwargs):
                http = self.http(versions)
                with self.assertRaisesRegex(ValueError, "отправка не выполнена"):
                    self.submit_with(http, **kwargs)
                http.get.assert_called_once()
                http.post.assert_not_called()

    def test_failed_health_check_never_posts(self):
        http = self.http({"c4": 3})
        http.get.return_value.raise_for_status.side_effect = RuntimeError("нет ответа стенда")
        with self.assertRaisesRegex(RuntimeError, "нет ответа стенда"):
            self.submit_with(http)
        http.post.assert_not_called()

    def agent(self):
        source = self.root / "temporary_agent.py"
        trace = self.root / "trace.jsonl"
        source.write_text(
            "import json\n"
            "CALLS = 0\n"
            f"TRACE = {str(trace)!r}\n"
            "def act(observation, memory):\n"
            "    global CALLS\n"
            "    row = {'day': observation['day'], 'global_calls': CALLS,\n"
            "           'memory_calls': memory.get('calls', 0),\n"
            "           'seen': list(memory.get('days', [])),\n"
            "           'saved': memory.get('saved'),\n"
            "           'rules_version': observation.get('rules_version')}\n"
            "    with open(TRACE, 'a', encoding='utf-8') as stream:\n"
            "        stream.write(json.dumps(row, ensure_ascii=False) + '\\n')\n"
            "    CALLS += 1\n"
            "    memory['calls'] = memory.get('calls', 0) + 1\n"
            "    memory.setdefault('days', []).append(observation['day'])\n"
            "    memory.setdefault('saved', {'text': 'память', 'values': [None, True, 2.5]})\n"
            "    return {'order_raw': 0, 'produce': 0, 'price': 500.0}\n",
            encoding="utf-8",
        )
        spec = importlib.util.spec_from_file_location("temporary_agent", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, trace

    def test_restart_resets_globals_and_copies_json_memory(self):
        module, _ = self.agent()
        memory = {}
        module.act({"day": 0}, memory)
        expected = json.loads(json.dumps(memory, allow_nan=False))
        act, restored = polygon._restart_local_agent(module.act, memory)
        self.assertIsNot(act, module.act)
        self.assertEqual(module.CALLS, 1)
        self.assertEqual(act.__globals__["CALLS"], 0)
        self.assertEqual(restored, expected)
        self.assertIsNot(restored, memory)
        self.assertIsNot(restored["saved"], memory["saved"])
        act({"day": 1}, restored)
        self.assertEqual(restored["calls"], 2)
        self.assertEqual(memory["calls"], 1)
        self.assertEqual(module.CALLS, 1)

    def test_restart_rejects_non_json_memory(self):
        module, _ = self.agent()
        for value in ({1}, object(), math.nan, math.inf):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises((TypeError, ValueError)):
                    polygon._restart_local_agent(module.act, {"invalid": value})

    def test_local_episode_restarts_at_market_boundaries(self):
        seed, days = polygon.DEV_SEEDS[0], 60
        market = Market(seed, days)
        restart_days = {day for day in range(days) if market.restart_before_day(day)}
        self.assertGreaterEqual(len(restart_days), 2)
        module, trace = self.agent()
        profit = polygon.local_episode(module.act, seed=seed, days=days, rules_version=2)
        self.assertTrue(math.isfinite(profit))
        rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), days)
        expected_calls = 0
        for day, row in enumerate(rows):
            if day in restart_days:
                expected_calls = 0
            self.assertEqual(row["day"], day)
            self.assertEqual(row["rules_version"], 2)
            self.assertEqual(row["global_calls"], expected_calls)
            self.assertEqual(row["memory_calls"], day)
            self.assertEqual(row["seen"], list(range(day)))
            if day:
                self.assertEqual(row["saved"], {"text": "память", "values": [None, True, 2.5]})
            expected_calls += 1

    def agent_from_source(self, source):
        path = self.root / "sized_agent.py"
        path.write_text(source, encoding="utf-8")
        spec = importlib.util.spec_from_file_location("sized_agent", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_v3_restarts_before_every_act_including_first(self):
        module, trace = self.agent()
        module.CALLS = 99
        profit = polygon.local_episode(module.act, days=6)
        self.assertTrue(math.isfinite(profit))
        self.assertIsInstance(profit, (int, float))
        self.assertEqual(module.CALLS, 99)
        rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(rows), 6)
        for day, row in enumerate(rows):
            self.assertEqual(row["day"], day)
            self.assertEqual(row["rules_version"], 3)
            self.assertEqual(row["global_calls"], 0)
            self.assertEqual(row["memory_calls"], day)
            self.assertEqual(row["seen"], list(range(day)))
            if day:
                self.assertEqual(row["saved"], {"text": "память", "values": [None, True, 2.5]})

    def test_v3_json_roundtrip_breaks_shared_object_identity(self):
        module = self.agent_from_source(
            "def act(observation, memory):\n"
            "    if observation['day'] == 0:\n"
            "        shared = {'text': 'память', 'values': [None, True, 2.5]}\n"
            "        memory.update(left=shared, right=shared)\n"
            "    else:\n"
            "        assert memory['left'] == memory['right']\n"
            "        assert memory['left'] is not memory['right']\n"
            "        assert memory['left']['values'] is not memory['right']['values']\n"
            "    return {}\n"
        )
        polygon.local_episode(module.act, days=3)

    def test_v3_checks_size_before_step_even_on_last_day(self):
        for bad_day in (0, 2):
            with self.subTest(bad_day=bad_day):
                module = self.agent_from_source(
                    "def act(observation, memory):\n"
                    f"    if observation['day'] == {bad_day}:\n"
                    f"        memory['blob'] = 'x' * {MAX_STATE_BYTES}\n"
                    "    return {}\n"
                )
                game = Market(polygon.DEV_SEEDS[0], 3)
                with patch.object(polygon, "MarketV2", return_value=game), \
                        patch.object(game, "step", wraps=game.step) as step:
                    with self.assertRaisesRegex(MemoryPolicyError, "65536"):
                        polygon.local_episode(module.act, days=3)
                self.assertEqual(step.call_count, bad_day)

    def test_v3_accepts_exact_state_limit(self):
        length = MAX_STATE_BYTES - memory_size({"blob": ""})
        module = self.agent_from_source(
            "def act(observation, memory):\n"
            f"    memory['blob'] = 'x' * {length}\n"
            "    return {}\n"
        )
        _, peak = polygon._run_local_episode(module.act, 24, 1, False, False, 3)
        self.assertEqual(peak, MAX_STATE_BYTES)

    def test_v3_remembers_peak_after_state_shrinks(self):
        peak = 20_000
        length = peak - memory_size({"blob": ""})
        module = self.agent_from_source(
            "def act(observation, memory):\n"
            "    memory.clear()\n"
            f"    memory['blob'] = 'x' * ({length} if observation['day'] == 0 else 0)\n"
            "    return {}\n"
        )
        _, actual = polygon._run_local_episode(module.act, 24, 3, False, False, 3)
        self.assertEqual(actual, peak)
        output = io.StringIO()
        with redirect_stdout(output):
            polygon.local_episode(module.act, days=3, verbose=True)
        text = output.getvalue()
        self.assertIn("пик JSON-состояния: 20000 байт", text)
        self.assertIn("фиксированное списание: 30 баллов", text)
        self.assertIn("баллы за прибыль:", text)
        self.assertIn("итог:", text)

    def test_v3_rejects_invalid_state_before_step(self):
        for literal in ("(1, 2)", "{1, 2}", "float('nan')", "float('inf')", "{1: 'x'}"):
            with self.subTest(literal=literal):
                module = self.agent_from_source(
                    "def act(observation, memory):\n"
                    f"    memory['invalid'] = {literal}\n"
                    "    return {}\n"
                )
                game = Market(24, 1)
                with patch.object(polygon, "MarketV2", return_value=game), \
                        patch.object(game, "step", wraps=game.step) as step:
                    with self.assertRaises(MemoryPolicyError):
                        polygon.local_episode(module.act, days=1)
                step.assert_not_called()

    def test_v3_frame_limit_includes_newline(self):
        overhead = memory_size({"action": {"blob": ""}, "memory": {}}) + 1
        for extra in (0, 1):
            with self.subTest(extra=extra):
                length = MAX_FRAME_BYTES - overhead + extra
                module = self.agent_from_source(
                    "def act(observation, memory):\n"
                    f"    return {{'blob': 'x' * {length}}}\n"
                )
                game = Market(24, 1)
                with patch.object(polygon, "MarketV2", return_value=game), \
                        patch.object(game, "step", wraps=game.step) as step:
                    if extra:
                        with self.assertRaisesRegex(MemoryPolicyError, "262144"):
                            polygon.local_episode(module.act, days=1)
                        step.assert_not_called()
                    else:
                        polygon.local_episode(module.act, days=1)
                        step.assert_called_once()

    def test_v3_rejects_non_json_action_before_step(self):
        for literal in ("{'price': float('nan')}", "{'extra': (1, 2)}", "[1]"):
            with self.subTest(literal=literal):
                module = self.agent_from_source(
                    "def act(observation, memory):\n"
                    f"    return {literal}\n"
                )
                game = Market(24, 1)
                with patch.object(polygon, "MarketV2", return_value=game), \
                        patch.object(game, "step", wraps=game.step) as step:
                    with self.assertRaises(MemoryPolicyError):
                        polygon.local_episode(module.act, days=1)
                step.assert_not_called()

    def test_v3_uses_same_market_and_texts_as_v2(self):
        module, trace = self.agent()
        v2 = polygon.local_episode(module.act, days=60, rules_version=2)
        trace.unlink()
        v3 = polygon.local_episode(module.act, days=60, rules_version=3)
        self.assertEqual(v3, v2)

    def test_v1_local_has_no_restart_or_state_cap(self):
        module, trace = self.agent()
        with patch.object(polygon, "_restart_local_agent",
                          side_effect=AssertionError("v1 не перезапускается")):
            polygon.local_episode(module.act, days=6, rules_version=1)
        rows = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([row["global_calls"] for row in rows], list(range(6)))
        self.assertTrue(all(row["rules_version"] is None for row in rows))

    def test_v1_v2_keep_unrestricted_json_state(self):
        module = self.agent_from_source(
            "def act(observation, memory):\n"
            f"    memory['blob'] = 'x' * {MAX_STATE_BYTES + 1}\n"
            "    return {}\n"
        )
        for version in (1, 2):
            with self.subTest(version=version):
                self.assertTrue(math.isfinite(
                    polygon.local_episode(module.act, days=3, rules_version=version)
                ))

    def test_final_flag_keeps_v1_behavior(self):
        module, _ = self.agent()
        with patch.object(polygon, "_restart_local_agent",
                          side_effect=AssertionError("финальный тур v1")):
            result = polygon.local_episode(module.act, days=3, final=True)
        self.assertIsInstance(result, (int, float))

    def test_self_check_uses_peak_across_all_five_episodes(self):
        profits = [10_000.0, -20_000.0, 30_000.0, 40_000.0, 50_000.0]
        peaks = [100, 8192, 16_384, 32_769, 100]
        output = io.StringIO()
        with patch.object(polygon, "_run_local_episode", side_effect=list(zip(profits, peaks))) as run, \
                patch.object(polygon, "score_with_memory", wraps=score_with_memory) as score, \
                redirect_stdout(output):
            result = polygon.self_check(lambda *_: {})
        self.assertEqual(run.call_count, 5)
        self.assertEqual([call.args[1] for call in run.call_args_list], list(polygon.DEV_SEEDS))
        score.assert_called_once_with(profits, 32_769)
        self.assertEqual(result, {
            "profit": 22_000.0, "episodes": 5, "raw_total": 22.0,
            "state_peak_bytes": 32_769, "memory_penalty": 70, "total": -48.0,
        })
        self.assertIn("баллы за прибыль: 22.00", output.getvalue())
        self.assertIn("пик JSON-состояния: 32769 байт", output.getvalue())
        self.assertIn("фиксированное списание: 70 баллов", output.getvalue())
        self.assertIn("итог: -48.00 баллов", output.getvalue())

    def test_v1_v2_self_check_return_mean_profit(self):
        for version in (1, 2):
            with self.subTest(version=version):
                with patch.object(polygon, "local_episode", side_effect=[1000.0, 3000.0]) as run, \
                        patch.object(polygon, "score_with_memory",
                                     side_effect=AssertionError("старые правила без списания")):
                    result = polygon.self_check(lambda *_: {}, seeds=(1, 2),
                                                verbose=False, rules_version=version)
                self.assertEqual(result, 2000.0)
                self.assertEqual(run.call_count, 2)

    def test_replay_defaults_to_v3(self):
        act = lambda *_: {}
        with patch.object(polygon, "local_episode", return_value=123.0) as run:
            self.assertEqual(polygon.replay(act, seed=35), 123.0)
        run.assert_called_once_with(act, 35, verbose=True, final=False, rules_version=3)

    def test_unknown_local_version_rejected(self):
        with self.assertRaisesRegex(ValueError, "неизвестная версия"):
            polygon.local_episode(lambda *_: {}, rules_version=4)

    def test_model_transport_uses_only_fake_sdk(self):
        create = Mock(return_value=SimpleNamespace(content=[
            SimpleNamespace(type="thinking", thinking="не текст ответа"),
            SimpleNamespace(type="text", text="готово"),
        ]))
        sdk = SimpleNamespace(
            Anthropic=Mock(return_value=SimpleNamespace(messages=SimpleNamespace(create=create))),
            RateLimitError=type("FakeRateLimitError", (Exception,), {}),
        )
        with patch.dict(os.environ, {"POLYGON_TOKEN": "fake-test-token"}), \
                patch.dict(sys.modules, {"anthropic": sdk}):
            self.assertEqual(polygon.ask_model("учебный запрос"), "готово")
        sdk.Anthropic.assert_called_once_with(
            api_key="fake-test-token", base_url=polygon.PUBLIC_POLYGON_URL,
            timeout=60.0, max_retries=0,
        )
        create.assert_called_once_with(
            model="deepseek-flash", max_tokens=300,
            messages=[{"role": "user", "content": "учебный запрос"}],
            thinking={"type": "disabled"}, extra_body={"temperature": 0.0},
        )


class MemoryPolicyTests(unittest.TestCase):
    def test_shared_constants(self):
        self.assertEqual(MAX_STATE_BYTES, 65_536)
        self.assertEqual(MAX_FRAME_BYTES, 262_144)
        self.assertEqual(RAM_LIMIT_MB, 128)

    def test_size_is_canonical_utf8_not_character_count(self):
        memory = {"я": "ёж", "a": [True, None, 1.5]}
        canonical = json.dumps(memory, ensure_ascii=False, separators=(",", ":"),
                               sort_keys=True, allow_nan=False)
        self.assertEqual(memory_size(memory), len(canonical.encode("utf-8")))
        self.assertGreater(memory_size(memory), len(canonical))
        self.assertEqual(memory_size({}), 2)
        self.assertEqual(memory_size({"b": 1, "a": 2}), memory_size({"a": 2, "b": 1}))

    def test_all_tier_boundaries_are_inclusive(self):
        cases = ((0, 0), (8192, 0), (8193, 10), (16384, 10),
                 (16385, 30), (32768, 30), (32769, 70), (65536, 70))
        for size, expected in cases:
            with self.subTest(size=size):
                self.assertEqual(memory_penalty(size), expected)
        for size in (65537, -1, True, 1.0, "8192"):
            with self.subTest(size=size):
                with self.assertRaises(MemoryPolicyError):
                    memory_penalty(size)

    def test_strict_json_rejects_non_dict_and_non_json_values(self):
        circular = {}
        circular["self"] = circular
        for value in ([], None, {1: "value"}, {"x": (1, 2)}, {"x": {1}},
                      {"x": object()}, {"x": math.nan}, {"x": math.inf}, circular):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaises(MemoryPolicyError):
                    memory_size(value)

    def test_measurement_does_not_silently_truncate_oversize_state(self):
        value = {"blob": "x" * MAX_STATE_BYTES}
        self.assertGreater(memory_size(value), MAX_STATE_BYTES)
        with self.assertRaises(MemoryPolicyError):
            memory_penalty(memory_size(value))

    def test_negative_profit_keeps_fixed_deduction(self):
        result = score_with_memory([-10_000.0, 0.0], 8193)
        self.assertEqual(result["profit"], -5000.0)
        self.assertEqual(result["raw_total"], -5.0)
        self.assertEqual(result["memory_penalty"], 10)
        self.assertEqual(result["total"], -15.0)
        self.assertEqual(result["episodes"], 2)
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(MemoryPolicyError):
                    score_with_memory([value], 0)


if __name__ == "__main__":
    unittest.main()
