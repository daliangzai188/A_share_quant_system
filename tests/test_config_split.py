"""配置拆分与启动校验（2026-11-14 项目优化第5步）。

config/config.json 只放生产代码在读的段，研究段在 config/research/research.json；
load_json_config 读 config.json 时把研究段并进来（QMT包里没有研究配置，只看到生产段）；
守护进程启动先过配置门禁（src/config_check.py + config/production_config_schema.json）。
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_check import build_schema, check_config, check_production_config  # noqa: E402
from src.utils.config import load_json_config  # noqa: E402
from tests.daemon_testing import patch_daemon  # noqa: E402
from tests.golden.project_metrics import import_edges, closure, module_files, ROOTS  # noqa: E402

CONFIG = PROJECT_ROOT / "config" / "config.json"
RESEARCH = PROJECT_ROOT / "config" / "research" / "research.json"


def raw(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class ConfigSplitTests(unittest.TestCase):
    def test_production_config_holds_only_sections_production_code_reads(self) -> None:
        files = module_files(PROJECT_ROOT)
        prod = closure(import_edges(files), ROOTS)
        text = "\n".join(files[m].read_text(encoding="utf-8", errors="replace") for m in prod)
        production, research = raw(CONFIG), raw(RESEARCH)
        unread = [k for k in production if ('"%s"' % k) not in text and ("'%s'" % k) not in text]
        self.assertEqual(unread, [], "config.json 里有生产代码不读的段，应移到 config/research/research.json")
        reads_research = [k for k in research if ('"%s"' % k) in text or ("'%s'" % k) in text]
        self.assertEqual(reads_research, [], "生产代码读了研究段（QMT包里没有研究配置）：%s" % reads_research)

    def test_loader_merges_research_sections_without_overlap(self) -> None:
        production, research = raw(CONFIG), raw(RESEARCH)
        self.assertEqual(set(production) & set(research), set())
        merged = load_json_config(CONFIG)
        self.assertEqual(set(merged), set(production) | set(research))
        for key, value in research.items():
            self.assertEqual(merged[key], value)

    def test_loader_without_research_file_returns_production_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "config"
            folder.mkdir()
            (folder / "config.json").write_text(json.dumps({"trade_mode": "paper"}), encoding="utf-8")
            self.assertEqual(load_json_config(folder / "config.json"), {"trade_mode": "paper"})
            (folder / "research").mkdir()
            (folder / "research" / "research.json").write_text(json.dumps({"trade_mode": "x"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_json_config(folder / "config.json")

    def test_current_production_config_passes(self) -> None:
        problems, notes = check_production_config(PROJECT_ROOT)
        self.assertEqual(problems, [])
        self.assertEqual(notes, [])

    def test_schema_file_matches_current_config_structure(self) -> None:
        schema = raw(PROJECT_ROOT / "config" / "production_config_schema.json")
        self.assertEqual(schema, build_schema(raw(CONFIG)),
                         "改了生产配置的结构就要重新生成：python -m src.config_check --write-schema")


class ConfigCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = raw(CONFIG)
        self.schema = build_schema(self.config)

    def test_missing_section_key_and_type_change_block(self) -> None:
        broken = copy.deepcopy(self.config)
        del broken["strategy_m"]
        del broken["live_trade"][next(iter(broken["live_trade"]))]
        broken["qmt_enabled"] = "true"
        problems, _ = check_config(broken, self.schema)
        self.assertTrue(any("strategy_m" in p for p in problems))
        self.assertTrue(any(p.startswith("缺少 live_trade.") for p in problems))
        self.assertTrue(any("qmt_enabled 应为 bool" in p for p in problems))

    def test_numbers_do_not_distinguish_int_and_float(self) -> None:
        tweaked = copy.deepcopy(self.config)
        tweaked["process_keeper"]["check_interval_sec"] = float(tweaked["process_keeper"]["check_interval_sec"]) + 0.5
        self.assertEqual(check_config(tweaked, self.schema)[0], [])

    def test_unknown_trade_mode_blocks(self) -> None:
        bad = copy.deepcopy(self.config)
        bad["trade_mode"] = "lvie"
        self.assertTrue(any("trade_mode" in p for p in check_config(bad, self.schema)[0]))

    def test_extra_sections_and_bad_handoff_time_only_notify(self) -> None:
        extra = copy.deepcopy(self.config)
        extra["backtest"] = {"x": 1}
        extra["live_trade"]["exit_pov_handoff_time"] = "14点53分"
        problems, notes = check_config(extra, self.schema)
        self.assertEqual(problems, [])
        self.assertTrue(any("backtest" in n for n in notes))
        self.assertTrue(any("14:53" in n for n in notes))


class ConfigGateTests(unittest.TestCase):
    def test_gate_passes_through_with_valid_config(self) -> None:
        from scripts import trading_daemon as daemon
        with patch_daemon("_notify_once_per") as notify:
            daemon.wait_for_config_gate()
        notify.assert_not_called()

    def test_gate_blocks_and_notifies_until_config_is_fixed(self) -> None:
        from scripts import trading_daemon as daemon
        results = [(["缺少 strategy_m"], []), ([], [])]
        with patch_daemon("check_production_config", side_effect=lambda root: results.pop(0)), \
                patch_daemon("_notify_once_per") as notify, patch_daemon("write_heartbeat") as heartbeat, \
                patch.object(daemon.time, "sleep") as sleep:
            daemon.wait_for_config_gate()
        notify.assert_called_once()
        self.assertIn("配置检查不通过", notify.call_args.args[2])
        heartbeat.assert_called_once_with("config_invalid")
        sleep.assert_called_once_with(60)


if __name__ == "__main__":
    unittest.main()
