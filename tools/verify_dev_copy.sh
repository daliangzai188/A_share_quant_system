#!/bin/sh
# 发布前的自动检查（2026-11-15 起 tools/publish_dev_copy.sh 每次发布都先跑它；也可以单独跑）。
#
#   sh tools/verify_dev_copy.sh [导出副本路径]      （默认当前目录）
#
# 在导出副本里（不在同步工作区，避免测试打开生产交易意图账本）：
# 1. 全量测试；
# 2. 生产链对照标准：从冻结行情重建研究池，影子账、停手判定、实盘下单、因子健康、M候选单、三级警告、日报、仓位
#    逐字节和 tests/golden/expected 比对（约7分钟）；
# 3. 打印验收指标。
# 全部通过才在 .git/a_system_verified 记下这个提交号；发布同一个提交时不再重跑。只检查已提交的内容：有未提交的改动就停。
set -e
DEV=$(cd "${1:-.}" && pwd)
PY=${A_SYSTEM_PY:-$HOME/A_System_Rebuild/main_venv/bin/python}
cd "$DEV"
dirty=$(git status --porcelain --untracked-files=no | grep -v ' data/' || true)
[ -z "$dirty" ] || { echo "导出副本有未提交的改动，先提交再检查："; echo "$dirty"; exit 1; }
HEAD=$(git rev-parse HEAD)
LOG=$(git rev-parse --git-dir)/a_system_verify.log
: > "$LOG"
export PYTHONDONTWRITEBYTECODE=1

echo "== 1. 全量测试（$(git rev-parse --short HEAD)）"
if ! "$PY" -m pytest -q -p no:cacheprovider tests >> "$LOG" 2>&1; then
  tail -30 "$LOG"; echo "全量测试没通过（完整输出：$LOG）"; exit 1
fi
tail -1 "$LOG"

echo "== 2. 生产链对照标准（逐字节比对）"
if ! "$PY" tests/golden/golden_master.py check >> "$LOG" 2>&1; then
  grep -E "^  (不同|一致)|^RESULT|^        " "$LOG" | tail -40; echo "对照标准不一致（完整输出：$LOG）"; exit 1
fi
grep "^RESULT" "$LOG" | tail -1

echo "== 3. 验收指标"
"$PY" tests/golden/project_metrics.py

echo "$HEAD $(date '+%Y-%m-%d %H:%M:%S')" > "$(git rev-parse --git-dir)/a_system_verified"
echo "检查全部通过：$HEAD"
