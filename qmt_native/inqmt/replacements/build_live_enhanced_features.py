# coding: utf-8
"""QMT-native stand-in for close-pipeline step 5 (enhanced audit features).

Step 5 fetches Tushare moneyflow / top_list / auction data, which cannot be reached from
QMT's built-in Python. Its outputs (auction/open_5m/sector_moneyflow/top_list feature
files) are optional inputs that no A/C/E selection condition references; when they are
absent the strategy code logs "特征文件不存在，跳过" and continues, exactly as here.
"""
import argparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--trade-date')
    parser.add_argument('--input-path')
    parser.add_argument('--max-trade-days')
    args = parser.parse_args()
    print('QMT原生模式：⑤ 增强因子需要 Tushare 资金流/龙虎榜/竞价数据，QMT 内无法获取；'
          '这些特征不参与 A/C/E 选股条件，本步跳过（%s）' % args.trade_date)


if __name__ == '__main__':
    main()
