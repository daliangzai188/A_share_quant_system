# 券商原始资金流水恢复

`scripts/recover_broker_cash_ledger.py` 是独立、完整可运行的现金恢复工具，只使用 Python 标准库。
从已导出的券商原件恢复可核对的现金变化，输出必须放在项目和同步目录之外。
它不接券商接口，不下单、不撤单，不修改正式净值账本和认证状态。

## 本次修改

新增文件 `scripts/recover_broker_cash_ledger.py`：

- `restore()`：将现场恢复脚本整理为显式传入原件、输出目录、原件哈希、期初现金及查询期间的工具。
  读入后检查必需字段、单账户、人民币币种、日期、重复记录、金额和业务分类；
  使用 Decimal 逐条核对现金变化，再核对分类汇总与期末余额。
- `_write_private()`：临时文件创建时限制权限，完整写入后原子替换单个输出；异常时清理临时文件。
- `main()`：命令行参数全部显式提供；失败返回非零退出码。
- 未替换或删除原有策略、交易执行、启动恢复或正式账户账本代码。

新增 `tests/test_broker_cash_ledger_recovery.py`，使用虚构账户和临时目录验证余额核对、
冻结与利息不重复计账、原件篡改拒绝、未知业务拒绝、数据质量和输出隔离。

## 输入与运行

输入为券商导出的 GB18030 制表符分隔文本；UTF-8 BOM 导出可指定 `--encoding utf-8-sig`。
必需字段：资金账号、发生日期、发生时间、流水序号、业务名称、发生金额、剩余金额、币种。
日期用 `YYYYMMDD`；币种为人民币；原始顺序保留，不自行排序或删行。

期初现金是原件第一条发生之前的余额，必须有可核验的来源；不能为使检查通过而猜填。
预期 SHA-256 应在导出端独立核对，随后传入工具。哈希只证明字节一致，不证明导出覆盖全部历史。
下面均为示例路径，实际使用时指向已经核验的私有原件与私有目录：

```powershell
python scripts/recover_broker_cash_ledger.py --source "C:\Users\eass\AppData\Local\A_System\history\original.txt" --output "C:\Users\eass\AppData\Local\A_System\history\recovered" --expected-sha256 "<已独立核对的64位哈希>" --opening-cash "<已核验的期初现金>" --query-start 20260601 --query-end 20261011
```

macOS 同样用 `python3` 执行，传入同步目录之外的绝对路径。
输出两个文件：`recovered_broker_cash_ledger.json` 与 `recovered_broker_cash_flow.csv`。
JSON 记录原件哈希、查询期间、实际返回期间、业务分类、逐条现金变化和有记录日的最后现金余额。
CSV 使用 UTF-8 BOM。两个文件分别原子写入；如文件系统中途失败，需重跑并检查两份输出一致。
macOS/Linux 文件权限为 0600，新目录为 0700；Windows 应使用当前用户的私有 AppData 目录，
POSIX 权限数字不代表 Windows ACL 已完成独立认证。
私人原件及输出禁止放入 Git；工具输出省略账号和姓名，但仍包含私人流水标识及金额。

## 核对口径

- 银行转存/转取单列外部资金；不能当作策略盈亏。
- 证券买入/卖出单列现金清算；净清算额不能当作策略收益。
- 转存管、预冻结、取消冻结及利息结算若不改变余额，其现金变化必须为零。
  利息实际归本另计，避免重复计账。
- 中签缴款、回冲及确认缴款单列，并严格按原件余额变化核对。
- 未审核业务、币种变化、余额不符、原件哈希不符及重复记录均拒绝生成新的恢复结果。

`cash_reconciliation_ready=true` 仅表示返回记录的现金变化核对通过。
`strategy_equity_certified=false` 和 `history_coverage_certified=false` 始终保留。
无记录日不补造净值；缺少证券代码、方向、数量、价格、费用和历史持仓时，不生成成交账本或回撤认证。

## 本次恢复与验证

2026-10-11 已从券商原始资金流水恢复 113 条记录，查询期间为 2026-06-01 至 2026-10-11，
返回记录期间为 2026-06-08 至 2026-09-21。逐条余额核对 113/113 通过，
原件及恢复结果已在 Windows 和 Mac 的私有恢复目录保存并核对哈希。
完整逐笔证券成交和旧策略净值仍待取得原件；本次现金恢复不能解除历史回撤未认证提醒。
仓库只保存工具、虚构数据测试和本说明，不保存私人原件、金额明细或恢复包。

在同步目录外的导出副本运行测试：

```bash
python -m unittest discover -s tests -p 'test_broker_cash_ledger_recovery.py' -v
python -m unittest discover -s tests
```

正确结果是测试通过，现金核对数与原件行数一致，两个历史认证字段仍为 false。
失败时先查原件哈希、编码、字段、期初现金和业务余额；禁止改原件或猜余额来通过。
后续任何实盘执行改动继续先模拟、小资金验证。
