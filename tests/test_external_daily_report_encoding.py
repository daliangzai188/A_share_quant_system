"""用严格GBK重现日报打印失败；只跑隔离统计/推送替身，不读取生产配置或联网。"""
from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.daily_report import build_daily_report


ROOT = Path(__file__).resolve().parents[1]
EXTERNAL = ROOT / "qmt_native_phase0/qmt_external_checks.py"
WEEKLY_FIXTURE = ROOT / "qmt_native/inqmt/test_external_weekly.py"
DAY = "20261116"


def _review_report():
    trigger = SimpleNamespace(
        key="fixture", category="数据", detail="仅模拟中文数据缺口🚨",
        tier=1, requires_review=True, scope="", action="只读核查",
    )
    return build_daily_report(DAY, [trigger], {}, {}, {}, ["真实日报格式，模拟统计数值"])


def test_real_review_report_fails_on_strict_gbk_stdout():
    report = _review_report()
    assert report["title"].startswith("🚨")
    raw = io.BytesIO()
    with io.TextIOWrapper(raw, encoding="gbk", errors="strict") as stream:
        with pytest.raises(UnicodeEncodeError) as error:
            print("STRATEGY_DAILY_REPORT %s\n%s" % (report["title"], report["body"]), file=stream, flush=True)
    assert error.value.object[error.value.start] == "🚨"


def _stub_root(tmp_path):
    """复用已审查的离线夹具，但日报构造器用真实源码，通知只记到tmp。"""
    tree = ast.parse(WEEKLY_FIXTURE.read_text(encoding="utf-8"))
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"STUBS", "HEALTH"}
    }
    root, local = tmp_path / "root", tmp_path / "local"
    root.mkdir()
    for name, source in constants["STUBS"].items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    for name in ("src/daily_report.py", "src/portfolio_ace_m.py"):
        (root / name).write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    # 原夹具的简化触发器不足以交给真实formatter；保留同一统计替身，补齐其展示字段。
    with (root / "src/review_triggers.py").open("a", encoding="utf-8") as stream:
        stream.write('''
def factor_health_triggers(health):
    return [ReviewTrigger("factor_health:" + leg, "数据", "仅模拟中文数据缺口🚨", action="只读核查")
            for leg, item in sorted(health.items()) if item["triggered"]]
''')
    (root / "dotenv.py").write_text('''
import os
from pathlib import Path
def load_dotenv(path, override=False):
    assert Path(path) == Path(os.environ["A_SYSTEM_ROOT"]) / ".env"
    return False  # 不打开任何.env。
''', encoding="utf-8")
    (root / "scripts/collect_all_data.py").write_text('''
import json, os, sys
from pathlib import Path
json.dump({"encoding": sys.stdout.encoding, "errors": sys.stdout.errors,
           "inherited": os.environ.get("PYTHONIOENCODING")},
          open(Path(os.environ["A_SYSTEM_ROOT"]) / "child_encoding.json", "w", encoding="utf-8"))
print("隔离采集子进程中文🚨", flush=True)
''', encoding="utf-8")
    (root / "config").mkdir()
    (root / "config/config.json").write_text(json.dumps({"review_triggers": {"acknowledged": []}}), encoding="utf-8")
    (root / "data/raw").mkdir(parents=True)
    (root / "data/raw/trade_calendar.csv").write_text(
        "exchange,cal_date,is_open\nSSE,20261112,1\nSSE,20261113,1\nSSE,20261116,1\n", encoding="utf-8",
    )
    (root / "health.json").write_text(json.dumps(constants["HEALTH"]), encoding="utf-8")
    return root, local


def _gbk_env(root, local):
    return dict(os.environ, A_SYSTEM_ROOT=str(root), LOCALAPPDATA=str(local),
                PYTHONIOENCODING="gbk:strict", PYTHONUTF8="0", PYTHONDONTWRITEBYTECODE="1")


@pytest.mark.parametrize("no_push", [False, True])
def test_real_main_gbk_redirection_keeps_unicode_and_inherits_utf8(tmp_path, no_push):
    root, local = _stub_root(tmp_path)
    args = [sys.executable, "-B", str(EXTERNAL), "--signal-date", DAY]
    if no_push:
        args.append("--no-push")
    log = tmp_path / "redirected.log"

    def run():
        with log.open("wb") as output:
            result = subprocess.run(args, cwd=root, env=_gbk_env(root, local),
                                    stdout=output, stderr=subprocess.STDOUT, timeout=30)
        text = log.read_bytes().decode("utf-8")
        assert result.returncode == 0, text
        assert "UnicodeEncodeError" not in text
        assert "🚨 方案甲策略日报 11-16：需要临时复核（1项）" in text
        assert "仅模拟中文数据缺口🚨" in text and "隔离采集子进程中文🚨" in text
        return text

    run()
    child = json.loads((root / "child_encoding.json").read_text(encoding="utf-8"))
    assert child == {"encoding": "utf-8", "errors": "backslashreplace", "inherited": "utf-8:backslashreplace"}
    work = local / "A_System/qmt_native/external_checks"
    if no_push:
        assert not (root / "pushes.jsonl").exists()
        assert not (work / ("daily_%s.json" % DAY)).exists()
        assert not (work / ("done_%s.json" % DAY)).exists()
        assert not (work / "daily_report_latest.json").exists()
    else:
        report = json.loads((work / "daily_report_latest.json").read_text(encoding="utf-8"))
        pushes = [json.loads(line) for line in (root / "pushes.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(pushes) == 1
        assert pushes[0]["title"] == report["title"] and pushes[0]["body"] == report["body"]
        assert report["level"] == "review" and report["title"].startswith("🚨")
        assert (work / ("daily_%s.json" % DAY)).is_file() and (work / ("done_%s.json" % DAY)).is_file()
        mirror = root / "reports/qmt_native/external_checks/daily_report_latest.json"
        assert json.loads(mirror.read_text(encoding="utf-8")) == report
        run()
        assert len((root / "pushes.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_utf8_configuration_accepts_stringio_capture_streams():
    tree = ast.parse(EXTERNAL.read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "_configure_utf8_output")
    streams = SimpleNamespace(stdout=io.StringIO(), stderr=io.StringIO())
    environment = {}
    namespace = {"sys": streams, "os": SimpleNamespace(environ=environment)}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(EXTERNAL), "exec"), namespace)
    namespace["_configure_utf8_output"]()
    assert environment["PYTHONIOENCODING"] == "utf-8:backslashreplace"
    for stream in (streams.stdout, streams.stderr):
        print("捕获流中文🚨", file=stream)
        assert stream.getvalue() == "捕获流中文🚨\n"


def test_main_configures_stderr_before_argparse(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    result = subprocess.run([sys.executable, "-B", str(EXTERNAL), "--signal-date", DAY, "--unknown-🚨"], cwd=root,
                            env=_gbk_env(root, tmp_path / "local"), capture_output=True, timeout=15)
    assert result.returncode == 2
    stderr = result.stderr.decode("utf-8")
    assert "--unknown-🚨" in stderr and "UnicodeEncodeError" not in stderr
