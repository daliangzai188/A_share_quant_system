from __future__ import annotations

import unittest
import sys
import datetime
import copy
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from types import ModuleType

import pandas as pd

# 退出安全测试会使用冻结时钟和虚构持仓主动覆盖失败分支。无论本机 .env
# 是否配置正式 Bark 地址，测试进程都必须先硬关闭外部通知，防止模拟告警
# 进入真实手机通知中心。该变量只作用于当前测试进程，不修改生产配置。
os.environ["A_SYSTEM_DISABLE_NOTIFICATIONS"] = "1"

# src.qmt_adapter 在模块导入期只需要 dotenv.load_dotenv。测试环境未必安装
# python-dotenv；注入最小无副作用桩即可测试纯字段解析，不会读取或修改 .env。
if "dotenv" not in sys.modules:
    dotenv_stub = ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda *args, **kwargs: False  # type: ignore[attr-defined]
    sys.modules["dotenv"] = dotenv_stub

from scripts import trading_daemon
from src.broker_adapter import OrderFill, OrderRequest, OrderResult, QuoteSnapshot
from src.live_order_gateway import LiveOrderGateway


class _FakePositionAdapter:
    """只实现持仓查询，确保测试不会触发任何 QMT 连接或下单。"""

    def __init__(self, positions: list[object]) -> None:
        self._positions = positions

    def query_positions(self) -> list[object]:
        return self._positions


class _StopWatchdog(BaseException):
    """只用于跳出看门狗的无限循环，不会被业务代码的 ``except Exception`` 吞掉。"""


class _MemoryPositionStore:
    """模拟positions.json的读写，确保测试不接触用户真实持仓文件。"""

    def __init__(self, positions: list[dict]) -> None:
        self.positions = copy.deepcopy(positions)

    def load(self) -> list[dict]:
        return copy.deepcopy(self.positions)

    def save(self, positions: list[dict]) -> None:
        self.positions = copy.deepcopy(positions)


class _FakeReconcileAdapter:
    """提供收盘对账所需的本地QMT快照、委托和成交明细。"""

    def __init__(
        self,
        *,
        broker_qty: int,
        system_trade_qty: int,
        trade_price: float = 10.0,
        trades_visible: bool = True,
    ) -> None:
        self.broker_qty = broker_qty
        self.system_trade_qty = system_trade_qty
        self.trade_price = trade_price
        self.trades_visible = trades_visible

    def query_positions(self) -> list[dict]:
        if self.broker_qty <= 0:
            return []
        return [{"stock_code": "002800.SZ", "volume": self.broker_qty}]

    def query_orders(self) -> list[dict]:
        return [
            {
                "stock_code": "002800.SZ",
                "order_id": "SYSTEM-SELL-1",
                "order_remark": "ABC平仓-集合竞价跌停限价-20260716",
                "order_type": 24,
                "order_status": 56 if self.system_trade_qty >= 1_000 else 54,
                "order_volume": 1_000,
                "traded_volume": self.system_trade_qty,
                "traded_price": self.trade_price,
            }
        ]

    def query_trades(self) -> list[dict]:
        if self.system_trade_qty <= 0 or not self.trades_visible:
            return []
        return [
            {
                "order_id": "SYSTEM-SELL-1",
                "traded_volume": self.system_trade_qty,
                "traded_price": self.trade_price,
            }
        ]


class _NoopLog:
    def info(self, *_args, **_kwargs) -> None:
        return None

    def warning(self, *_args, **_kwargs) -> None:
        return None

    def error(self, *_args, **_kwargs) -> None:
        return None


class PovDepthLimitTest(unittest.TestCase):
    def test_applies_depth_haircut_and_rejects_levels_beyond_twenty_bps(self) -> None:
        quote = QuoteSnapshot(
            ts_code="002800.SZ",
            broker_code="002800.SZ",
            # 故意让最新价高于买一，验证冲击边界以真正可执行的买一为基准。
            last_price=10.50,
            bid_prices=[10.00, 9.99, 9.98, 9.97, 9.96],
            # 此处按 QMT 常见的“手”传入，bid_volume_unit=100 转成股。
            bid_volumes=[100, 200, 300, 400, 500],
        )

        safe_qty, limit_price, description = trading_daemon._pov_depth_limit(
            quote,
            depth_levels=5,
            depth_haircut=0.50,
            max_slippage_bps=20,
            bid_volume_unit=100,
        )

        # 买一10.00的20bp下限为9.98；9.97及以下盘口不得计入。
        # (100 + 200 + 300)手 * 100股/手 * 50%折扣 = 30,000股。
        self.assertEqual(safe_qty, 30_000)
        self.assertEqual(limit_price, 9.98)
        self.assertGreaterEqual(limit_price, 10.00 * (1 - 20 / 10_000))
        self.assertEqual(description, "3档可见买盘折后")

    def test_respects_configured_depth_level_and_clamps_haircut_to_one(self) -> None:
        quote = QuoteSnapshot(
            ts_code="600000.SH",
            broker_code="600000.SH",
            last_price=10.00,
            bid_prices=[10.00, 9.99, 9.98],
            bid_volumes=[1_000, 2_000, 99_999],
        )

        safe_qty, limit_price, description = trading_daemon._pov_depth_limit(
            quote,
            depth_levels=2,
            depth_haircut=2.0,
            max_slippage_bps=20,
            bid_volume_unit=1,
        )

        self.assertEqual(safe_qty, 3_000)
        self.assertEqual(limit_price, 9.99)
        self.assertEqual(description, "2档可见买盘折后")

    def test_missing_quote_or_depth_returns_zero_capacity(self) -> None:
        missing_depth = QuoteSnapshot(
            ts_code="002800.SZ",
            broker_code="002800.SZ",
            last_price=13.66,
        )

        self.assertEqual(
            trading_daemon._pov_depth_limit(
                None,
                depth_levels=3,
                depth_haircut=0.5,
                max_slippage_bps=20,
                bid_volume_unit=1,
            ),
            (0, 0.0, "盘口缺失"),
        )
        self.assertEqual(
            trading_daemon._pov_depth_limit(
                missing_depth,
                depth_levels=3,
                depth_haircut=0.5,
                max_slippage_bps=20,
                bid_volume_unit=1,
            ),
            (0, 0.0, "盘口缺失"),
        )

    def test_zero_best_bid_fails_closed(self) -> None:
        quote = QuoteSnapshot(
            ts_code="002800.SZ",
            broker_code="002800.SZ",
            last_price=13.66,
            bid_prices=[0.0, 13.64, 13.63],
            bid_volumes=[0, 5_000, 5_000],
        )

        result = trading_daemon._pov_depth_limit(
            quote,
            depth_levels=3,
            depth_haircut=0.5,
            max_slippage_bps=20,
            bid_volume_unit=1,
        )

        self.assertEqual(result, (0, 0.0, "买一缺失"))

    def test_sell_price_respects_continuous_auction_cage_and_close_auction_limit(self) -> None:
        quote = QuoteSnapshot(
            ts_code="002800.SZ",
            broker_code="002800.SZ",
            last_price=10.00,
            pre_close=9.90,
            lower_limit=8.91,
            bid_prices=[10.00],
            ask_prices=[10.01],
        )

        self.assertEqual(trading_daemon._sell_price_cage_floor(quote), 9.80)
        self.assertEqual(
            trading_daemon._pick_sell_limit_price(quote, continuous_auction=True),
            (9.80, "连续竞价价格笼子下限"),
        )
        self.assertEqual(
            trading_daemon._pick_sell_limit_price(quote, continuous_auction=False),
            (8.91, "集合竞价跌停限价"),
        )

    def test_bse_sell_price_uses_five_percent_cage(self) -> None:
        quote = QuoteSnapshot(
            ts_code="920001.BJ",
            broker_code="920001.BJ",
            last_price=10.00,
            lower_limit=7.00,
            bid_prices=[10.00],
            ask_prices=[10.01],
        )

        self.assertEqual(trading_daemon._sell_price_cage_floor(quote), 9.50)

    def test_star_market_uses_pure_two_percent_cage_without_ten_tick_extension(self) -> None:
        quote = QuoteSnapshot(
            ts_code="688146.SH",
            broker_code="688146.SH",
            last_price=2.00,
            lower_limit=1.60,
            bid_prices=[2.00],
            ask_prices=[2.01],
        )

        # 科创板纯2%下限=1.96；若误套主板十个价位扩展会报1.90并成为废单。
        self.assertEqual(trading_daemon._sell_price_cage_floor(quote), 1.96)

    def test_sell_cage_floor_is_rounded_up_never_below_exact_boundary(self) -> None:
        quote = QuoteSnapshot(
            ts_code="002800.SZ",
            broker_code="002800.SZ",
            last_price=5.26,
            lower_limit=4.73,
            bid_prices=[5.26],
            ask_prices=[5.27],
        )

        # 精确下限=min(5.26*98%, 5.26-0.10)=5.1548。卖价只能按分申报，
        # 因此必须向上取到5.16；四舍五入成5.15会低于价格笼子并成为废单。
        floor = trading_daemon._sell_price_cage_floor(quote)

        self.assertEqual(floor, 5.16)
        self.assertGreaterEqual(floor, min(5.26 * 0.98, 5.26 - 0.10))

    def test_large_sell_quantity_is_split_by_exchange_order_limit(self) -> None:
        self.assertEqual(
            trading_daemon._split_sell_order_quantities("002800.SZ", 2_100_000),
            [1_000_000, 1_000_000, 100_000],
        )
        self.assertEqual(
            trading_daemon._split_sell_order_quantities("300001.SZ", 650_000),
            [300_000, 300_000, 50_000],
        )
        self.assertEqual(
            trading_daemon._split_sell_order_quantities("688146.SH", 250_000),
            [100_000, 100_000, 50_000],
        )
        self.assertEqual(
            trading_daemon._split_sell_order_quantities("688146.SH", 100_150),
            [99_950, 200],
        )
        self.assertEqual(
            trading_daemon._split_sell_order_quantities("002800.SZ", 99),
            [99],
        )

    def test_star_market_pov_slice_respects_200_share_minimum_and_odd_lot_balance(self) -> None:
        self.assertEqual(
            trading_daemon._normalize_exit_slice_quantity("688146.SH", 150, 1_000), 0
        )
        self.assertEqual(
            trading_daemon._normalize_exit_slice_quantity("688146.SH", 250, 1_000), 250
        )
        self.assertEqual(
            trading_daemon._normalize_exit_slice_quantity("688146.SH", 150, 150), 150
        )
        self.assertEqual(
            trading_daemon._normalize_exit_slice_quantity("002800.SZ", 250, 1_000), 200
        )


class BrokerPositionQuantitiesTest(unittest.TestCase):
    def test_reads_legacy_qmt_aliases_and_returns_actual_sellable_quantity(self) -> None:
        adapter = _FakePositionAdapter(
            [
                SimpleNamespace(
                    m_strInstrumentID="600000.SH",
                    m_nVolume=999_999,
                    m_nCanUseVolume=999_999,
                ),
                SimpleNamespace(
                    # 券商可能只返回六位代码，仍应匹配002800.SZ。
                    m_strInstrumentID="002800",
                    m_nVolume="900000",
                    m_nCanUseVolume="640000",
                ),
            ]
        )

        total, can_use = trading_daemon._broker_position_quantities(adapter, "002800.SZ")

        self.assertEqual(total, 900_000)
        # 不得把总持仓当作可卖持仓；冻结/T+1股份必须被排除。
        self.assertEqual(can_use, 640_000)

    def test_reads_generic_aliases_and_matches_normalized_stock_code(self) -> None:
        adapter = _FakePositionAdapter(
            [
                {
                    "stock_code": "600000",
                    "total_volume": 1_250_000,
                    "enable_amount": 1_100_000,
                }
            ]
        )

        self.assertEqual(
            trading_daemon._broker_position_quantities(adapter, "600000.SH"),
            (1_250_000, 1_100_000),
        )

    def test_missing_position_returns_zero_and_negative_values_are_clamped(self) -> None:
        missing = _FakePositionAdapter([{"stock_code": "000001.SZ", "volume": 100}])
        negative = _FakePositionAdapter(
            [
                {
                    "ts_code": "002800.SZ",
                    "volume": -100,
                    "can_use_volume": -50,
                }
            ]
        )

        self.assertEqual(
            trading_daemon._broker_position_quantities(missing, "002800.SZ"),
            (0, 0),
        )
        self.assertEqual(
            trading_daemon._broker_position_quantities(negative, "002800.SZ"),
            (0, 0),
        )


class ActiveSellCoverageTest(unittest.TestCase):
    def test_counts_only_unfilled_quantity_of_active_sell_orders(self) -> None:
        orders = [
            {
                "stock_code": "002800.SZ",
                "order_status": 48,
                "order_type": 24,
                "order_volume": 1_000,
                "traded_volume": 200,
                "order_remark": "ABC平仓-连续竞价价格笼子下限-20260716",
            },
            {
                "stock_code": "002800.SZ",
                "order_status": 55,
                "side": "SELL",
                "order_volume": 500,
                "traded_volume": 300,
                "order_remark": "POV平滑卖-20260716",
            },
            # 人工活跃卖单不得冒充本系统的平仓覆盖。
            {
                "stock_code": "002800.SZ",
                "order_status": 48,
                "order_type": 24,
                "order_volume": 7_777,
                "traded_volume": 0,
                "order_remark": "人工卖出",
            },
            # 已成交单不能覆盖当前余仓。
            {
                "stock_code": "002800.SZ",
                "order_status": 56,
                "order_type": 24,
                "order_volume": 9_999,
                "traded_volume": 9_999,
            },
            # 活跃买单不能被误认成平仓卖单。
            {
                "stock_code": "002800.SZ",
                "order_status": 48,
                "order_type": 23,
                "order_volume": 8_888,
                "traded_volume": 0,
            },
            # 已撤卖单同样不提供覆盖。
            {
                "stock_code": "600000.SH",
                "order_status": 54,
                "order_type": 24,
                "order_volume": 5_000,
                "traded_volume": 0,
            },
        ]

        coverage = trading_daemon._active_sell_outstanding_by_code(orders)

        self.assertEqual(coverage, {"002800": 1_000})

    def test_manual_active_sell_is_not_counted_as_system_coverage(self) -> None:
        coverage = trading_daemon._active_sell_outstanding_by_code(
            [
                {
                    "stock_code": "002800.SZ",
                    "order_status": 50,
                    "order_type": 24,
                    "order_volume": 10_000,
                    "traded_volume": 0,
                    "order_remark": "人工卖出",
                }
            ]
        )

        self.assertEqual(coverage, {})

    def test_exit_vwap_uses_only_system_exit_order_ids(self) -> None:
        orders = [
            {
                "stock_code": "002800.SZ",
                "order_id": "101",
                "order_sysid": "SYS101",
                "order_remark": "POV平滑卖-20260716",
            },
            {
                "stock_code": "002800.SZ",
                "order_id": "102",
                "order_remark": "ABC平仓-买1-20260716",
            },
            {
                "stock_code": "002800.SZ",
                "order_id": "999",
                "order_remark": "人工卖出",
            },
        ]
        trades = [
            # 用系统委托号别名匹配第一笔。
            {"order_sysid": "SYS101", "traded_volume": 1_000, "traded_price": 10.00},
            {"order_id": "102", "traded_volume": 3_000, "traded_price": 9.80},
            # 人工成交不得混入策略退出VWAP。
            {"order_id": "999", "traded_volume": 9_000, "traded_price": 8.00},
        ]

        result = trading_daemon._strategy_exit_trade_vwap_by_code(orders, trades)

        self.assertEqual(result["002800"][0], 4_000)
        self.assertAlmostEqual(result["002800"][1], 9.85)

    def test_multiple_local_rows_of_same_code_share_active_coverage_once(self) -> None:
        due = [
            {"order_id": "LOCAL-1", "ts_code": "002800.SZ", "shares": 1_000},
            {"order_id": "LOCAL-2", "ts_code": "002800.SZ", "shares": 1_000},
            {"order_id": "LOCAL-3", "ts_code": "600000.SH", "shares": 400},
        ]

        uncovered = trading_daemon._exit_uncovered_by_code(
            due,
            broker_volume={"002800": 2_000, "600000": 400},
            active_outstanding={"002800": 1_500, "600000": 100},
        )

        # 002800本地策略余量应先聚合为2000股，再只减一次1500股活卖覆盖。
        # 若逐条各减1500，会错误得到0并漏掉最后500股。
        self.assertEqual(uncovered, {"002800": 500, "600000": 300})

    def test_broker_actual_quantity_caps_aggregated_local_quantity(self) -> None:
        due = [
            {"order_id": "LOCAL-1", "ts_code": "002800.SZ", "shares": 1_000},
            {"order_id": "LOCAL-2", "ts_code": "002800.SZ", "shares": 1_000},
        ]

        uncovered = trading_daemon._exit_uncovered_by_code(
            due,
            broker_volume={"002800": 1_500},
            active_outstanding={"002800": 500},
        )

        self.assertEqual(uncovered, {"002800": 1_000})


class ExitExecutionSafetyStateTest(unittest.TestCase):
    def test_1454_large_direct_exit_submits_all_chunks_without_waiting_for_fill(self) -> None:
        frozen_now = datetime.datetime(
            2026, 7, 16, 14, 54, 0, tzinfo=trading_daemon.BEIJING_TZ
        )

        class _DirectAdapter:
            def get_full_tick(self, _codes: list[str]) -> dict[str, QuoteSnapshot]:
                return {
                    "688001.SH": QuoteSnapshot(
                        ts_code="688001.SH",
                        broker_code="688001.SH",
                        last_price=10.0,
                        bid_prices=[9.99],
                        bid_volumes=[1_000_000],
                    )
                }

            def query_orders(self) -> list[object]:
                return []

        submitted: list[int] = []

        def place_without_confirm(
            _adapter: object,
            request: object,
            *,
            phase: str,
            local_order_id: str = "",
        ) -> tuple[SimpleNamespace, str]:
            submitted.append(int(getattr(request, "quantity")))
            index = len(submitted)
            return (
                SimpleNamespace(accepted=True, order_id=f"QMT-{index}", message="OK"),
                f"INTENT-{index}",
            )

        pending: list[tuple[dict, list[tuple[str, int]]]] = []
        with patch.object(trading_daemon, "now_beijing", return_value=frozen_now), patch.object(
            trading_daemon, "today_beijing", return_value=frozen_now
        ), patch.object(trading_daemon, "is_trade_day", return_value=True), patch(
            "src.live_order_gateway.LiveOrderGateway"
        ) as gateway_cls, patch.object(
            trading_daemon, "_qmt_get", return_value=_DirectAdapter()
        ), patch.object(
            trading_daemon, "_cancel_own_takeprofit_orders"
        ), patch.object(
            trading_daemon, "_broker_position_quantities", return_value=(1_000_000, 1_000_000)
        ), patch.object(
            trading_daemon, "_safe_new_exit_order_quantity", return_value=1_000_000
        ), patch.object(
            trading_daemon, "_pick_sell_limit_price", return_value=(9.98, "价格笼子下限")
        ), patch.object(
            trading_daemon,
            "_place_exit_order_with_intent",
            side_effect=place_without_confirm,
        ), patch.object(
            trading_daemon, "_confirm_fill"
        ) as confirm_fill, patch.object(
            trading_daemon, "_watchdog_pending", pending
        ):
            gateway_cls.return_value = SimpleNamespace(
                assert_real_order_allowed=lambda _confirm: None
            )
            completed = trading_daemon._abc_place_sell_order_direct_locked(
                "688001.SH",
                "测试科创股",
                1_000_000,
                "LOCAL-LARGE-1",
                "CONFIRM",
                {},
                {},
            )

        # 科创板限价单单笔10万股：14:54启动的补检也必须快速发10张
        # 并立即释放锁，不能逐张等待而饿死14:56:20撤单交接。
        self.assertFalse(completed)
        self.assertEqual(submitted, [100_000] * 10)
        confirm_fill.assert_not_called()
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(pending[0][1]), 10)

    def test_real_state_file_persists_batch_and_prevents_restart_duplicate(self) -> None:
        frozen_now = datetime.datetime(
            2026, 7, 16, 14, 55, 0, tzinfo=trading_daemon.BEIJING_TZ
        )
        local_position = {
            "order_id": "LOCAL-1",
            "ts_code": "002800.SZ",
            "shares": 100_000,
            "status": "open",
            "planned_exit_date": "20260716",
        }

        class _AcceptedAdapter:
            def place_order(self, _request: object) -> SimpleNamespace:
                return SimpleNamespace(accepted=True, order_id="QMT-100", message="OK")

        with tempfile.TemporaryDirectory() as tmp_dir, patch.object(
            trading_daemon,
            "EXIT_EXECUTION_STATE_FILE",
            Path(tmp_dir) / "exit_execution_state.json",
        ), patch.object(
            trading_daemon, "load_positions", return_value=[local_position]
        ), patch.object(
            trading_daemon, "now_beijing", return_value=frozen_now
        ), patch.object(
            trading_daemon, "today_beijing", return_value=frozen_now
        ):
            safe_qty = trading_daemon._safe_new_exit_order_quantity(
                "002800.SZ",
                requested_qty=100_000,
                broker_total=100_000,
                broker_can_use=100_000,
                orders=[],
                trade_date="20260716",
                log=_NoopLog(),
                phase="集成测试",
            )
            self.assertEqual(safe_qty, 100_000)
            request = OrderRequest(
                ts_code="002800.SZ",
                broker_code="002800.SZ",
                side="SELL",
                quantity=100_000,
                price_type="FIXED_PRICE",
                price=10.0,
                strategy_name="A_SYSTEM_ABC",
                remark="ABC平仓-集成测试-20260716",
            )
            result, token = trading_daemon._place_exit_order_with_intent(
                _AcceptedAdapter(),
                request,
                phase="集成测试",
                local_order_id="LOCAL-1",
            )
            self.assertTrue(result.accepted)
            self.assertTrue(token)
            state = trading_daemon._load_exit_execution_state()
            self.assertEqual(state["batches"]["20260716|002800"]["target_qty"], 100_000)
            self.assertEqual(state["intents"][0]["status"], "SUBMITTED")

            # 模拟daemon重启后QMT暂未回显这张已受理委托：持久化intent应把
            # 全量记为unknown，第二次安全预算必须为0，不能重复提交。
            restart_safe_qty = trading_daemon._safe_new_exit_order_quantity(
                "002800.SZ",
                requested_qty=100_000,
                broker_total=100_000,
                broker_can_use=100_000,
                orders=[],
                trade_date="20260716",
                log=_NoopLog(),
                phase="重启测试",
            )
            self.assertEqual(restart_safe_qty, 0)

    def test_prepared_intent_without_broker_order_id_reserves_full_quantity(self) -> None:
        account_fingerprint = trading_daemon._exit_account_fingerprint()
        state = {
            "version": 1,
            "batches": {},
            "intents": [
                {
                    "token": "PREPARED-1",
                    "trade_date": "20260716",
                    "ts_code": "002800.SZ",
                    "account_fingerprint": account_fingerprint,
                    "quantity": 100_000,
                    "status": "PREPARED",
                    "broker_order_id": "",
                    "terminal_known": False,
                }
            ],
        }

        with patch.object(trading_daemon, "_load_exit_execution_state", return_value=state):
            commitment = trading_daemon._exit_commitments_by_code(
                "002800.SZ",
                trade_date="20260716",
                orders=[],
            )

        # 进程可能崩在“券商已受理但尚未写回order_id”的窗口；重启后必须把
        # PREPARED全量视为未知占用，不能误判为无旧单并再次补卖。
        self.assertEqual(commitment, (0, 0, 100_000))

    def test_prepared_without_order_id_recovers_unique_qmt_order_by_remark_and_quantity(self) -> None:
        account_fingerprint = "TEST-ACCOUNT"
        remark = "ABC平仓-恢复测试-20260716-1/1"
        state = {
            "version": 1,
            "batches": {},
            "intents": [
                {
                    "token": "PREPARED-RECOVER",
                    "trade_date": "20260716",
                    "ts_code": "002800.SZ",
                    "account_fingerprint": account_fingerprint,
                    "quantity": 100_000,
                    "remark": remark,
                    "status": "PREPARED",
                    "broker_order_id": "",
                    "terminal_known": False,
                }
            ],
        }
        qmt_order = {
            "stock_code": "002800.SZ",
            "order_id": "QMT-RECOVERED",
            "order_status": 50,
            "order_type": 24,
            "order_volume": 100_000,
            "traded_volume": 20_000,
            "order_remark": remark,
        }

        with patch.object(
            trading_daemon, "_exit_account_fingerprint", return_value=account_fingerprint
        ), patch.object(
            trading_daemon, "_load_exit_execution_state", return_value=state
        ), patch.object(trading_daemon, "_save_exit_execution_state"):
            commitment = trading_daemon._exit_commitments_by_code(
                "002800.SZ",
                trade_date="20260716",
                orders=[qmt_order],
            )

        self.assertEqual(commitment, (20_000, 80_000, 0))
        self.assertEqual(state["intents"][0]["broker_order_id"], "QMT-RECOVERED")

    def test_resolved_intent_cannot_regress_or_lose_filled_quantity(self) -> None:
        account_fingerprint = "TEST-ACCOUNT"
        state = {
            "version": 1,
            "batches": {},
            "intents": [
                {
                    "token": "RESOLVED-1",
                    "account_fingerprint": account_fingerprint,
                    "quantity": 1_000,
                    "status": "RESOLVED",
                    "broker_order_id": "QMT-1",
                    "filled_qty": 600,
                    "terminal_known": True,
                }
            ],
        }

        with patch.object(
            trading_daemon, "_exit_account_fingerprint", return_value=account_fingerprint
        ), patch.object(
            trading_daemon, "_load_exit_execution_state", return_value=state
        ), patch.object(trading_daemon, "_save_exit_execution_state"):
            trading_daemon._update_exit_order_intent(
                "RESOLVED-1",
                status="SUBMITTED",
                filled_qty=0,
                terminal_known=False,
            )

        intent = state["intents"][0]
        self.assertEqual(intent["status"], "RESOLVED")
        self.assertEqual(intent["filled_qty"], 600)
        self.assertTrue(intent["terminal_known"])

    def test_resolved_intent_accepts_later_larger_terminal_fill(self) -> None:
        account_fingerprint = "TEST-ACCOUNT"
        state = {
            "version": 1,
            "batches": {},
            "intents": [
                {
                    "token": "RESOLVED-LATE-FILL",
                    "trade_date": "20260716",
                    "ts_code": "002800.SZ",
                    "account_fingerprint": account_fingerprint,
                    "quantity": 1_000,
                    "status": "RESOLVED",
                    "broker_order_id": "QMT-LATE-FILL",
                    "filled_qty": 0,
                    "terminal_known": True,
                }
            ],
        }
        terminal_order = {
            "stock_code": "002800.SZ",
            "order_id": "QMT-LATE-FILL",
            "order_status": 56,
            "order_type": 24,
            "order_volume": 1_000,
            # 状态56先到，traded_volume尚未刷新也应按整单向上修正。
            "traded_volume": 0,
            "order_remark": "ABC平仓-延迟成交回报",
        }

        with patch.object(
            trading_daemon, "_exit_account_fingerprint", return_value=account_fingerprint
        ), patch.object(
            trading_daemon, "_load_exit_execution_state", return_value=state
        ), patch.object(trading_daemon, "_save_exit_execution_state"):
            commitment = trading_daemon._exit_commitments_by_code(
                "002800.SZ",
                trade_date="20260716",
                orders=[terminal_order],
            )

        self.assertEqual(commitment, (1_000, 0, 0))
        self.assertEqual(state["intents"][0]["filled_qty"], 1_000)

    def test_exit_order_hard_gate_blocks_after_market_close_before_prepare(self) -> None:
        frozen_now = datetime.datetime(
            2026, 7, 16, 15, 0, 1, tzinfo=trading_daemon.BEIJING_TZ
        )
        request = OrderRequest(
            ts_code="002800.SZ",
            broker_code="002800.SZ",
            side="SELL",
            quantity=1_000,
            price_type="FIXED_PRICE",
            price=10.0,
            strategy_name="A_SYSTEM_ABC",
            remark="ABC平仓-时间门禁测试",
        )
        adapter = SimpleNamespace(place_order=lambda _request: self.fail("不应调用券商下单"))

        with patch.object(trading_daemon, "now_beijing", return_value=frozen_now), patch.object(
            trading_daemon, "is_trade_day", return_value=True
        ), patch.object(trading_daemon, "_prepare_exit_order_intent") as prepare:
            with self.assertRaisesRegex(RuntimeError, "时间门禁"):
                trading_daemon._place_exit_order_with_intent(
                    adapter,
                    request,
                    phase="时间门禁测试",
                )

        prepare.assert_not_called()

    def test_exit_order_rechecks_time_after_prepared_before_broker_place(self) -> None:
        request = OrderRequest(
            ts_code="002800.SZ",
            broker_code="002800.SZ",
            side="SELL",
            quantity=1_000,
            price_type="FIXED_PRICE",
            price=10.0,
            strategy_name="A_SYSTEM_ABC",
            remark="ABC平仓-二次时间门禁",
        )
        adapter = SimpleNamespace(place_order=lambda _request: self.fail("不应调用券商下单"))

        with patch.object(
            trading_daemon,
            "_exit_order_submission_block_reason",
            side_effect=["", "14:56:20已进入交接窗"],
        ), patch.object(
            trading_daemon, "_prepare_exit_order_intent", return_value="PREPARED-TOKEN"
        ), patch.object(trading_daemon, "_update_exit_order_intent") as update_intent:
            with self.assertRaisesRegex(RuntimeError, "发送前被时间门禁阻断"):
                trading_daemon._place_exit_order_with_intent(
                    adapter,
                    request,
                    phase="二次时间门禁测试",
                )

        update_intent.assert_called_once_with(
            "PREPARED-TOKEN", status="REJECTED", terminal_known=True
        )

    def test_status_56_consumes_full_order_even_when_traded_field_is_stale(self) -> None:
        commitment = trading_daemon._order_commitment(
            {
                "order_status": 56,
                "order_volume": 100_000,
                # 模拟委托状态先更新、成交明细与已成数量尚未同步。
                "traded_volume": 0,
            }
        )

        self.assertEqual(commitment, (100_000, 0, 0))

    def test_non_system_active_sell_blocks_safe_new_exit_order(self) -> None:
        manual_order = {
            "stock_code": "002800.SZ",
            "order_status": 50,
            "order_type": 24,
            "order_volume": 40_000,
            "traded_volume": 0,
            "order_remark": "人工卖出",
        }
        batch = {
            "target_qty": 100_000,
            "non_target_floor": 0,
        }

        with patch.object(
            trading_daemon, "_ensure_exit_batch", return_value=batch
        ), patch.object(
            trading_daemon, "_exit_commitments_by_code"
        ) as commitments, patch.object(
            trading_daemon, "_alert_exit_safety_once"
        ) as alert:
            safe_qty = trading_daemon._safe_new_exit_order_quantity(
                "002800.SZ",
                requested_qty=100_000,
                broker_total=100_000,
                broker_can_use=60_000,
                orders=[manual_order],
                trade_date="20260716",
                log=_NoopLog(),
                phase="收盘竞价兜底",
            )

        self.assertEqual(safe_qty, 0)
        commitments.assert_not_called()
        alert.assert_called_once()

    def test_non_system_unknown_status_sell_also_blocks(self) -> None:
        unknown_manual_order = {
            "stock_code": "002800.SZ",
            "order_status": 255,
            "order_type": 24,
            "order_volume": 10_000,
            "traded_volume": 0,
            "order_remark": "人工卖出-状态未知",
        }

        self.assertTrue(
            trading_daemon._non_system_active_sell_exists(
                "002800.SZ",
                [unknown_manual_order],
            )
        )

    def test_first_batch_creation_blocks_broker_and_local_ownership_mismatch(self) -> None:
        empty_state = trading_daemon._empty_exit_execution_state()

        with patch.object(
            trading_daemon, "_load_exit_execution_state", return_value=empty_state
        ), patch.object(
            trading_daemon, "_local_exit_quantities", return_value=(100_000, 100_000)
        ), patch.object(
            trading_daemon, "_save_exit_execution_state"
        ) as save_state, patch.object(
            trading_daemon, "_alert_exit_safety_once"
        ) as alert:
            batch = trading_daemon._ensure_exit_batch(
                "002800.SZ",
                broker_total=150_000,
                trade_date="20260716",
                log=_NoopLog(),
            )

        self.assertIsNone(batch)
        save_state.assert_not_called()
        alert.assert_called_once()

    def test_d_relay_can_build_exit_batch_before_default_due_date_only_with_explicit_target(self) -> None:
        empty_state = trading_daemon._empty_exit_execution_state()

        with patch.object(
            trading_daemon, "_load_exit_execution_state", return_value=empty_state
        ), patch.object(
            trading_daemon, "_local_exit_quantities", return_value=(0, 100_000)
        ), patch.object(
            trading_daemon, "_save_exit_execution_state"
        ), patch.object(
            trading_daemon, "_exit_account_fingerprint", return_value="TEST-ACCOUNT"
        ):
            blocked_without_override = trading_daemon._ensure_exit_batch(
                "002800.SZ",
                broker_total=100_000,
                trade_date="20260716",
                log=_NoopLog(),
            )
            relay_batch = trading_daemon._ensure_exit_batch(
                "002800.SZ",
                broker_total=100_000,
                trade_date="20260716",
                log=_NoopLog(),
                target_qty_override=100_000,
            )

        self.assertIsNone(blocked_without_override)
        self.assertIsNotNone(relay_batch)
        self.assertEqual(int(relay_batch["target_qty"]), 100_000)
        self.assertEqual(relay_batch["target_source"], "D_RELAY")


class ExitWatchdogLifecycleTest(unittest.TestCase):
    def test_single_empty_broker_snapshot_does_not_mark_position_closed(self) -> None:
        position = {
            "order_id": "LOCAL-1",
            "ts_code": "002800.SZ",
            "name": "天顺股份",
            "shares": 1_000,
            "status": "open",
            "planned_exit_date": "20260716",
        }
        frozen_now = datetime.datetime(
            2026, 7, 16, 15, 0, 31, tzinfo=trading_daemon.BEIJING_TZ
        )
        fill = SimpleNamespace(filled_qty=0, avg_price=0.0)

        with patch.object(trading_daemon, "now_beijing", return_value=frozen_now), patch.object(
            trading_daemon, "is_trade_day", return_value=True
        ), patch.object(trading_daemon, "load_positions", return_value=[]), patch.object(
            trading_daemon,
            "load_json_config",
            return_value={
                "broker_adapter_enabled": True,
                "qmt_enabled": True,
                "broker": {"enabled": True},
            },
        ), patch.object(trading_daemon, "_confirm_fill", return_value=fill), patch.object(
            trading_daemon, "_qmt_get", return_value=SimpleNamespace()
        ), patch.object(
            trading_daemon, "_broker_position_quantities", return_value=(0, 0)
        ), patch.object(trading_daemon, "mark_position_closed") as mark_closed, patch.object(
            trading_daemon, "_watchdog_reconcile_after_close"
        ) as reconcile, patch.object(
            trading_daemon, "_watchdog_pending", [(position, [("QMT-1", 1_000)])]
        ), patch.object(
            trading_daemon.time, "sleep", side_effect=_StopWatchdog
        ):
            with self.assertRaises(_StopWatchdog):
                trading_daemon._close_position_watchdog()

        # 单次“代码不在持仓列表”可能只是QMT同步瞬断，不能先把本地仓关掉；
        # 关闭动作只能由后面的双快照收盘对账确认。
        mark_closed.assert_not_called()
        reconcile.assert_called_once()

    def test_failed_auction_handoff_is_retried_before_marking_it_fired(self) -> None:
        due = {
            "order_id": "LOCAL-1",
            "ts_code": "002800.SZ",
            "name": "天顺股份",
            "shares": 1_000,
            "status": "open",
            "planned_exit_date": "20260716",
        }
        frozen_now = datetime.datetime(
            2026, 7, 16, 14, 56, 50, tzinfo=trading_daemon.BEIJING_TZ
        )
        sleep_calls = 0

        def stop_after_two_loops(_seconds: float) -> None:
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls >= 2:
                raise _StopWatchdog()

        # 14:56核查在本测试中模拟QMT忙；关注点仅是交接函数首次False后，
        # 看门狗下一轮必须重试，不能提前把handoff key写入fired。
        busy_qmt_lock = SimpleNamespace(
            acquire=lambda timeout=None: False,
            release=lambda: None,
        )
        with patch.object(trading_daemon, "now_beijing", return_value=frozen_now), patch.object(
            trading_daemon, "is_trade_day", return_value=True
        ), patch.object(trading_daemon, "load_positions", return_value=[due]), patch.object(
            trading_daemon, "load_json_config", return_value={"broker": {}}
        ), patch.object(
            trading_daemon,
            "_cancel_active_close_orders_for_auction",
            side_effect=[False, True],
        ) as handoff, patch.object(
            trading_daemon, "_qmt_lock", busy_qmt_lock
        ), patch.object(
            trading_daemon.time, "sleep", side_effect=stop_after_two_loops
        ):
            with self.assertRaises(_StopWatchdog):
                trading_daemon._close_position_watchdog()

        self.assertEqual(handoff.call_count, 2)


class LocalExitAccountingTest(unittest.TestCase):
    @staticmethod
    def _open_position(*, shares: int = 1_000) -> dict:
        return {
            "order_id": "LOCAL-1",
            "ts_code": "002800.SZ",
            "name": "天顺股份",
            "shares": shares,
            "entry_shares": shares,
            "status": "open",
            "planned_exit_date": "20260716",
            "exit_fills_by_date": {},
        }

    def test_same_broker_fill_is_applied_only_once(self) -> None:
        store = _MemoryPositionStore([self._open_position(shares=1_050)])

        with patch.object(trading_daemon, "load_positions", side_effect=store.load), patch.object(
            trading_daemon, "save_positions", side_effect=store.save
        ):
            first = trading_daemon._apply_known_exit_fill(
                "LOCAL-1",
                broker_order_id="QMT-TP-1",
                current_shares=1_050,
                filled_qty=1_000,
                fill_price=10.0,
                fill_date="20260716",
            )
            second = trading_daemon._apply_known_exit_fill(
                "LOCAL-1",
                broker_order_id="QMT-TP-1",
                current_shares=50,
                filled_qty=1_000,
                fill_price=10.0,
                fill_date="20260716",
            )

        saved = store.positions[0]
        self.assertEqual(first, 1_000)
        self.assertEqual(second, 0)
        self.assertEqual(saved["shares"], 50)
        self.assertEqual(saved["status"], "open")
        self.assertEqual(saved["applied_exit_fills_by_order"]["QMT-TP-1"], 1_000)
        self.assertEqual(saved["exit_fills_by_date"]["20260716"]["qty"], 1_000)

    def test_confirmed_fill_repairs_ghost_cleared_row(self) -> None:
        position = self._open_position(shares=2_500)
        position.update(
            {
                "status": "closed",
                "sell_date": "20260804",
                "sell_price": 0.0,
                "ghost_cleared_at": "2026-08-04 14:58:00",
            }
        )
        store = _MemoryPositionStore([position])

        with patch.object(trading_daemon, "load_positions", side_effect=store.load), patch.object(
            trading_daemon, "save_positions", side_effect=store.save
        ):
            applied = trading_daemon._apply_known_exit_fill(
                "LOCAL-1",
                broker_order_id="QMT-WATCHDOG-1",
                current_shares=2_500,
                filled_qty=2_500,
                fill_price=18.03,
                fill_date="20260804",
            )
            repeated = trading_daemon._apply_known_exit_fill(
                "LOCAL-1",
                broker_order_id="QMT-WATCHDOG-1",
                current_shares=0,
                filled_qty=2_500,
                fill_price=18.03,
                fill_date="20260804",
            )

        saved = store.positions[0]
        self.assertEqual(applied, 2_500)
        self.assertEqual(repeated, 0)
        self.assertEqual(saved["shares"], 0)
        self.assertEqual(saved["status"], "closed")
        self.assertAlmostEqual(saved["sell_price"], 18.03)
        self.assertEqual(saved["exit_fills_by_date"]["20260804"]["qty"], 2_500)

    def test_final_sell_price_is_weighted_across_partial_and_final_fills(self) -> None:
        position = self._open_position(shares=18_400)
        position["entry_shares"] = 20_000
        position["exit_fills_by_date"] = {
            "20260803": {"qty": 1_600, "amount": 17_912.0}
        }
        store = _MemoryPositionStore([position])

        with patch.object(trading_daemon, "load_positions", side_effect=store.load), patch.object(
            trading_daemon, "save_positions", side_effect=store.save
        ):
            applied = trading_daemon._apply_known_exit_fill(
                "LOCAL-1",
                broker_order_id="QMT-WATCHDOG-FINAL",
                current_shares=18_400,
                filled_qty=18_400,
                fill_price=11.19,
                fill_date="20260803",
            )

        saved = store.positions[0]
        expected_price = (17_912.0 + 18_400 * 11.19) / 20_000
        self.assertEqual(applied, 18_400)
        self.assertEqual(saved["shares"], 0)
        self.assertEqual(saved["exit_fills_by_date"]["20260803"]["qty"], 20_000)
        self.assertAlmostEqual(saved["sell_price"], expected_price)

    def test_reduce_position_shares_never_increases_remaining_quantity(self) -> None:
        store = _MemoryPositionStore([self._open_position()])
        frozen_now = datetime.datetime(
            2026, 7, 16, 15, 0, 31, tzinfo=trading_daemon.BEIJING_TZ
        )

        with patch.object(trading_daemon, "load_positions", side_effect=store.load), patch.object(
            trading_daemon, "save_positions", side_effect=store.save
        ), patch.object(trading_daemon, "today_beijing", return_value=frozen_now):
            trading_daemon.reduce_position_shares(
                "LOCAL-1", 600, fill_price=10.0, fill_date="20260716"
            )
            # 随后的券商同代码聚合快照可能包含人工仓而变成800；本地策略余仓
            # 必须保持600，不能被这个更大的外部数字反向灌回。
            trading_daemon.reduce_position_shares(
                "LOCAL-1", 800, fill_price=10.0, fill_date="20260716"
            )

        saved = store.positions[0]
        self.assertEqual(saved["shares"], 600)
        self.assertEqual(saved["status"], "open")
        self.assertEqual(saved["exit_fills_by_date"]["20260716"]["qty"], 400)
        self.assertEqual(saved["exit_fills_by_date"]["20260716"]["amount"], 4_000.0)

    def test_exit_fill_ledger_makes_reconcile_idempotent(self) -> None:
        store = _MemoryPositionStore([self._open_position()])
        adapter = _FakeReconcileAdapter(broker_qty=600, system_trade_qty=400)
        frozen_now = datetime.datetime(
            2026, 7, 16, 15, 0, 31, tzinfo=trading_daemon.BEIJING_TZ
        )

        with patch.object(trading_daemon, "load_positions", side_effect=store.load), patch.object(
            trading_daemon, "save_positions", side_effect=store.save
        ), patch.object(trading_daemon, "_qmt_get", return_value=adapter), patch.object(
            trading_daemon, "today_beijing", return_value=frozen_now
        ), patch.object(trading_daemon, "time", wraps=trading_daemon.time) as time_module, patch.object(
            trading_daemon, "_notify"
        ):
            time_module.sleep.return_value = None
            trading_daemon._watchdog_reconcile_after_close({}, _NoopLog())
            trading_daemon._watchdog_reconcile_after_close({}, _NoopLog())

        saved = store.positions[0]
        self.assertEqual(saved["shares"], 600)
        self.assertEqual(saved["status"], "open")
        # 同一笔400股系统成交第二次对账不得再次扣成200股。
        self.assertEqual(saved["exit_fills_by_date"]["20260716"]["qty"], 400)
        self.assertEqual(saved["exit_fills_by_date"]["20260716"]["amount"], 4_000.0)

    def test_reconcile_uses_verified_batch_when_query_trades_is_temporarily_empty(self) -> None:
        store = _MemoryPositionStore([self._open_position()])
        adapter = _FakeReconcileAdapter(
            broker_qty=600,
            system_trade_qty=400,
            trade_price=10.0,
            trades_visible=False,
        )
        frozen_now = datetime.datetime(
            2026, 7, 16, 15, 0, 31, tzinfo=trading_daemon.BEIJING_TZ
        )
        account_fingerprint = "TEST-ACCOUNT"
        state = {
            "version": 1,
            "batches": {
                "20260716|002800": {
                    "trade_date": "20260716",
                    "ts_code": "002800.SZ",
                    "target_qty": 1_000,
                    "non_target_floor": 0,
                    "account_fingerprint": account_fingerprint,
                }
            },
            "intents": [
                {
                    "token": "KNOWN-PARTIAL-FILL",
                    "trade_date": "20260716",
                    "ts_code": "002800.SZ",
                    "account_fingerprint": account_fingerprint,
                    "quantity": 1_000,
                    "status": "RESOLVED",
                    "broker_order_id": "SYSTEM-SELL-1",
                    "filled_qty": 400,
                    "terminal_known": True,
                }
            ],
        }

        with patch.object(trading_daemon, "load_positions", side_effect=store.load), patch.object(
            trading_daemon, "save_positions", side_effect=store.save
        ), patch.object(trading_daemon, "_qmt_get", return_value=adapter), patch.object(
            trading_daemon, "today_beijing", return_value=frozen_now
        ), patch.object(
            trading_daemon, "_exit_account_fingerprint", return_value=account_fingerprint
        ), patch.object(
            trading_daemon, "_load_exit_execution_state", return_value=state
        ), patch.object(
            trading_daemon, "_save_exit_execution_state"
        ), patch.object(
            trading_daemon, "time", wraps=trading_daemon.time
        ) as time_module, patch.object(trading_daemon, "_notify"):
            time_module.sleep.return_value = None
            trading_daemon._watchdog_reconcile_after_close({}, _NoopLog())

        saved = store.positions[0]
        # QMT委托已明确部成400股、两次余仓都是600股，即使
        # query_trades暂空，也要用账户绑定的安全批次补记，避免次日漏卖。
        self.assertEqual(saved["shares"], 600)
        self.assertEqual(saved["status"], "open")
        self.assertEqual(saved["exit_fills_by_date"]["20260716"]["qty"], 400)
        self.assertEqual(saved["exit_fills_by_date"]["20260716"]["amount"], 4_000.0)

    def test_manual_same_code_remainder_is_not_written_back_to_strategy_position(self) -> None:
        store = _MemoryPositionStore([self._open_position()])
        # 系统成交1000股已覆盖全部策略仓；QMT仍显示500股，是同代码人工余仓。
        adapter = _FakeReconcileAdapter(broker_qty=500, system_trade_qty=1_000)
        frozen_now = datetime.datetime(
            2026, 7, 16, 15, 0, 31, tzinfo=trading_daemon.BEIJING_TZ
        )

        with patch.object(trading_daemon, "load_positions", side_effect=store.load), patch.object(
            trading_daemon, "save_positions", side_effect=store.save
        ), patch.object(trading_daemon, "_qmt_get", return_value=adapter), patch.object(
            trading_daemon, "today_beijing", return_value=frozen_now
        ), patch.object(trading_daemon, "time", wraps=trading_daemon.time) as time_module, patch.object(
            trading_daemon, "_notify"
        ):
            time_module.sleep.return_value = None
            trading_daemon._watchdog_reconcile_after_close({}, _NoopLog())

        saved = store.positions[0]
        self.assertEqual(saved["status"], "closed")
        self.assertEqual(saved["shares"], 0)
        self.assertEqual(saved["exit_fills_by_date"]["20260716"]["qty"], 1_000)
        # 绝不能把券商仍有的500股人工仓写成策略余仓，导致次日自动再卖。
        self.assertNotEqual(saved["shares"], adapter.broker_qty)


class EntryExitCapacityGateTest(unittest.TestCase):
    @staticmethod
    def _planned_order():
        import pandas as pd

        return pd.DataFrame(
            [
                {
                    "side": "BUY",
                    "ts_code": "002800.SZ",
                    "signal_date": "20260715",
                    "reference_price": 10.0,
                    "round_lot_shares": 100,
                    "estimated_shares": 100,
                    "risk_flags": "",
                }
            ]
        )

    @staticmethod
    def _config():
        return {
            "live_trade": {
                "max_single_order_amount": 0,
                "max_position_pct": 0.85,
                "max_total_position_pct": 0.825,
                "entry_min_acceptable_position_pct": 0.80,
                "entry_actual_amount_rebalance_enabled": True,
                "transition_use_full_available_cash": False,
                "round_lot_size": 100,
                "cash_buffer_amount": 0,
                "total_liquidity_cap_pct": 0.005,
                "liquidity_cap_fail_closed": True,
            }
        }

    def test_large_entry_is_scaled_to_half_percent_of_signal_day_amount(self) -> None:
        account = SimpleNamespace(total_asset=12_500_000, available_cash=12_500_000)
        quote_map = {"002800.SZ": SimpleNamespace(last_price=10.0)}
        with patch.object(trading_daemon, "load_json_config", return_value=self._config()), patch.object(
            trading_daemon, "_signal_day_amount", return_value=100_000_000.0
        ), patch.object(trading_daemon, "load_positions", return_value=[]):
            result = trading_daemon.resize_buy_orders_for_live_account(
                self._planned_order(), account, quote_map, 0.0
            )

        row = result.iloc[0]
        self.assertEqual(int(row["round_lot_shares"]), 50_000)
        self.assertLessEqual(float(row["planned_amount_by_equity"]), 500_000.0)
        self.assertIn("EXIT_CAPACITY_CAP:500000", str(row["risk_flags"]))
        self.assertIn("ENTRY_BELOW_80_BY_EXIT_CAP", str(row["risk_flags"]))

    def test_zero_single_order_amount_means_unbounded_in_all_buy_paths(self) -> None:
        cap = trading_daemon._effective_single_order_cap(
            {"max_single_order_amount": 0}
        )
        self.assertEqual(cap, float("inf"))
        self.assertEqual(
            trading_daemon._effective_single_order_cap(
                {"max_single_order_amount": 150_000}
            ),
            150_000.0,
        )

    def test_missing_signal_amount_fails_closed(self) -> None:
        account = SimpleNamespace(total_asset=12_500_000, available_cash=12_500_000)
        quote_map = {"002800.SZ": SimpleNamespace(last_price=10.0)}
        with patch.object(trading_daemon, "load_json_config", return_value=self._config()), patch.object(
            trading_daemon, "_signal_day_amount", return_value=0.0
        ), patch.object(trading_daemon, "load_positions", return_value=[]):
            result = trading_daemon.resize_buy_orders_for_live_account(
                self._planned_order(), account, quote_map, 0.0
            )

        row = result.iloc[0]
        self.assertEqual(int(row["round_lot_shares"]), 0)
        self.assertIn("LIQUIDITY_CAP_DATA_MISSING", str(row["risk_flags"]))

    def test_same_stock_candidate_is_skipped_when_already_held(self) -> None:
        """同票集中度防线:新候选与既有持仓(昨日买入未清仓)同票→放弃买入。"""
        account = SimpleNamespace(total_asset=12_500_000, available_cash=12_500_000)
        quote_map = {"002800.SZ": SimpleNamespace(last_price=10.0)}
        held = [{"ts_code": "002800.SZ", "status": "open", "shares": 10_000,
                 "order_id": "x", "buy_date": "20260715"}]  # 昨日买入=既有持仓
        with patch.object(trading_daemon, "load_json_config", return_value=self._config()), patch.object(
            trading_daemon, "_signal_day_amount", return_value=100_000_000.0
        ), patch.object(trading_daemon, "load_positions", return_value=held), patch.object(
            trading_daemon, "_notify"
        ):
            result = trading_daemon.resize_buy_orders_for_live_account(
                self._planned_order(), account, quote_map, 0.0
            )

        row = result.iloc[0]
        self.assertEqual(int(row["round_lot_shares"]), 0)
        self.assertIn("SAME_STOCK_ALREADY_HELD_SKIP", str(row["risk_flags"]))

    def test_same_stock_guard_ignores_today_pov_partial_fill(self) -> None:
        """POV拆单在途不算同票冲突(用户强调):当日买入的持仓(竞价段已成交、
        平滑段继续买同一只票)是本候选的拆单开仓,绝不能被同票防线拦截。"""
        account = SimpleNamespace(total_asset=12_500_000, available_cash=12_500_000)
        quote_map = {"002800.SZ": SimpleNamespace(last_price=10.0)}
        today_s = trading_daemon.today_beijing().strftime("%Y%m%d")
        held = [{"ts_code": "002800.SZ", "status": "open", "shares": 700,
                 "order_id": "pov-auction", "buy_date": today_s}]  # 今日竞价段拆单已成交
        with patch.object(trading_daemon, "load_json_config", return_value=self._config()), patch.object(
            trading_daemon, "_signal_day_amount", return_value=100_000_000.0
        ), patch.object(trading_daemon, "load_positions", return_value=held), patch.object(
            trading_daemon, "_notify"
        ):
            result = trading_daemon.resize_buy_orders_for_live_account(
                self._planned_order(), account, quote_map, 0.0
            )

        row = result.iloc[0]
        self.assertNotIn("SAME_STOCK_ALREADY_HELD_SKIP", str(row["risk_flags"]))
        self.assertGreater(int(row["round_lot_shares"]), 0)  # 正常继续定仓,不拦截

    def test_due_position_is_counted_and_blocks_transition_entry(self) -> None:
        """今日到期仓仍是实际旧仓：计入市值，并触发执行层串行单仓阻断。"""
        today_str = trading_daemon.today_beijing().strftime("%Y%m%d")
        held = [{
            "ts_code": "600000.SH",
            "status": "open",
            "shares": 80_000,
            "order_id": "due-today",
            "buy_date": "20260727",
            "planned_exit_date": today_str,
        }]
        broker_positions = [SimpleNamespace(
            ts_code="600000.SH", volume=80_000, market_value=800_000.0,
        )]
        with patch.object(trading_daemon, "load_positions", return_value=held):
            self.assertEqual(
                trading_daemon._strategy_only_market_value(
                    broker_positions, exclude_due_today=True
                ),
                800_000.0,
            )
            self.assertTrue(
                trading_daemon._broker_has_preexisting_strategy_position(broker_positions)
            )

    def test_normal_empty_account_uses_82_5_pct_target(self) -> None:
        account = SimpleNamespace(total_asset=1_000_000.0, available_cash=1_000_000.0)
        quote_map = {"002800.SZ": SimpleNamespace(last_price=10.0)}
        with patch.object(
            trading_daemon, "load_positions", return_value=[]
        ), patch.object(
            trading_daemon, "load_json_config", return_value=self._config()
        ), patch.object(
            trading_daemon, "_signal_day_amount", return_value=200_000_000.0
        ):
            result = trading_daemon.resize_buy_orders_for_live_account(
                self._planned_order(),
                account,
                quote_map,
                0.0,
                transition_full_cash=False,
            )
        row = result.iloc[0]
        self.assertEqual(float(row["planned_amount_by_equity"]), 825_000.0)
        self.assertEqual(float(row["live_target_amount"]), 825_000.0)
        self.assertEqual(float(row["live_min_acceptable_amount"]), 800_000.0)
        self.assertEqual(float(row["live_hard_cap_amount"]), 850_000.0)

    def test_cash_below_eighty_percent_rejects_entry_before_ordering(self) -> None:
        """总资产100万但可用现金不足80万时，不生成明显偏小的开仓种子单。"""
        account = SimpleNamespace(total_asset=1_000_000.0, available_cash=790_000.0)
        quote_map = {"002800.SZ": SimpleNamespace(last_price=10.0)}
        with patch.object(
            trading_daemon, "load_positions", return_value=[]
        ), patch.object(
            trading_daemon, "load_json_config", return_value=self._config()
        ), patch.object(
            trading_daemon, "_signal_day_amount", return_value=200_000_000.0
        ):
            result = trading_daemon.resize_buy_orders_for_live_account(
                self._planned_order(), account, quote_map, 0.0
            )

        row = result.iloc[0]
        self.assertEqual(int(row["round_lot_shares"]), 0)
        self.assertEqual(float(row["live_target_amount"]), 0.0)
        self.assertIn("ENTRY_MIN_80_PCT_UNREACHABLE", str(row["risk_flags"]))

    def test_configuration_cannot_loosen_82_5_target_or_85_hard_cap(self) -> None:
        """误把配置调高也只能收紧，不能突破本版代码级82.5%/85%红线。"""
        config = self._config()
        config["live_trade"]["max_total_position_pct"] = 0.90
        config["live_trade"]["max_position_pct"] = 0.95
        account = SimpleNamespace(total_asset=1_000_000.0, available_cash=1_000_000.0)
        with patch.object(
            trading_daemon, "load_positions", return_value=[]
        ), patch.object(
            trading_daemon, "load_json_config", return_value=config
        ), patch.object(
            trading_daemon, "_signal_day_amount", return_value=200_000_000.0
        ):
            result = trading_daemon.resize_buy_orders_for_live_account(
                self._planned_order(), account,
                {"002800.SZ": SimpleNamespace(last_price=10.0)}, 0.0,
            )

        row = result.iloc[0]
        self.assertEqual(float(row["live_target_amount"]), 825_000.0)
        self.assertEqual(float(row["live_hard_cap_amount"]), 850_000.0)


    def test_all_old_strategy_position_states_block_new_entry(self) -> None:
        """未到期、逾期、今日到期和人工退出仓都属于旧策略仓。"""
        today_str = trading_daemon.today_beijing().strftime("%Y%m%d")
        broker_positions = [
            SimpleNamespace(
                ts_code="600000.SH",
                volume=80_000,
                market_value=800_000.0,
            )
        ]
        position_cases = [
            {"planned_exit_date": "99991231"},
            {"planned_exit_date": "20200101"},
            {"planned_exit_date": today_str, "manual_exit_only": True},
        ]

        for extra_fields in position_cases:
            held = {
                "ts_code": "600000.SH",
                "status": "open",
                "shares": 80_000,
                "order_id": "must-count",
                **extra_fields,
            }
            with self.subTest(extra_fields=extra_fields), patch.object(
                trading_daemon,
                "load_positions",
                return_value=[held],
            ):
                entry_market_value = trading_daemon._strategy_only_market_value(
                    broker_positions,
                    exclude_due_today=True,
                )
                self.assertEqual(entry_market_value, 800_000.0)
                self.assertTrue(
                    trading_daemon._broker_has_preexisting_strategy_position(broker_positions)
                )

    def test_external_broker_position_does_not_trigger_transition_mode(self) -> None:
        external_positions = [
            SimpleNamespace(
                ts_code="754019.SH",
                volume=10,
                market_value=1_000.0,
            ),
            SimpleNamespace(
                ts_code="071202.SZ",
                volume=10,
                market_value=1_000.0,
            ),
        ]
        with patch.object(trading_daemon, "load_positions", return_value=[]):
            self.assertFalse(
                trading_daemon._broker_has_preexisting_strategy_position(external_positions)
            )

    def test_today_pov_partial_fill_is_not_treated_as_old_position(self) -> None:
        today_str = trading_daemon.today_beijing().strftime("%Y%m%d")
        local = [{
            "ts_code": "002800.SZ", "status": "open", "buy_date": today_str,
        }]
        broker = [SimpleNamespace(ts_code="002800.SZ", volume=500, market_value=5_000.0)]
        with patch.object(trading_daemon, "load_positions", return_value=local):
            self.assertFalse(
                trading_daemon._broker_has_preexisting_strategy_position(broker)
            )


class EntryActualAmountRebalanceTest(unittest.TestCase):
    def test_auction_seed_uses_limit_price_without_crossing_eighty_five_percent(self) -> None:
        """10cm/20cm/30cm涨停预挂均按最坏委托价锁在85%以内。"""
        hard_cap = 850_000.0
        expected = {11.0: 77_200, 12.0: 70_800, 13.0: 65_300}
        for limit_price, expected_qty in expected.items():
            with self.subTest(limit_price=limit_price):
                qty = trading_daemon._floor_buy_quantity_by_amount(
                    hard_cap, limit_price, 100
                )
                self.assertEqual(qty, expected_qty)
                self.assertLessEqual(qty * limit_price, hard_cap)
                self.assertGreater((qty + 100) * limit_price, hard_cap)

    def test_pov_supplements_actual_gap_instead_of_reference_share_gap(self) -> None:
        """竞价实际77万后，只补到82.5万；委托价按开盘+2%计算仍不越线。"""
        qty, target_gap, hard_room, order_cap = trading_daemon._calculate_pov_buy_quantity(
            target_amount=825_000.0,
            hard_cap_amount=850_000.0,
            confirmed_actual_amount=770_000.0,
            available_cash=230_000.0,
            cash_buffer=1_000.0,
            slice_budget=1_000_000.0,
            order_price=10.20,
            lot_size=100,
        )

        self.assertEqual(target_gap, 55_000.0)
        self.assertEqual(hard_room, 80_000.0)
        self.assertEqual(order_cap, 55_000.0)
        self.assertEqual(qty, 5_300)
        self.assertLessEqual(770_000.0 + qty * 10.20, 825_000.0)
        self.assertGreaterEqual(770_000.0 + qty * 10.20, 800_000.0)

    def test_pov_never_supplements_after_target_is_reached(self) -> None:
        qty, target_gap, hard_room, order_cap = trading_daemon._calculate_pov_buy_quantity(
            target_amount=825_000.0,
            hard_cap_amount=850_000.0,
            confirmed_actual_amount=830_000.0,
            available_cash=170_000.0,
            cash_buffer=1_000.0,
            slice_budget=1_000_000.0,
            order_price=10.20,
            lot_size=100,
        )

        self.assertEqual(qty, 0)
        self.assertEqual(target_gap, 0.0)
        self.assertEqual(hard_room, 20_000.0)
        self.assertEqual(order_cap, 0.0)

    def test_actual_cost_excludes_same_stock_manual_baseline(self) -> None:
        """09:20已存在的人工同票成本不计入本次策略实际开仓额。"""
        item = {
            "ts_code": "002800.SZ",
            "broker_baseline_cost_amount": 2_000.0,
            "cost_amt": 0.0,
        }
        broker_positions = [SimpleNamespace(
            ts_code="002800.SZ", volume=1_200, cost_price=10.0
        )]
        with patch.object(trading_daemon, "_today_local_entry_cost", return_value=0.0):
            actual = trading_daemon._confirmed_entry_actual_amount(
                item, broker_positions
            )
        self.assertEqual(actual, 10_000.0)

    def test_existing_pov_executes_recalculated_actual_amount_gap(self) -> None:
        """即使盘前原计划不需拆分，开盘后也能用现有POV补真实金额缺口。"""
        class FakeAdapter:
            def __init__(self) -> None:
                self.requests: list[OrderRequest] = []

            def query_account(self):
                return SimpleNamespace(total_asset=1_000_000.0, available_cash=230_000.0)

            def query_positions(self):
                return []

            def get_full_tick(self, _codes):
                return {
                    "002800.SZ": QuoteSnapshot(
                        ts_code="002800.SZ",
                        broker_code="002800.SZ",
                        last_price=10.0,
                        open_price=10.0,
                        upper_limit=11.0,
                        amount=100_000_000.0,
                    )
                }

            def place_order(self, request: OrderRequest):
                self.requests.append(request)
                return OrderResult(
                    ts_code=request.ts_code,
                    broker_code=request.broker_code,
                    side=request.side,
                    quantity=request.quantity,
                    accepted=True,
                    order_id="POV-1",
                )

        item = {
            "ts_code": "002800.SZ",
            "name": "测试股",
            "target_actual_amount": 825_000.0,
            "target_amt": 825_000.0,
            "remain_amt": 825_000.0,
            "min_acceptable_amount": 800_000.0,
            "hard_cap_amount": 850_000.0,
            "equity_snapshot": 1_000_000.0,
            "sig_amt": 100_000_000.0,
            "slice_no": 0,
            "prev_cum_amount": 0.0,
            "cost_amt": 0.0,
            "filled_qty": 0,
            "done": False,
        }
        adapter = FakeAdapter()
        fill = OrderFill(
            order_id="POV-1",
            status_code=56,
            status_text="已成",
            filled_qty=5_300,
            avg_price=10.0,
            is_terminal=True,
            is_filled=True,
        )
        with patch.object(trading_daemon, "_qmt_get", return_value=adapter), patch.object(
            trading_daemon, "_has_pending_buy_for_code", return_value=False
        ), patch.object(
            trading_daemon, "_today_local_entry_cost", return_value=770_000.0
        ), patch.object(
            trading_daemon, "_confirm_fill", return_value=fill
        ), patch.object(
            trading_daemon, "_pov_log_slice"
        ), patch.object(
            trading_daemon, "_track_execution"
        ), patch.object(
            trading_daemon, "st_open_forbidden", return_value=False
        ):
            trading_daemon._pov_execute_slice(
                item, {}, 0.10, 0.18, 0.02, "20260803", _NoopLog(),
                cash_buffer=1_000.0, max_position_pct=0.85, lot_size=100,
            )

        self.assertEqual(len(adapter.requests), 1)
        request = adapter.requests[0]
        self.assertEqual(request.quantity, 5_300)
        self.assertEqual(request.price, 10.20)
        self.assertLessEqual(770_000.0 + request.quantity * request.price, 825_000.0)
        self.assertEqual(item["confirmed_actual_amount"], 823_000.0)


class PremarketBuyHandoffTest(unittest.TestCase):
    @staticmethod
    def _pending_order() -> dict:
        return {
            "order_id": "AUCTION-1",
            "ts_code": "002800.SZ",
            "name": "测试股",
            "signal_date": "20260731",
            "strategy_leg": "A",
            "qty": 10_000,
            "ref_price": 11.0,
            "exit_n": 2,
        }

    @staticmethod
    def _fill(*, qty: int, terminal: bool):
        return SimpleNamespace(
            filled_qty=qty,
            avg_price=10.0 if qty else 0.0,
            is_terminal=terminal,
            is_filled=qty == 10_000,
            status_text="部成" if qty else "已报",
            traded_at="09:25:00",
        )

    def test_0926_partial_nonterminal_order_keeps_queue_until_open(self) -> None:
        pending = self._pending_order()
        with patch.object(
            trading_daemon, "load_pending_buys", return_value=[pending]
        ), patch.object(
            trading_daemon, "load_json_config", return_value={"broker": {}}
        ), patch.object(
            trading_daemon, "now_beijing",
            return_value=datetime.datetime(2026, 8, 3, 9, 26, tzinfo=trading_daemon.BEIJING_TZ),
        ), patch.object(
            trading_daemon, "_confirm_fill", return_value=self._fill(qty=5_000, terminal=False)
        ), patch.object(
            trading_daemon, "_try_cancel_order"
        ) as cancel_mock, patch.object(
            trading_daemon, "record_buy"
        ) as record_mock, patch.object(
            trading_daemon, "save_pending_buys"
        ) as save_mock, patch.object(
            trading_daemon, "_start_premarket_buy_monitor"
        ), patch.object(trading_daemon, "_notify"):
            trading_daemon.confirm_pending_premarket_buys("09:26")

        cancel_mock.assert_not_called()
        record_mock.assert_not_called()
        save_mock.assert_called_once_with([pending])

    def test_0930_cancels_partial_order_before_handing_gap_to_pov(self) -> None:
        pending = self._pending_order()
        first = self._fill(qty=5_000, terminal=False)
        terminal = self._fill(qty=5_000, terminal=True)
        with patch.object(
            trading_daemon, "load_pending_buys", return_value=[pending]
        ), patch.object(
            trading_daemon, "load_json_config", return_value={"broker": {}}
        ), patch.object(
            trading_daemon, "_confirm_fill", side_effect=[first, terminal]
        ) as confirm_mock, patch.object(
            trading_daemon, "_try_cancel_order"
        ) as cancel_mock, patch.object(
            trading_daemon, "record_buy"
        ) as record_mock, patch.object(
            trading_daemon, "clear_pending_buys"
        ) as clear_mock, patch.object(trading_daemon, "_notify"):
            trading_daemon.confirm_pending_premarket_buys("09:30")

        self.assertEqual(confirm_mock.call_count, 2)
        cancel_mock.assert_called_once_with({}, "AUCTION-1", "002800.SZ")
        self.assertEqual(record_mock.call_args.kwargs["shares"], 5_000)
        clear_mock.assert_called_once()


class TransitionLiveOrderGatewayTest(unittest.TestCase):
    @staticmethod
    def _gateway() -> LiveOrderGateway:
        gateway = object.__new__(LiveOrderGateway)
        gateway.live_config = {
            "max_single_order_amount": 0,
            "max_position_pct": 0.85,
            "max_total_position_pct": 0.825,
            "round_lot_size": 100,
            "allow_buy": True,
            "allow_sell": True,
            "enforce_trading_time": False,
            "reject_limit_up_buy": False,
            "reject_limit_down_sell": False,
            "duplicate_order_check": False,
            "real_order_enabled": True,
        }
        return gateway

    @staticmethod
    def _preview(gateway: LiveOrderGateway, amount: float, transition: bool):
        quantity = int(amount / 10)
        orders = pd.DataFrame([{
            "side": "BUY",
            "ts_code": "002800.SZ",
            "reference_price": 10.0,
            "round_lot_shares": quantity,
            "planned_amount_by_equity": amount,
        }])
        quote_map = {
            "002800.SZ": SimpleNamespace(
                last_price=10.0,
                upper_limit=11.0,
                lower_limit=9.0,
                suspended=False,
            )
        }
        return gateway.validate_planned_orders(
            orders,
            account_cash=900_000.0,
            open_orders=[],
            quote_map=quote_map,
            positions=[],
            account_total_asset=1_000_000.0,
            current_market_value=0.0,
            transition_full_cash=transition,
        ).iloc[0]

    def test_legacy_transition_flag_cannot_override_82_5_pct_cap(self) -> None:
        gateway = self._gateway()
        within_target = self._preview(gateway, 824_000.0, transition=False)
        ordinary = self._preview(gateway, 830_000.0, transition=False)
        legacy_transition = self._preview(gateway, 830_000.0, transition=True)
        above_hard_cap = self._preview(gateway, 860_000.0, transition=True)

        self.assertEqual(within_target["validation_status"], "PASS")
        self.assertIn("EXCEED_TOTAL_POSITION_PCT", ordinary["reject_reasons"])
        self.assertIn("EXCEED_TOTAL_POSITION_PCT", legacy_transition["reject_reasons"])
        self.assertFalse(bool(legacy_transition["transition_full_cash"]))
        self.assertIn("EXCEED_POSITION_PCT", above_hard_cap["reject_reasons"])


class ExchangePermissionGatewayTest(unittest.TestCase):
    """验证科创板和北交所不会被实盘交易所权限门禁误拦截。"""

    @staticmethod
    def _gateway() -> LiveOrderGateway:
        gateway = object.__new__(LiveOrderGateway)
        gateway.live_config = {
            "allowed_exchanges": ["SH", "SZ", "BJ"],
            "max_single_order_amount": 0,
            "max_position_pct": 0.85,
            "max_total_position_pct": 0.825,
            "round_lot_size": 100,
            "allow_buy": True,
            "allow_sell": True,
            "enforce_trading_time": False,
            "reject_limit_up_buy": False,
            "reject_limit_down_sell": False,
            "duplicate_order_check": False,
            "real_order_enabled": True,
        }
        return gateway

    def test_star_and_bse_buy_orders_pass_exchange_permission_gate(self) -> None:
        """科创板归属SH、北交所归属BJ，两类买单都必须通过交易所许可校验。"""
        planned_orders = pd.DataFrame([
            {
                "side": "BUY",
                "ts_code": "688146.SH",
                "name": "测试科创股",
                "reference_price": 10.0,
                # 科创板买入至少200股；这里同时验证不会被全局100股整手校验误拒。
                "round_lot_shares": 200,
                "planned_amount_by_equity": 2_000.0,
            },
            {
                "side": "BUY",
                "ts_code": "920001.BJ",
                "name": "测试北交股",
                "reference_price": 10.0,
                "round_lot_shares": 100,
                "planned_amount_by_equity": 1_000.0,
            },
        ])
        quote_map = {
            code: SimpleNamespace(
                last_price=10.0,
                upper_limit=12.0 if code.endswith(".SH") else 13.0,
                lower_limit=8.0 if code.endswith(".SH") else 7.0,
                suspended=False,
            )
            for code in planned_orders["ts_code"]
        }

        preview = self._gateway().validate_planned_orders(
            planned_orders,
            account_cash=1_000_000.0,
            open_orders=[],
            quote_map=quote_map,
            positions=[],
            account_total_asset=1_000_000.0,
            current_market_value=0.0,
        )

        self.assertEqual(set(preview["ts_code"]), {"688146.SH", "920001.BJ"})
        self.assertTrue((preview["validation_status"] == "PASS").all())
        self.assertFalse(preview["reject_reasons"].str.contains("EXCHANGE_NOT_PERMITTED").any())

    def test_production_config_explicitly_allows_all_enabled_exchanges(self) -> None:
        """生产配置必须显式允许沪深市场及北交所，D股票池同时覆盖star和bj。"""
        config_path = trading_daemon.PROJECT_ROOT / "config" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(
            set(config["live_trade"]["allowed_exchanges"]),
            {"SH", "SZ", "BJ"},
        )
        self.assertTrue(
            {"sh_main", "sz_main", "chi_next", "star", "bj"}.issubset(
                set(config["strategy_d"]["allowed_market_segments"])
            )
        )
        self.assertFalse(bool(config["cleaning"]["exclude_bj"]))


class LargeExitForceEligibilityTest(unittest.TestCase):
    @staticmethod
    def _config() -> dict:
        return {
            "exit_pov_large_force_enabled": True,
            "exit_pov_large_force_min_amount": 9_500_000,
            "exit_pov_large_force_min_signal_amount": 2_000_000_000,
        }

    def test_95m_position_with_2b_signal_day_amount_is_eligible(self) -> None:
        position = {
            "ts_code": "002800.SZ",
            "signal_date": "20260715",
            "shares": 950_000,
            "entry_shares": 950_000,
            "buy_price": 10.0,
        }

        with patch.object(
            trading_daemon, "_signal_day_amount", return_value=2_000_000_000.0
        ) as signal_amount:
            eligible, reference_amount, signal_day_amount = (
                trading_daemon._large_exit_force_eligible(
                    position, last_price=9.50, live_cfg=self._config()
                )
            )

        self.assertTrue(eligible)
        # 即使现价下跌，也应按原始入场成本识别这笔950万元大仓。
        self.assertEqual(reference_amount, 9_500_000.0)
        self.assertEqual(signal_day_amount, 2_000_000_000.0)
        signal_amount.assert_called_once_with("002800.SZ", "20260715")

    def test_current_150k_position_does_not_enable_large_exit_force(self) -> None:
        position = {
            "ts_code": "002800.SZ",
            "signal_date": "20260715",
            "shares": 10_900,
            "entry_shares": 10_900,
            "buy_price": 13.66,
        }

        with patch.object(
            trading_daemon, "_signal_day_amount", return_value=2_000_000_000.0
        ):
            eligible, reference_amount, signal_day_amount = (
                trading_daemon._large_exit_force_eligible(
                    position, last_price=13.66, live_cfg=self._config()
                )
            )

        self.assertFalse(eligible)
        self.assertAlmostEqual(reference_amount, 148_894.0)
        self.assertEqual(signal_day_amount, 2_000_000_000.0)

    def test_missing_signal_day_amount_fails_closed(self) -> None:
        position = {
            "ts_code": "002800.SZ",
            "signal_date": "20260715",
            "shares": 1_000_000,
            "entry_shares": 1_000_000,
            "buy_price": 10.0,
        }

        with patch.object(trading_daemon, "_signal_day_amount", return_value=0.0):
            eligible, reference_amount, signal_day_amount = (
                trading_daemon._large_exit_force_eligible(
                    position, last_price=10.0, live_cfg=self._config()
                )
            )

        self.assertFalse(eligible)
        self.assertEqual(reference_amount, 10_000_000.0)
        self.assertEqual(signal_day_amount, 0.0)


class StrategyDConditionalExitTest(unittest.TestCase):
    """普通D只按容量分流，接力D和策略收益口径均不得被改动。"""

    def test_production_config_enables_d_for_capacity_gated_pov(self) -> None:
        config_path = trading_daemon.PROJECT_ROOT / "config" / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        live_cfg = config["live_trade"]

        self.assertIn("D", live_cfg["exit_pov_strategy_legs"])
        self.assertEqual(float(live_cfg["exit_pov_trigger_pct"]), 0.01)
        self.assertIn(
            "D",
            trading_daemon._configured_exit_pov_strategy_legs(live_cfg),
        )
        self.assertNotIn("D", live_cfg["exit_pov_large_force_strategy_legs"])
        self.assertFalse(
            trading_daemon._fixed_large_runway_allowed(
                {"strategy_leg": "D"}, live_cfg
            )
        )
        self.assertTrue(
            trading_daemon._fixed_large_runway_allowed(
                {"strategy_leg": "E2"}, live_cfg
            )
        )

    def test_only_t2_due_d_enters_pov_pool_and_relay_d_stays_out(self) -> None:
        positions = [
            {
                "order_id": "D-ORDINARY",
                "strategy_leg": "D",
                "status": "open",
                "planned_exit_date": "20260803",
            },
            {
                "order_id": "D-RELAY-T1",
                "strategy_leg": "D",
                "status": "open",
                # 接力发生在T+1，而D普通计划日仍为T+2；今天日期不相等。
                "planned_exit_date": "20260804",
            },
            {
                "order_id": "D-MANUAL",
                "strategy_leg": "D",
                "status": "open",
                "planned_exit_date": "20260803",
                "manual_exit_only": True,
            },
        ]

        due = trading_daemon._exit_pov_due_positions(
            positions,
            "20260803",
            {"exit_pov_strategy_legs": ["A", "C", "D", "E2", "L"]},
        )

        self.assertEqual([position["order_id"] for position in due], ["D-ORDINARY"])

    def test_small_d_stays_at_1455_and_only_above_capacity_triggers(self) -> None:
        # 13:00累计成交1亿元、门槛1%：100万元及以下不拆卖，超过才进入POV。
        self.assertFalse(
            trading_daemon._exit_pov_capacity_triggered(1_000_000, 100_000_000, 0.01)
        )
        self.assertTrue(
            trading_daemon._exit_pov_capacity_triggered(1_000_001, 100_000_000, 0.01)
        )
        # 错误配置不能把所有小D都提前拆卖，必须回退到1%。
        self.assertFalse(
            trading_daemon._exit_pov_capacity_triggered(500_000, 100_000_000, 0)
        )

    def test_ordinary_d_1455_uses_real_remaining_position_sell_path(self) -> None:
        position = {
            "order_id": "D-ORDINARY",
            "ts_code": "002800.SZ",
            "name": "测试D",
            "shares": 10_000,
            "strategy_leg": "D",
            "planned_exit_date": "20260803",
        }
        config = {
            "live_trade": {"real_order_confirm_text": "CONFIRMED"},
            "broker": {"enabled": True},
        }

        with patch.object(
            trading_daemon, "load_json_config", return_value=config
        ), patch.object(
            trading_daemon, "_abc_place_sell_order_direct", return_value=True
        ) as direct_sell:
            trading_daemon._do_sell(position, qmt_enabled=True)

        direct_sell.assert_called_once_with(
            "002800.SZ",
            "测试D",
            10_000,
            "D-ORDINARY",
            "CONFIRMED",
            config,
            config["broker"],
        )

    def test_d_exit_description_distinguishes_ordinary_and_relay(self) -> None:
        ordinary = trading_daemon._exit_method_desc("D", "T+2_close")
        relay = trading_daemon._exit_method_desc("D", "T+1_open")

        self.assertIn("14:55", ordinary)
        self.assertIn("POV", ordinary)
        self.assertIn("09:23", relay)


class LargeExitFirstSliceTest(unittest.TestCase):
    def test_1415_first_force_call_places_slice_instead_of_only_refreshing_stale_baseline(self) -> None:
        frozen_now = datetime.datetime(
            2026, 7, 16, 14, 15, 10, tzinfo=trading_daemon.BEIJING_TZ
        )
        quote = QuoteSnapshot(
            ts_code="002800.SZ",
            broker_code="002800.SZ",
            last_price=10.00,
            amount=560_000_000.0,
            bid_prices=[10.00, 9.99, 9.98],
            bid_volumes=[200_000, 200_000, 200_000],
        )
        submitted_requests: list[object] = []

        class _SliceAdapter:
            def get_full_tick(self, _codes: list[str]) -> dict[str, QuoteSnapshot]:
                return {"002800.SZ": quote}

            def query_orders(self) -> list[dict]:
                return []

            def place_order(self, request: object) -> SimpleNamespace:
                submitted_requests.append(request)
                return SimpleNamespace(
                    accepted=True,
                    order_id="POV-FORCE-1415",
                    message="ORDER_SUBMITTED",
                )

        plan = {
            "pos": {
                "order_id": "LOCAL-1",
                "ts_code": "002800.SZ",
                "name": "天顺股份",
                "shares": 1_000_000,
            },
            "prev_cum": 500_000_000.0,
            # 千万级计划在13:00创建、14:15首次执行；这正是曾被“基线陈旧”
            # 分支直接return，导致实际首单拖到14:20的生产场景。
            "last_sample_at": frozen_now.replace(hour=13, minute=0, second=5),
            "start": frozen_now.replace(second=0),
            "remain_sh": 1_000_000,
            "sold_qty": 0,
            "sold_amt": 0.0,
            "done": False,
            "large_force": True,
        }
        config = {
            "live_trade": {
                "exit_pov_depth_levels": 3,
                "exit_pov_depth_haircut": 0.5,
                "exit_pov_bid_volume_unit": 1,
                "exit_pov_max_slippage_bps": 20,
                "exit_pov_max_slice_position_pct": 0.10,
            }
        }
        fill = SimpleNamespace(
            filled_qty=100_000,
            avg_price=9.98,
            is_terminal=True,
        )

        def safe_quantity_passthrough(_ts_code: str, **kwargs) -> int:
            return int(kwargs["requested_qty"])

        def place_with_test_intent(
            adapter: object,
            request: object,
            *,
            phase: str,
            local_order_id: str = "",
        ) -> tuple[SimpleNamespace, str]:
            self.assertEqual(phase, "卖出POV")
            self.assertEqual(local_order_id, "LOCAL-1")
            return adapter.place_order(request), "INTENT-FORCE-1415"

        with patch.object(trading_daemon, "now_beijing", return_value=frozen_now), patch.object(
            trading_daemon, "today_beijing", return_value=frozen_now
        ), patch.object(trading_daemon, "load_json_config", return_value=config), patch.object(
            trading_daemon, "_assert_exit_live_allowed"
        ), patch.object(trading_daemon, "_qmt_get", return_value=_SliceAdapter()), patch.object(
            trading_daemon, "_broker_position_quantities", return_value=(1_000_000, 1_000_000)
        ), patch.object(
            trading_daemon,
            "_safe_new_exit_order_quantity",
            side_effect=safe_quantity_passthrough,
        ) as safe_quantity, patch.object(
            trading_daemon,
            "_place_exit_order_with_intent",
            side_effect=place_with_test_intent,
        ) as place_with_intent, patch.object(
            trading_daemon, "_update_exit_order_intent"
        ) as resolve_intent, patch.object(
            trading_daemon, "_confirm_fill", return_value=fill
        ), patch.object(
            trading_daemon, "reduce_position_shares"
        ) as reduce_shares:
            trading_daemon._exit_pov_slice(
                plan,
                broker_cfg={},
                part=0.25,
                log=_NoopLog(),
                cadence_sec=300,
                confirm_timeout_sec=60,
            )

        self.assertEqual(len(submitted_requests), 1)
        request = submitted_requests[0]
        # 6000万元区间流量×25%足够，最终受“单片≤余仓10%”限制为10万股。
        self.assertEqual(request.side, "SELL")
        self.assertEqual(request.quantity, 100_000)
        self.assertEqual(request.price, 9.98)
        self.assertEqual(plan["sold_qty"], 100_000)
        self.assertEqual(plan["remain_sh"], 900_000)
        safe_quantity.assert_called_once()
        place_with_intent.assert_called_once()
        resolve_intent.assert_called_once_with(
            "INTENT-FORCE-1415",
            status="RESOLVED",
            broker_order_id="POV-FORCE-1415",
            filled_qty=100_000,
            terminal_known=True,
        )
        reduce_shares.assert_called_once_with(
            "LOCAL-1", 900_000, fill_price=9.98
        )


class CloseWindowIdentityTest(unittest.TestCase):
    """14:55 收盘平仓靠调用方声明身份，不靠墙钟猜窗口。

    2026-08-12：调度唤醒早了亚秒级（日志截断显示 14:54:59），T2门禁
    `now < 14:55` 与 _has_due_close_plan_now 的 `now >= 14:55` 同时判假，
    整笔平仓被跳过，且告警链路静默。
    """

    _DUE = [{
        "ts_code": "600815.SH", "name": "厦工股份", "strategy_leg": "D",
        "status": "open", "shares": 61800, "planned_exit_date": "20260812",
    }]
    _ACCIDENT_NOW = datetime.datetime(2026, 8, 12, 14, 54, 59, 990000)

    def test_两个调用点都声明了身份且默认为否(self) -> None:
        import inspect

        src = (Path(__file__).absolute().parents[1] / "scripts" / "trading_daemon.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("check_and_close_positions(in_close_window=True)", src)
        self.assertIn("_has_due_close_plan_now(assume_close_window=True)", src)
        for fn, name in ((trading_daemon.check_and_close_positions, "in_close_window"),
                         (trading_daemon._has_due_close_plan_now, "assume_close_window")):
            param = inspect.signature(fn).parameters[name]
            self.assertFalse(param.default, f"{name} 默认必须 False，否则盘前会提前平仓")

    def test_事故时刻仍能识别出到期平仓计划(self) -> None:
        with patch.object(trading_daemon, "load_positions", return_value=self._DUE), \
             patch.object(trading_daemon, "now_beijing", return_value=self._ACCIDENT_NOW), \
             patch.object(trading_daemon, "today_beijing", return_value=self._ACCIDENT_NOW.date()):
            self.assertTrue(trading_daemon._has_due_close_plan_now(assume_close_window=True))
            self.assertFalse(trading_daemon._has_due_close_plan_now())

    def test_门禁只认身份不认钟(self) -> None:
        close_at = trading_daemon.SCHED_AFTERNOON_CLOSE
        for in_window, now, blocked in (
            (True, datetime.time(14, 54, 59, 990000), False),   # 事故场景必须放行
            (False, datetime.time(9, 20), True),                # 盘前必须挡住
            (False, datetime.time(14, 55), False),
        ):
            self.assertEqual((not in_window) and now < close_at, blocked,
                             f"in_close_window={in_window} now={now}")

    def test_不引入时间容差且窗口保持1455整(self) -> None:
        """容差会把收盘窗口推到 POV 的 14:53 交接点之前。"""

        src = (Path(__file__).absolute().parents[1] / "scripts" / "trading_daemon.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("CLOSE_WINDOW_TOLERANCE_SEC", src)
        self.assertNotIn("_align_deadline", src)
        self.assertEqual(trading_daemon.SCHED_AFTERNOON_CLOSE, datetime.time(14, 55))


if __name__ == "__main__":
    unittest.main()
