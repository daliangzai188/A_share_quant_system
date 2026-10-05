"""启动原自动化程序前，只读确认 QMT 内置执行端已进入可运行状态。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any


def assert_selected_transport_ready(project_root: str | Path) -> dict[str, Any]:
    """内置通道未就绪时拒绝清除人工停机标记。

    这里只读取内置端心跳并调用账户、持仓、委托和成交查询，不会下单或撤单。
    miniQMT 配置保留原来的启动流程。
    """

    explicit_transport = os.getenv("QMT_TRANSPORT", "").strip().lower()
    if not explicit_transport:
        from qmt_inner.protocol import config_path

        try:
            inner_configured = config_path().is_file()
        except RuntimeError:
            inner_configured = False
        if inner_configured:
            raise RuntimeError(
                "本机已有QMT内置配置，但未明确设置QMT_TRANSPORT。"
                "请恢复原项目.env并设置QMT_TRANSPORT=qmt_inner；"
                "人工停机标记保持不变。"
            )
    transport = explicit_transport or "miniqmt"
    if transport != "qmt_inner":
        return {"transport": transport, "checked": False}

    from qmt_inner.protocol import FileClient, load_settings

    settings = load_settings()
    expected_runtime = (Path(project_root).resolve() / "config" / "config.json").resolve()
    actual_runtime = Path(settings["runtime_config"]).resolve()
    if actual_runtime != expected_runtime:
        raise RuntimeError(
            "QMT内置端连接了其他项目配置，人工停机标记保持不变："
            + str(actual_runtime)
        )
    client = FileClient(settings)
    hello = client.connect()
    if hello.get("mode") != "live":
        raise RuntimeError("QMT内置端仍为只读模式，人工停机标记保持不变")
    counts = {}
    for method in ("positions", "orders", "trades"):
        rows = client.call(method, {})
        if not isinstance(rows, list):
            raise RuntimeError("QMT内置端" + method + "查询返回非法，人工停机标记保持不变")
        counts[method] = len(rows)
    return {
        "transport": "qmt_inner",
        "checked": True,
        "mode": "live",
        "instance": str(hello.get("instance", client.instance or "")),
        "snapshot_counts": counts,
        "engine_revision": str(client.heartbeat().get("engine_revision", "")),
    }
