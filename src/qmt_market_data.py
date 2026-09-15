"""研究脚本的数据接口选择；内置模式不导入 xtquant。"""
from pathlib import Path
import os

from dotenv import load_dotenv
from qmt_inner.protocol import FileClient, load_settings, read_json

ROOT = Path(__file__).resolve().parents[1]


def selected_transport(config=None):
    load_dotenv(ROOT / '.env', override=False)
    config = config if config is not None else read_json(ROOT / 'config/config.json')
    value = os.getenv('QMT_TRANSPORT', config.get('broker', {}).get('transport', 'miniqmt')).strip().lower()
    if value not in ('miniqmt', 'qmt_inner'):
        raise RuntimeError('Unsupported QMT_TRANSPORT: ' + value)
    return value


class InnerMarketData:
    def __init__(self, settings=None):
        self.client = FileClient(settings or load_settings())
        self.client.connect()

    def download_history_data(self, stock_code, period='1d', start_time='', end_time=''):
        return self.client.call('download_history', dict(code=stock_code, period=period,
                                start_time=start_time, end_time=end_time))

    def get_market_data_ex(self, field_list, stock_list, period='1d', start_time='',
                           end_time='', count=-1, dividend_type='none', fill_data=True):
        import pandas as pd
        payload = self.client.call('history', dict(fields=field_list, codes=stock_list, period=period,
                         start_time=start_time, end_time=end_time, count=count,
                         dividend_type=dividend_type, fill_data=fill_data))
        return {code: pd.DataFrame(data['rows'], index=data['index'], columns=data['columns'])
                for code, data in payload.items()}


def get_market_data_client():
    if selected_transport() == 'qmt_inner':
        from src.qmt_single_owner import assert_standalone_qmt_allowed
        # Bulk research downloads cannot starve the live timer and execution queue.
        assert_standalone_qmt_allowed(ROOT, caller='QMT内置历史研究数据')
        return InnerMarketData()
    from xtquant import xtdata
    return xtdata
