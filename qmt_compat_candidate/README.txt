公告兼容：最小范围审阅副本

本目录仅保留守护修复、原生CSV读取器及对应测试、三个已有验收脚本。
未接入正式交易入口，不能据此认为10月23日后自动交易已通过验收。
完整原生迁移及重复部署副本已移到同步目录外，完整归档位置见下：
/Users/eass/A_System_Rebuild/2026-10-18/qmt_restriction_adaptation/scope_reduction_20261019/20261019T082225Z

文件位置沿用原审阅路径；验收脚本的完整依赖包仍在隔离目录，勿将此精简目录作为独立安装包运行。
隔离完整运行与验证目录：
/Users/eass/A_System_Rebuild/2026-10-18/qmt_restriction_adaptation/project
/Users/eass/A_System_Rebuild/2026-10-18/qmt_restriction_adaptation/deployment_ready_20261019

本次只移动未接线副本，正式策略、配置、Windows已安装守护和QMT登录状态均未操作。

公告适配与回测对齐的验收范围（2026-10-19用户明确）
以当前正式HEAD a0474b5f9d8bb3d57dbe2e414e78786b8cc0ec14 为基线，只适配券商通信接口。
保留原数据清洗、因子、选股、A>C>E>D、MA60/滞后6交易日、仓位、费用、滑点、T+1、买卖时点及D/POV逻辑。
相同可见输入必须产生相同计划、价格类型、价格、整手数量、意图顺序、资金占用和恢复状态；禁止以收益接近替代逐项对齐。
查询失败和超时保持未知；不得把空CSV解释为空仓、撤单成功或允许补单。
实际成交仍按券商回报记录，成交不足、费用和滑点差异须归因，不改写成回测成交。
新通道需独立验证盘口/分钟数据口径、查询完整性及新旧逐事件结果后方可申请部署；不得借公告适配重写策略。
正式HEAD隔离基线300项测试及8项子检查通过（含10项D分钟pytest），不等于替代通道或10月23日后实机验收通过。
基线与运行记录：/Users/eass/A_System_Rebuild/2026-10-19/notice_logic_alignment/formal_head_targeted_alignment_regression.json
