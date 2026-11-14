"""配置拆分与启动校验（2026-11-14 项目优化第5步）。

config/config.json 只放生产代码在读的段，研究段在 config/research/research.json；
load_json_config 读 config.json 时把研究段并进来（QMT包里没有研究配置，只看到生产段）；
守护进程启动先过配置门禁（src/config_check.py + config/production_config_schema.json）：
2026-11-15 起致命问题（券商连接、交易模式）不启动交易；其它结构问题只禁止开新仓，恢复和平仓照常；说明文字缺了只提示。
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

import src.config_check as config_check  # noqa: E402
from src.config_check import (  # noqa: E402
    ConfigStatus, build_schema, check_config, check_production_config, config_status,
)
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
            # 同名段以生产为准，研究段照常并进来
            (folder / "research" / "research.json").write_text(json.dumps({"trade_mode": "x", "r": 1}), encoding="utf-8")
            with self.assertLogs("src.utils.config", "WARNING"):
                self.assertEqual(load_json_config(folder / "config.json"), {"trade_mode": "paper", "r": 1})
            # 研究配置坏了：只用生产配置，不影响生产加载
            for broken in ("{不是JSON", "[1, 2]"):
                (folder / "research" / "research.json").write_text(broken, encoding="utf-8")
                with self.assertLogs("src.utils.config", "WARNING"):
                    self.assertEqual(load_json_config(folder / "config.json"), {"trade_mode": "paper"})

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
        del broken["live_trade"][next(k for k in broken["live_trade"]
                                      if not config_check.is_doc_key("live_trade." + k, k))]
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

    def test_doc_text_missing_or_changed_only_notifies(self) -> None:
        """说明文字不是平仓总闸：note、*_note、file_role、报警说明缺了或改了类型都只提示。"""
        edited = copy.deepcopy(self.config)
        del edited["notify"]["health_beacon"]["note"]
        live_note = next(k for k in edited["live_trade"] if k.endswith("_note"))
        del edited["live_trade"][live_note]
        role_section = next(k for k, v in edited.items() if isinstance(v, dict) and "file_role" in v)
        edited[role_section]["file_role"] = 1
        del edited["strategy_warnings"]["reference"]
        problems, notes = check_config(edited, self.schema)
        self.assertEqual(problems, [])
        self.assertTrue(any("notify.health_beacon.note" in n for n in notes))
        self.assertTrue(any("strategy_warnings.reference" in n for n in notes))

    def test_fatal_and_entry_only_problems(self) -> None:
        broken = copy.deepcopy(self.config)
        del broken["strategy_m"]                                  # 只禁止开新仓
        del broken["live_trade"][next(k for k in broken["live_trade"]
                                      if not config_check.is_doc_key("live_trade." + k, k))]
        status = config_status(broken, self.schema)
        self.assertEqual(status.fatal, ())
        self.assertEqual(len(status.entry), 2)
        for fatal_edit in (lambda c: c.__setitem__("trade_mode", "lvie"),
                           lambda c: c["broker"].pop("enabled"),
                           lambda c: c.__setitem__("qmt_enabled", "true")):
            bad = copy.deepcopy(self.config)
            fatal_edit(bad)
            self.assertTrue(config_status(bad, self.schema).fatal)

    def test_extra_sections_and_bad_handoff_time_only_notify(self) -> None:
        extra = copy.deepcopy(self.config)
        extra["backtest"] = {"x": 1}
        extra["live_trade"]["exit_pov_handoff_time"] = "14点53分"
        problems, notes = check_config(extra, self.schema)
        self.assertEqual(problems, [])
        self.assertTrue(any("backtest" in n for n in notes))
        self.assertTrue(any("14:53" in n for n in notes))


class ConfigGateTests(unittest.TestCase):
    def tearDown(self) -> None:
        config_check.clear_new_entry_block()

    def test_gate_passes_through_with_valid_config(self) -> None:
        from scripts import trading_daemon as daemon
        config_check.block_new_entries("旧的禁止")
        with patch_daemon("_notify_once_per") as notify:
            daemon.wait_for_config_gate()
        notify.assert_not_called()
        self.assertEqual(config_check.new_entry_block_reason(), "", "配置通过就解除禁止开新仓")

    def test_fatal_problem_blocks_everything_until_fixed(self) -> None:
        from scripts import trading_daemon as daemon
        results = [ConfigStatus(("trade_mode='lvie' 不是 paper/simulation/dry_run/research/live 之一",), (), ()),
                   ConfigStatus((), (), ())]
        with patch_daemon("production_config_status", side_effect=lambda root: results.pop(0)), \
                patch_daemon("_notify_once_per") as notify, patch_daemon("write_heartbeat") as heartbeat, \
                patch.object(daemon.time, "sleep") as sleep:
            daemon.wait_for_config_gate()
        notify.assert_called_once()
        self.assertIn("交易未启动", notify.call_args.args[2])
        heartbeat.assert_called_once_with("config_invalid")
        sleep.assert_called_once_with(60)

    def test_structure_problem_only_blocks_new_entries(self) -> None:
        """非致命的结构问题：门禁不停（照常恢复、平仓），只禁止开新仓。"""
        from scripts import trading_daemon as daemon
        status = ConfigStatus((), ("缺少 strategy_m",), ())
        with patch_daemon("production_config_status", return_value=status), \
                patch_daemon("_notify_once_per") as notify, patch.object(daemon.time, "sleep") as sleep:
            daemon.wait_for_config_gate()
        sleep.assert_not_called()
        notify.assert_called_once()
        self.assertIn("只平仓，不开新仓", notify.call_args.args[2])
        self.assertIn("缺少 strategy_m", config_check.new_entry_block_reason())
        ok, reason = daemon._new_buy_execution_gate()
        self.assertFalse(ok)
        self.assertIn("禁止开新仓", reason)

    def test_execution_channel_rejects_buy_but_not_sell_while_blocked(self) -> None:
        from unittest.mock import Mock
        from src.broker_adapter import OrderRequest
        from src.broker_execution_service import IntentSerializedBrokerProxy
        service = object.__new__(IntentSerializedBrokerProxy)   # 业务线程（含D盘中共享代理）拿到的统一执行代理
        service._intent_service = Mock()
        service._adapter_provider = Mock()

        def order(side: str) -> OrderRequest:
            return OrderRequest(ts_code="600000.SH", broker_code="600000.SH", side=side, quantity=100,
                                price_type="FIXED_PRICE", price=10.0, strategy_name="A_SYSTEM_ABC",
                                strategy_leg="A", source_key="TEST|%s" % side)

        config_check.block_new_entries("缺少 strategy_m")
        result = service.place_order(order("BUY"))
        self.assertFalse(result.accepted)
        self.assertIn("NEW_ENTRY_BLOCKED_BY_CONFIG", result.message)
        service._intent_service.submit_order.assert_not_called()
        service.place_order(order("SELL"))
        service._intent_service.submit_order.assert_called_once()
        config_check.clear_new_entry_block()
        service.place_order(order("BUY"))
        self.assertEqual(service._intent_service.submit_order.call_count, 2)

    def test_entry_block_clears_itself_once_config_is_fixed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            (root / "config" / "config.json").write_text(json.dumps({"trade_mode": "paper"}), encoding="utf-8")
            (root / "config" / "production_config_schema.json").write_text(
                json.dumps({"trade_mode": "string", "strategy_m": {"enabled": "bool"}}), encoding="utf-8")
            config_check.block_new_entries("缺少 strategy_m", root)
            with patch.object(config_check, "ENTRY_RECHECK_SECONDS", 0.0):
                self.assertIn("strategy_m", config_check.new_entry_block_reason())
                (root / "config" / "config.json").write_text(
                    json.dumps({"trade_mode": "paper", "strategy_m": {"enabled": True}}), encoding="utf-8")
                self.assertEqual(config_check.new_entry_block_reason(), "")


if __name__ == "__main__":
    unittest.main()
