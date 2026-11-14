"""D盘中监控（scripts/monitor_strategy_d_intraday.py）按职责拆出的模块（2026-11-15 项目优化，纯搬移）。

support     策略参数、数据结构、配置读取和辅助函数
checkpoint  断点续跑（CheckpointMixin）
market      市场环境（MarketContextMixin）
signals     选股与触发（SignalMixin）
orders      下单与撤单（OrderMixin）

四个方法组只导入 support，彼此不导入；StrategyDMonitor（仍在 scripts/monitor_strategy_d_intraday.py）继承它们。
"""
MIXIN_MODULES = ("checkpoint", "market", "signals", "orders")
