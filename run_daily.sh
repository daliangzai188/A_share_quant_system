#!/bin/bash
# A_System 每日自动化流水线
# 每个交易日收盘后（15:30）运行，自动更新数据并生成模拟盘信号
# 日志输出到 logs/daily_run_YYYYMMDD.log

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

LOG_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/daily_run_$(date +%Y%m%d).log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log "===== 每日流水线开始 ====="

# 加载环境变量
set -a
source "$PROJECT_ROOT/.env"
set +a

PYTHON="$PROJECT_ROOT/.venv/bin/python"

log "① 采集日线 + 漲停池数据..."
$PYTHON -B scripts/collect_all_data.py >> "$LOG_FILE" 2>&1
log "① 完成"

log "② 清洗合并数据..."
$PYTHON -B scripts/clean_collected_data.py >> "$LOG_FILE" 2>&1
log "② 完成"

log "③ 构建市场情绪 / 题材热度特征..."
$PYTHON -B scripts/build_dynamic_features.py >> "$LOG_FILE" 2>&1
log "③ 完成"

log "④ 更新漲停池成交概率打分..."
$PYTHON -B scripts/score_limit_up_fill_probability.py >> "$LOG_FILE" 2>&1
log "④ 完成"

log "⑤ 更新次日溢价因子..."
$PYTHON -B scripts/analyze_next_day_premium.py >> "$LOG_FILE" 2>&1
log "⑤ 完成"

log "⑥ 生成 A+B+C 每日模拟盘信号..."
$PYTHON -B scripts/run_paper_ab_filtered_daily_ops.py --top-n 10 >> "$LOG_FILE" 2>&1
log "⑥ 完成"

log "===== 每日流水线完成，结果见 reports/paper_trade/ab_filtered_daily_ops/ ====="
