"""Bark长消息改POST；所有网络均mock，原生日志桥也用真实shim作离线验证。"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.error
import urllib.parse

import pytest

from src import notify as notify_module


BASE = "https://example.invalid/mock-key"


class Response:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


@pytest.fixture
def transport(monkeypatch):
    cfg = {"enabled": True, "channel": "bark", "throttle_sec": 300,
           "bark_group": "方案甲日报", "bark_sound": "minuet"}
    requests = []
    with notify_module._lock:
        notify_module._last_sent.clear()
        notify_module._inflight.clear()
    monkeypatch.setenv("A_SYSTEM_DISABLE_NOTIFICATIONS", "0")
    monkeypatch.setattr(notify_module, "_load_notify_config", lambda: cfg)
    monkeypatch.setattr(notify_module, "_bark_url", lambda: BASE)

    def send(request, timeout):
        assert timeout == 8
        requests.append(request)
        return Response()

    monkeypatch.setattr(notify_module.urllib.request, "urlopen", send)
    yield SimpleNamespace(cfg=cfg, requests=requests)
    with notify_module._lock:
        notify_module._last_sent.clear()
        notify_module._inflight.clear()


def test_long_chinese_emoji_body_uses_same_endpoint_utf8_json_without_loss(transport):
    title = "🚨 方案甲日报"
    body = "完整中文明细🚨：保留换行、&、/、百分号%\n" * 100 + "末尾不能丢失"
    assert notify_module.notify("equity_curve_stop", title, body, level="timeSensitive")
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.get_method() == "POST" and request.full_url == BASE
    assert request.get_header("Content-type") == "application/json; charset=utf-8"
    assert "🚨".encode("utf-8") in request.data
    assert json.loads(request.data.decode("utf-8")) == {
        "title": title, "body": body, "group": "方案甲日报", "level": "timeSensitive", "sound": "minuet",
    }


def test_short_notification_keeps_existing_get_path_and_query(transport):
    title, body = "提醒/检查🚨", "中文内容 & = ?\n第二行"
    assert notify_module.notify("connection", title, body, level="timeSensitive")
    request = transport.requests[0]
    assert request.get_method() == "GET" and request.data is None
    url = urllib.parse.urlsplit(request.full_url)
    assert url.path == "/mock-key/" + urllib.parse.quote(title, safe="") + "/" + urllib.parse.quote(body, safe="")
    assert urllib.parse.parse_qs(url.query) == {
        "group": ["方案甲日报"], "level": ["timeSensitive"], "sound": ["minuet"],
    }


@pytest.mark.parametrize("url_size,method", [(4096, "GET"), (4097, "POST")])
def test_get_post_boundary_uses_complete_encoded_url_bytes(transport, url_size, method):
    transport.cfg.update(bark_group="group", bark_sound="")
    suffix = "?" + urllib.parse.urlencode({"group": "group", "level": "active"})
    body = "x" * (url_size - len((BASE + "/t/" + suffix).encode("utf-8")))
    assert notify_module.notify("connection", "t", body)
    request = transport.requests[0]
    assert request.get_method() == method
    if method == "GET":
        assert len(request.full_url.encode("utf-8")) == 4096
    else:
        assert request.full_url == BASE
        assert json.loads(request.data)["body"] == body


@pytest.mark.parametrize("long", [False, True])
def test_sell_failure_call_sound_volume_and_level_are_preserved(transport, long):
    transport.cfg.update(bark_sound="alarm", bark_critical_volume=9)
    body = "卖出失败🚨" * (200 if long else 1)
    assert notify_module.notify("sell_fail", "平仓失败", body, level="critical", call=True)
    request = transport.requests[0]
    expected = {"group": "方案甲日报", "level": "critical", "sound": "alarm", "call": "1", "volume": "9"}
    if long:
        assert request.get_method() == "POST"
        assert json.loads(request.data) == dict(expected, title="平仓失败", body=body)
    else:
        assert request.get_method() == "GET"
        assert urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query) == {k: [v] for k, v in expected.items()}


def test_long_non_sell_alert_keeps_existing_critical_downgrade(transport):
    transport.cfg["bark_critical_volume"] = 9
    body = "日报完整内容🚨" * 200
    assert notify_module.notify("equity_curve_stop", "日报", body, level="critical", call=True)
    data = json.loads(transport.requests[0].data)
    assert data["body"] == body and data["level"] == "active"
    assert "call" not in data and "volume" not in data


@pytest.mark.parametrize("failure", ["http_error", "non_2xx"])
def test_failed_post_releases_inflight_and_only_success_consumes_throttle(transport, monkeypatch, failure):
    def send(request, timeout):
        transport.requests.append(request)
        if len(transport.requests) == 1:
            if failure == "http_error":
                raise urllib.error.HTTPError(BASE, 431, "too long", None, None)
            return Response(503)
        return Response()

    monkeypatch.setattr(notify_module.urllib.request, "urlopen", send)
    title, body = "完整日报🚨", "长中文完整明细🚨\n" * 200
    assert not notify_module.notify("equity_curve_stop", title, body)
    assert not notify_module._inflight and not notify_module._last_sent
    assert notify_module.notify("equity_curve_stop", title, body)
    assert not notify_module.notify("equity_curve_stop", title, body)
    assert len(transport.requests) == 2
    assert all(request.get_method() == "POST" for request in transport.requests)
    assert all(json.loads(request.data)["body"] == body for request in transport.requests)


def test_long_title_preserves_existing_blank_body_fallback(transport):
    title = "长标题🚨" * 200
    assert notify_module.notify("connection", title, "")
    assert json.loads(transport.requests[0].data)["body"] == " "


def test_native_log_bridge_keeps_complete_get_wire_protocol(transport, monkeypatch):
    runtime = Path(__file__).resolve().parents[1] / "qmt_native/inqmt/qn_runtime.py"
    tree = ast.parse(runtime.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))
             and node.name in {"_QnRequest", "_QnNotifyResponse", "_qn_urlopen"}]
    lines = []
    namespace = {"QN_NOTIFY_BASE": "http://a-system-notify.invalid", "QN_NOTIFY_SEQ": [0],
                 "QN_NOTIFY_MODE": ["shadow"], "QN_RUN": ["native-fixture"], "QN_PRINT_LINES": [],
                 "_builtins": SimpleNamespace(print=lines.append), "_NetworkDisabled": OSError}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(runtime), "exec"), namespace)
    monkeypatch.setattr(notify_module, "_bark_url", lambda: namespace["QN_NOTIFY_BASE"])
    monkeypatch.setattr(notify_module.urllib.request, "Request", namespace["_QnRequest"])
    monkeypatch.setattr(notify_module.urllib.request, "urlopen", namespace["_qn_urlopen"])
    title, body = "原生日报🚨", "桥接保留完整中文🚨\n" * 200
    assert notify_module.notify("equity_curve_stop", title, body)
    assert len(lines) == 1 and lines[0].startswith("[A_SYSTEM_NOTIFY shadow] native-fixture-1 ")
    path = lines[0].split(" ", 3)[-1]
    encoded_title, encoded_body = path.split("?", 1)[0].lstrip("/").split("/", 1)
    assert urllib.parse.unquote(encoded_title) == title
    assert urllib.parse.unquote(encoded_body) == body
    assert namespace["QN_PRINT_LINES"] == lines
