"""按配置创建QMT券商适配器：miniQMT（src.qmt_adapter）或 QMT 内置 Python（src.qmt_inner_adapter）。

2026-11-15（项目优化，去掉最后一组循环依赖）：这段分派原样从 ``QMTBrokerAdapter.from_config`` 搬来。
原来基类按配置导入子类 ``QMTInnerBrokerAdapter``、子类又导入基类，两个模块互相依赖；现在只有这里
知道两种适配器，所有按配置建立连接的地方都调用 ``create_qmt_adapter``。
"""
from __future__ import annotations

from typing import Any

from dotenv import load_dotenv

from src.qmt_adapter import QMTBrokerAdapter, selected_transport
from src.utils.config import get_project_root


def create_qmt_adapter(broker_config: dict[str, Any]) -> QMTBrokerAdapter:
    """从 config/config.json 的 broker 配置创建 QMT 适配器。"""

    # 不覆盖调用方已经设置的环境变量（启动门禁和自动重连临时写入的 qmt_path/session_id，
    # 原因见 QMTBrokerAdapter.from_config）。
    load_dotenv(get_project_root() / ".env", override=False)

    # 内置模式必须显式选择；连接失败绝不回退到停服的 miniQMT。
    transport = selected_transport(broker_config)
    if transport == "qmt_inner":
        from src.qmt_inner_adapter import QMTInnerBrokerAdapter
        return QMTInnerBrokerAdapter.from_config(broker_config)
    if transport != "miniqmt":
        raise ValueError(f"未知QMT transport: {transport}")
    return QMTBrokerAdapter.from_config(broker_config)
