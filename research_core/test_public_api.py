import copy
import json
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from public_api import CONFIG, _digest_named_sources, _engine, generate_sample, run_research
from seb_engine.models import Bar, Condition, Direction, StrategyEvaluation, StrategyState, TradeTicket


class PublicApiTests(unittest.TestCase):
    def payload(self):
        return {"bars": generate_sample(), "label": "test", "kind": "synthetic"}

    def test_real_engine_is_deterministic(self):
        first = run_research(self.payload())
        second = run_research(self.payload())
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["trades"], second["trades"])
        self.assertGreater(first["metrics"]["trade_count"], 0)

    def test_cost_math_and_exit_order(self):
        result = run_research(self.payload())
        self.assertAlmostEqual(result["metrics"]["net_pnl"], result["metrics"]["gross_pnl"] - result["metrics"]["costs"])
        self.assertEqual(result["trades"], sorted(result["trades"], key=lambda value: (value["exit_time"], value["id"])))

    def test_rejects_malformed_and_tick_errors(self):
        with self.assertRaisesRegex(ValueError, "header"):
            run_research({"csv": ""})
        bars = generate_sample()[:35]
        bars[3]["close"] += .1
        with self.assertRaisesRegex(ValueError, "ticks"):
            run_research({"bars": bars})

    def test_gap_is_reported_and_configs_unchanged(self):
        before = {path.name: path.read_text(encoding="utf-8") for path in CONFIG.glob("*.json")}
        bars = generate_sample()[:80]
        del bars[40]
        result = run_research({"bars": bars})
        self.assertEqual(result["diagnostics"]["gap_count"], 1)
        self.assertEqual(before, {path.name: path.read_text(encoding="utf-8") for path in CONFIG.glob("*.json")})

    def test_prefix_does_not_change_prior_closed_trades(self):
        full = run_research(self.payload())
        prefix_bars = generate_sample()[:900]
        prefix = run_research({"bars": prefix_bars, "label": "prefix", "kind": "synthetic"})
        cutoff = prefix_bars[-1]["timestamp"]
        expected = [trade for trade in full["trades"] if trade["exit_time"] <= cutoff]
        self.assertEqual(prefix["trades"], expected)

    def test_same_bar_stop_target_collision_is_adverse_first(self):
        engine = _engine({"commission_per_contract": 1.9, "slippage_ticks": 0, "strategy": "combined"})
        stamp = datetime(2026, 7, 1, 14, 0, tzinfo=UTC)
        bars = [Bar(stamp, stamp, "MNQ", 1, 100, 100, 100, 100, 100, "test"), Bar(stamp + timedelta(minutes=1), stamp + timedelta(minutes=1), "MNQ", 1, 100, 102, 98, 100, 100, "test")]
        ticket = TradeTicket("test", "v", "MNQ", Direction.LONG, stamp, 100, 99, 101, 1, stamp + timedelta(minutes=5), {}, "test", [], "test", "fresh")
        evaluation = StrategyEvaluation("momentum_breakout", "test", "v", StrategyState.READY, Direction.LONG, stamp, [Condition("ready", "ready", True)], [], "", None, {}, "", paper_candidate=ticket)
        trade, _ = engine._simulate_trade(bars, 0, evaluation)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.first_hit, "STOP")

    def _engine_trade(self, direction, entry_open, stop, target, path, quantity=1):
        engine = _engine({"commission_per_contract": 0, "slippage_ticks": 0, "strategy": "combined"})
        stamp = datetime(2026, 7, 1, 14, 0, tzinfo=UTC)
        bars = [Bar(stamp, stamp, "MNQ", 1, 100, 100, 100, 100, 100, "test")]
        rows = [(entry_open, *path[0][1:])] + path[1:]
        for offset, (opening, high, low, close) in enumerate(rows, 1):
            moment = stamp + timedelta(minutes=offset)
            bars.append(Bar(moment, moment, "MNQ", 1, opening, high, low, close, 100, "test"))
        ticket = TradeTicket("test", "v", "MNQ", direction, stamp, 100, stop, target, quantity, stamp + timedelta(minutes=5), {}, "test", [], "test", "fresh")
        evaluation = StrategyEvaluation("momentum_breakout", "test", "v", StrategyState.READY, direction, stamp, [Condition("ready", "ready", True)], [], "", None, {}, "", paper_candidate=ticket)
        return engine._simulate_trade(bars, 0, evaluation)

    def test_actual_open_risk_resizes_and_rejects_bad_reward_risk(self):
        # The planned 5 contracts are reduced to 3 after a next-open gap widens stop risk.
        trade, _ = self._engine_trade(Direction.LONG, 105, 90, 120, [(105, 121, 104, 120)], quantity=5)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.quantity, 3)
        # This next open remains inside the bracket but leaves only .2R to the target.
        rejected, _ = self._engine_trade(Direction.LONG, 115, 90, 120, [(115, 121, 114, 120)], quantity=1)
        self.assertIsNone(rejected)

    def test_adverse_opening_stop_gaps_long_and_short(self):
        long_trade, _ = self._engine_trade(Direction.LONG, 100, 99, 105, [(100, 100, 100, 100), (98, 99, 97, 98)])
        short_trade, _ = self._engine_trade(Direction.SHORT, 100, 101, 95, [(100, 100, 100, 100), (102, 103, 101, 102)])
        self.assertEqual((long_trade.first_hit, long_trade.exit), ("STOP_GAP", 98))
        self.assertEqual((short_trade.first_hit, short_trade.exit), ("STOP_GAP", 102))

    def test_gap_segments_never_create_trade_across_missing_minute(self):
        bars = generate_sample()
        baseline_gaps = run_research({"bars": bars})["diagnostics"]["gap_count"]
        del bars[500]
        result = run_research({"bars": bars})
        self.assertEqual(result["diagnostics"]["gap_count"], baseline_gaps + 1)
        self.assertTrue(all(not (trade["entry_index"] <= 500 <= trade["exit_index"]) for trade in result["trades"]))
        for trade in result["trades"]:
            self.assertEqual(result["bars"][trade["entry_index"]]["timestamp"], trade["entry_time"])
            self.assertEqual(result["bars"][trade["exit_index"]]["timestamp"], trade["exit_time"])

    def test_rejects_invalid_cadence_and_numeric_input(self):
        bars = generate_sample()[:35]
        bars[1]["timestamp"] = "2026-07-01T13:32:30+00:00"
        with self.assertRaisesRegex(ValueError, "full minute"):
            run_research({"bars": bars})
        bars = generate_sample()[:35]
        bars[1]["timestamp"] = "2026-07-01T13:32:30+00:00"
        with self.assertRaisesRegex(ValueError, "full minute"):
            run_research({"bars": bars})
        for key, value in (("volume", -1), ("open", float("nan"))):
            bars = generate_sample()[:35]
            bars[2][key] = value
            with self.assertRaises(ValueError):
                run_research({"bars": bars})
        bars = generate_sample()[:35]
        bars[3]["timestamp"] = bars[2]["timestamp"]
        with self.assertRaisesRegex(ValueError, "strictly chronological"):
            run_research({"bars": bars})

    def test_zero_trade_equity_and_setting_bounds(self):
        stamp = datetime(2026, 7, 1, 13, 31, tzinfo=UTC)
        bars = [{"timestamp": (stamp + timedelta(minutes=index)).isoformat(), "open": 22000, "high": 22000, "low": 22000, "close": 22000, "volume": 1} for index in range(35)]
        result = run_research({"bars": bars})
        self.assertEqual(result["metrics"]["trade_count"], 0)
        self.assertEqual(result["equity"], [{"timestamp": bars[0]["timestamp"], "balance": 50000.0, "drawdown": 0.0}])
        for settings in ({"slippage_ticks": True}, {"commission_per_contract": True}, {"slippage_ticks": 1.5}, {"commission_per_contract": 101}):
            with self.assertRaises(ValueError):
                run_research({"bars": generate_sample()[:35], "settings": settings})

    def test_engine_identity_participates_in_run_identity(self):
        payload = self.payload()
        with patch("public_api._engine_fingerprint", return_value="a" * 64):
            first = run_research(payload)
        with patch("public_api._engine_fingerprint", return_value="b" * 64):
            second = run_research(payload)
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_source_identity_normalizes_line_endings_and_changes_with_source(self):
        crlf = _digest_named_sources([("adapter.py", b"line_one\r\nline_two\r\n")])
        self.assertEqual(crlf, _digest_named_sources([("adapter.py", b"line_one\nline_two\n")]))
        self.assertNotEqual(crlf, _digest_named_sources([("adapter.py", b"line_one\nchanged_logic\n")]))


if __name__ == "__main__":
    unittest.main()
