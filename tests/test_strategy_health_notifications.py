"""旧周报停用与日报体检回归：只解释已有回放结果，不调用采集、券商或通知网络。"""
from __future__ import annotations

import ast
import builtins
import datetime as dt
import os
import socket
import subprocess
import sys
import types
import urllib.request
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from src import daily_report


ROOT = Path(__file__).resolve().parents[1]
LEGACY = ROOT / "scripts/strategy_health_monitor.py"
EXTERNAL = ROOT / "qmt_native_phase0/qmt_external_checks.py"
DAY = "20261116"
LEGS = ("A", "C", "E", "M")


def _values(*, minimum=7, window=180, enabled=True):
    return {
        "alignment": None,
        "strategy_health_context": {
            "signal_date": DAY, "enabled": enabled,
            "min_trades": minimum, "window_days": window,
        },
        "warnings": {
            "legs": {leg: {"value": 0.125, "trades": 17, "as_of": "20261113"} for leg in LEGS},
            "signals": {"as_of": "20261116"},
            "replay": {"m_reference_error_days": 0, "m_reference_errors_recent": []},
        },
    }


def _details(values):
    return daily_report.strategy_health_details(values)


def _leg_line(values, leg):
    lines = [line for line in _details(values) if line.startswith(leg + "：")]
    assert len(lines) == 1
    return lines[0]


def test_legacy_entry_is_a_monday_noop_without_notification_network_or_persistence(tmp_path, monkeypatch, capsys):
    source = compile(LEGACY.read_text(encoding="utf-8"), str(LEGACY), "exec")
    old_state, old_history = tmp_path / "state.json", tmp_path / "strategy_health_history.csv"
    old_state.write_bytes(b'{"E":"RED","_last_weekly":"20261109"}')
    old_history.write_bytes(b"date,leg,n\n20261109,E,12\n")
    before = (old_state.read_bytes(), old_history.read_bytes())

    class Monday(dt.date):
        @classmethod
        def today(cls):
            return cls(2026, 11, 16)

    assert Monday.today().weekday() == 0
    monkeypatch.setattr(dt, "date", Monday)
    notify = Mock(side_effect=AssertionError("旧体检入口不能发送通知"))
    fake_notify = types.ModuleType("src.notify")
    fake_notify.notify = notify
    monkeypatch.setitem(sys.modules, "src.notify", fake_notify)
    namespace = {"__name__": "legacy_health_test", "__file__": str(tmp_path / "scripts/strategy_health_monitor.py")}
    guards = []
    # 源代码已在保护前读入；执行入口期间连旧文件的读取都不允许。
    with ExitStack() as stack:
        for owner, name in (
            (builtins, "open"), (Path, "open"), (Path, "read_text"), (Path, "read_bytes"),
            (Path, "write_text"), (Path, "write_bytes"), (Path, "mkdir"), (Path, "unlink"),
            (os, "makedirs"), (os, "remove"), (os, "replace"),
            (urllib.request, "urlopen"), (socket, "create_connection"), (socket.socket, "connect"),
            (subprocess, "run"),
        ):
            guards.append(stack.enter_context(patch.object(
                owner, name, side_effect=AssertionError("旧体检入口不应读写数据或联网"),
            )))
        exec(source, namespace)
        namespace["main"]()
        namespace["main"]()  # 同一天重复手动执行也只能说明迁移去向。
    assert (old_state.read_bytes(), old_history.read_bytes()) == before
    assert "日报" in capsys.readouterr().out
    notify.assert_not_called()
    for guard in guards:
        guard.assert_not_called()


def test_detailed_health_uses_actual_window_minimum_counts_dates_and_return_scope():
    values = _values()
    values["warnings"]["legs"]["A"]["as_of"] = "20261009"
    before = deepcopy(values)
    lines = _details(values)
    text = "\n".join(lines)
    assert "180" in lines[0] and "7笔" in lines[0]
    assert "2026-11-16" in lines[0]
    assert "数据重算截至2026-11-16" in text
    assert "17笔" in _leg_line(values, "A") and "2026-10-09" in _leg_line(values, "A")
    assert "+12.5%" in _leg_line(values, "A")
    assert "各腿独立规则回放" in text and "M为ACE+M组合内回放" in text
    assert "非实盘成交收益" in text and "D不在本项" in text
    assert "不足10笔" not in text and "近12个月" not in text
    rendered = daily_report.value_lines(values)
    assert all(line in rendered for line in lines)
    assert not any(line.startswith("各腿近12个月") for line in rendered)
    assert values == before


def test_missing_health_context_preserves_the_old_value_lines_bytes():
    values = {"alignment": None, "warnings": {"legs": {
        "A": {"value": 0.125}, "C": {"value": None}, "E": {"value": 0.0}, "M": {"value": -0.02},
    }}}
    expected = ["实盘：逐笔对账未启用", "各腿近12个月（按规则）：A +12.5%、C 不足10笔、E +0.0%、M -2.0%"]
    assert daily_report.value_lines(values) == expected
    assert daily_report.value_lines({**values, "strategy_health_context": None}) == expected


@pytest.mark.parametrize("count", [1, 6])
def test_known_small_samples_use_the_configured_minimum_without_claiming_no_trade(count):
    values = _values()
    values["warnings"]["legs"]["E"] = {"value": None, "trades": count, "as_of": "20261113"}
    line = _leg_line(values, "E")
    assert f"{count}笔" in line and "7笔" in line
    assert "样本不足" in line and "不判级" in line
    assert "10笔" not in line and "没有交易" not in line


def test_explicit_zero_samples_reports_zero_effective_returns_without_inventing_a_cause():
    values = _values()
    values["warnings"]["legs"]["E"] = {"value": None, "trades": 0, "as_of": ""}
    line = _leg_line(values, "E")
    assert "有效收益0笔" in line and "无法评估" in line
    assert "原因" in line and ("待核对" in line or "未知" in line)
    assert "无信号" in line and "未到期" in line and "缺数据" in line
    assert "没有交易" not in line and "策略正常" not in line and "样本不足" not in line


@pytest.mark.parametrize("item", [None, {}, {"value": 0.125, "as_of": "20261113"}])
def test_missing_metric_or_count_is_unknown_not_zero_or_insufficient(item):
    values = _values()
    if item is None:
        values["warnings"]["legs"].pop("E")
    else:
        values["warnings"]["legs"]["E"] = item
    line = _leg_line(values, "E")
    assert "未知" in line
    assert "0笔" not in line and "不足" not in line


@pytest.mark.parametrize("count", [None, "not-a-count", float("nan"), -1, 2.5, True])
def test_invalid_sample_count_is_unknown_instead_of_becoming_zero(count):
    values = _values()
    values["warnings"]["legs"]["E"]["trades"] = count
    line = _leg_line(values, "E")
    assert "未知" in line and "0笔" not in line and "样本不足" not in line


@pytest.mark.parametrize("metric", [
    {"trades": 7, "as_of": "20261113"},
    {"trades": 7, "as_of": "20261113", "value": None},
    {"trades": 7, "as_of": "20261113", "value": float("nan")},
    {"trades": 7, "as_of": "20261113", "value": float("inf")},
    {"trades": 7, "as_of": "20261113", "value": True},
    {"trades": 7, "value": 0.125},
    {"trades": 7, "as_of": "20261117", "value": 0.125},
    {"trades": 7, "as_of": "20261340", "value": 0.125},
])
def test_enough_count_with_missing_return_or_invalid_date_still_remains_unknown(metric):
    values = _values()
    values["warnings"]["legs"]["E"] = metric
    line = _leg_line(values, "E")
    assert "未知" in line
    if {"trades", "value", "as_of"}.issubset(metric):
        assert "7笔" in line
    assert "样本不足" not in line and "规则复利" not in line


def test_a_measured_zero_return_with_enough_samples_is_a_valid_number():
    values = _values()
    values["warnings"]["legs"]["E"]["value"] = 0.0
    line = _leg_line(values, "E")
    assert "规则复利" in line and "+0.0%" in line and "17笔" in line
    assert "未知" not in line and "不足" not in line


@pytest.mark.parametrize("warnings", [None, {}, {"legs": None}])
def test_absent_replay_result_is_unknown_not_a_table_of_zero_samples(warnings):
    values = _values()
    values["warnings"] = warnings
    text = "\n".join(_details(values))
    assert "缺失" in text and "未知" in text
    assert "0笔" not in text and "不足10笔" not in text


def test_a_failed_warning_calculation_overrides_apparently_good_cached_metrics():
    values = _values()
    values["warnings_error"] = "TEST_WARNING_REPLAY_FAILURE"
    text = "\n".join(daily_report.value_lines(values))
    assert "统计失败" in text and "未知" in text
    assert "规则复利" not in text and "+12.5%" not in text


def test_disabled_rule_check_is_not_presented_as_healthy_or_calculated():
    text = "\n".join(daily_report.value_lines(_values(enabled=False)))
    assert "未启用" in text and "未评估" in text
    assert "规则复利" not in text and "+12.5%" not in text


def test_partial_m_reference_history_is_explicitly_incomplete_even_with_enough_samples():
    values = _values()
    values["warnings"]["replay"] = {
        "m_reference_error_days": 3, "m_reference_errors_recent": ["20261110", "20261112", "20261113"],
    }
    line = _leg_line(values, "M")
    assert "3天" in line and "失败" in line and "完整性未确认" in line and "未知" in line
    assert "17笔" in line and "2026-11-13" in line and "+12.5%" in line
    assert "今天未被检查" not in line
    assert "+12.5%" in _leg_line(values, "A"), "M的缺口不应抹掉可核对的ACE独立数据"


def _load_external_context(monkeypatch, *, enabled=True):
    """只编译单个纯适配函数，不执行外部脚本的chdir、dotenv、run或main。"""
    tree = ast.parse(EXTERNAL.read_text(encoding="utf-8"))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_strategy_health_context")
    settings = SimpleNamespace(enabled=enabled, min_trades=7, window_days=180)
    loader = Mock(return_value=settings)
    factory = Mock(side_effect=lambda key, category, detail, **kwargs: SimpleNamespace(
        key=key, category=category, detail=detail, action=kwargs.get("action", ""),
        tier=1, scope="", requires_review=True,
    ))
    warning_module, trigger_module = types.ModuleType("src.strategy_warnings"), types.ModuleType("src.review_triggers")
    warning_module.load_warning_settings = loader
    trigger_module.ReviewTrigger = factory
    trigger_module.CHECK_REPAIR_ACTION = "TEST_EXISTING_CHECK_REPAIR_ACTION"
    monkeypatch.setitem(sys.modules, "src.strategy_warnings", warning_module)
    monkeypatch.setitem(sys.modules, "src.review_triggers", trigger_module)
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "<external health context only>", "exec"), namespace)
    return namespace[node.name], loader, factory


def test_external_adapter_uses_existing_settings_and_does_not_change_metrics(monkeypatch):
    helper, loader, factory = _load_external_context(monkeypatch)
    config = {"strategy_warnings": {"enabled": True, "min_trades": 7, "window_days": 180}}
    metrics, triggers = _values()["warnings"], []
    before = deepcopy(metrics)
    result = helper(config, DAY, metrics, "", triggers)
    assert result == {"signal_date": DAY, "enabled": True, "min_trades": 7, "window_days": 180}
    assert metrics == before and triggers == []
    loader.assert_called_once_with(config)
    factory.assert_not_called()


@pytest.mark.parametrize("enabled, error", [(False, ""), (True, "already reported")])
def test_external_disabled_or_already_failed_statistics_do_not_duplicate_failure_triggers(monkeypatch, enabled, error):
    helper, _loader, factory = _load_external_context(monkeypatch, enabled=enabled)
    existing = SimpleNamespace(key="existing-failure")
    triggers = [existing]
    result = helper({}, DAY, None, error, triggers)
    assert result["enabled"] is enabled and triggers == [existing]
    factory.assert_not_called()


@pytest.mark.parametrize("kind", ["no_metrics", "missing_leg", "missing_count", "m_partial"])
def test_external_data_gaps_join_existing_failure_triggers_without_new_pushes(monkeypatch, kind):
    helper, _loader, factory = _load_external_context(monkeypatch)
    metrics = _values()["warnings"]
    if kind == "no_metrics":
        metrics = None
    elif kind == "missing_leg":
        metrics["legs"].pop("E")
    elif kind == "missing_count":
        metrics["legs"]["E"].pop("trades")
    else:
        metrics["replay"]["m_reference_error_days"] = 2
    triggers = []
    helper({}, DAY, metrics, "", triggers)
    factory.assert_called_once()
    assert len(triggers) == 1 and triggers[0].key == "strategy_health_data"
    assert triggers[0].category == "数据"
    assert triggers[0].action == "TEST_EXISTING_CHECK_REPAIR_ACTION"
    assert "M历史候选重算" in triggers[0].detail if kind == "m_partial" else "未知" in triggers[0].detail


@pytest.mark.parametrize("count", [0, 1, 6])
def test_zero_and_ordinary_small_samples_do_not_create_data_failure_triggers(monkeypatch, count):
    helper, _loader, factory = _load_external_context(monkeypatch)
    metrics = _values()["warnings"]
    metrics["legs"]["E"] = {"trades": count, "value": None, "as_of": "20261113" if count else ""}
    triggers = []
    context = helper({}, DAY, metrics, "", triggers)
    assert triggers == []
    assert daily_report.strategy_health_data_issues({
        "strategy_health_context": context, "warnings": metrics,
    }) == []
    factory.assert_not_called()


@pytest.mark.parametrize("metric", [
    {"trades": 17, "value": None, "as_of": "20261113"},
    {"trades": 17, "value": float("nan"), "as_of": "20261113"},
    {"trades": 17, "value": 0.125, "as_of": "20261117"},
    {"trades": "broken", "value": 0.125, "as_of": "20261113"},
])
def test_unknown_structural_data_cannot_leave_the_daily_title_saying_all_normal(monkeypatch, metric):
    helper, _loader, _factory = _load_external_context(monkeypatch)
    values = _values()
    values["warnings"]["legs"]["E"] = metric
    triggers = []
    values["strategy_health_context"] = helper({}, DAY, values["warnings"], "", triggers)
    report = daily_report.build_daily_report(DAY, triggers, {}, {}, {}, daily_report.value_lines(values))
    assert report["level"] == "review" and "一切正常" not in report["title"]
    assert "未知" in report["body"] and "TEST_EXISTING_CHECK_REPAIR_ACTION" in report["body"]


def test_external_run_really_passes_context_to_the_daily_report_without_importing_the_program():
    tree = ast.parse(EXTERNAL.read_text(encoding="utf-8"))
    run = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
    context_assign = next(node for node in ast.walk(run) if isinstance(node, ast.Assign)
                          and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                          and node.value.func.id == "_strategy_health_context")
    assert [arg.id for arg in context_assign.value.args if isinstance(arg, ast.Name)] == [
        "config", "day", "warn_metrics", "warn_error", "triggers",
    ]
    context_name = context_assign.targets[0].id
    values = next(node.value for node in ast.walk(run) if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == "values" for target in node.targets)
                  and isinstance(node.value, ast.Dict))
    context_value = next(value for key, value in zip(values.keys, values.values)
                         if isinstance(key, ast.Constant) and key.value == "strategy_health_context")
    assert isinstance(context_value, ast.Name) and context_value.id == context_name
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "value_lines"
               and any(isinstance(arg, ast.Name) and arg.id == "values" for arg in node.args)
               for node in ast.walk(run))
