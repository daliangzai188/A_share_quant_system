#!/bin/sh
# QMT原生版部署：Mac 这边一条命令（2026-11-14 项目优化第5步）。
#
#   sh deploy.sh auto                一条命令（2026-11-15）：按主仓库打核心包、生成影子版和实盘版、跑完整回归（约1.5小时），
#                                    再用这份回归日志走下面的正式流程（主仓库的 tools/release.sh --qmt 调的就是它）
#   sh deploy.sh auto --check        同上，但只检查
#   sh deploy.sh 回归日志            正式：检查 → 打核心包 → 生成策略文件 → 旧文件进回滚目录、新文件放进虚拟机目录
#                                    → 核心包提交到本仓库并发布到主仓库 → 打印 QMT 里要做的步骤和回滚办法
#   sh deploy.sh 回归日志 --check    只检查和生成，不提交、不发布、不动虚拟机目录
#
# 回归日志：这次要上线的核心包跑完整回归的输出（sh inqmt/run_regression.sh 影子版 实盘版 目录 > 日志）。
# 必须是同一个核心包（日志第一行 "== core <校验码>"）、各节全部 RESULT PASS、整日演练正常结束且没有异常，
# 否则拒绝部署。完整回归约 1.5 小时，要先跑完再部署。
# QMT 里的粘贴、编译和重启仍要在虚拟机上做（券商平台只能这样换策略），脚本最后打印步骤。
# 不在 ⑬（约15:15）到次日 09:00 之间部署，除非确认重启后会补跑当天流水线。
set -e
cd "$(dirname "$0")"
LOG=${1:?"用法: sh deploy.sh 回归日志 [--check]"}
MODE=${2:-deploy}
REPO=/Users/eass/Desktop/A_System
VMDIR=$REPO/qmt_native_phase0
PY36="arch -x86_64 $PWD/tools/qmt36/bin/python"
[ -f local.env ] && . ./local.env
[ -n "$QMT_ACCOUNT_ID" ] || { echo "local.env 里没有 QMT_ACCOUNT_ID"; exit 2; }
QN_PAPER_CASH=${QN_PAPER_CASH:-100000}
cleanup() { git checkout -q inqmt/qn_core_full.py 2>/dev/null || true; }

echo "== 1. 前提检查"
# 主仓库：代码没有未提交的改动（虚拟机同步来的行情不算）、和 GitHub 一致；本仓库没有未提交的改动
dirty=$(git -C "$REPO" status --porcelain --untracked-files=no | grep -v ' data/' || true)
[ -z "$dirty" ] || { echo "主仓库有未提交的改动："; echo "$dirty"; exit 1; }
git -C "$REPO" fetch -q origin main
[ "$(git -C "$REPO" rev-parse HEAD)" = "$(git -C "$REPO" rev-parse origin/main)" ] || { echo "主仓库和 GitHub 不一致：先推送"; exit 1; }
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "QMT工作仓库有未提交的改动："; git status --short; exit 1; }
echo "主仓库 $(git -C "$REPO" rev-parse --short HEAD)，工作仓库 $(git rev-parse --short HEAD)"
# Keep candidate and formal builds on the same independently verified shadow seed.
# Other seed inputs retain their existing takeover/daily-update policy.
NEW_FRAMEWORK=$(venv/bin/python -c 'import json; print(int(str(json.load(open("/Users/eass/Desktop/A_System/config/config.json")).get("portfolio_framework") or "ace_m").strip().lower() == "ace_m_l"))')
if [ "$NEW_FRAMEWORK" = "1" ] || [ -n "$QN_EQ_SEED_ACE_M_L" ]; then
  QN_EQ_SEED_ACE_M_L=${QN_EQ_SEED_ACE_M_L:-$PWD/eqseed_prod_ace_m_l}
  export QN_EQ_SEED_ACE_M_L
fi

if [ "$LOG" = "auto" ]; then
  echo "== 1b. 打核心包、生成影子版和实盘版、完整回归（约1.5小时）"
  venv/bin/python build_inqmt.py "$REPO" inqmt/qn_core_full.py full | tail -1
  CORE=$(sed '2d' inqmt/qn_core_full.py | shasum | cut -c1-16)
  RDIR=regress_$CORE
  S=inqmt/A_SYSTEM_QMT_NATIVE_shadow_regress.py; L=inqmt/A_SYSTEM_QMT_NATIVE_live_regress.py
  QN_EQ_SEED=$PWD/eqseed_prod_full venv/bin/python build_qmt_strategy.py shadow "$QMT_ACCOUNT_ID" "$QN_PAPER_CASH" "$S"
  QN_EQ_SEED=$PWD/eqseed_prod_full venv/bin/python build_qmt_strategy.py live "$QMT_ACCOUNT_ID" 0 "$L"
  A_SYSTEM_REPO=$REPO sh inqmt/run_regression.sh "$S" "$L" "$RDIR" > "$RDIR.log" 2>&1 || true
  rm -f "$S" "$L"   # 实盘版含资金账号，用完就删
  LOG=$RDIR.log
  echo "回归日志 $LOG：$(grep -c '^RESULT PASS' "$LOG" | tr -d ' ') 节通过，$(grep '^RESULT' "$LOG" | grep -vc '^RESULT PASS' | tr -d ' ') 节其它结果"
fi

echo "== 2. 打核心包，核对回归日志"
venv/bin/python build_inqmt.py "$REPO" inqmt/qn_core_full.py full | tail -1
CORE=$(sed '2d' inqmt/qn_core_full.py | shasum | cut -c1-16)
echo "核心包 $CORE"
head -1 "$LOG" | grep -q "^== core $CORE" || { echo "回归日志不是这个核心包跑的（$(head -1 "$LOG")）"; cleanup; exit 1; }
sections=$(grep -c "^== " "$LOG" | tr -d ' ')
passed=$(grep -c "^RESULT PASS" "$LOG" | tr -d ' ')
others=$(grep "^RESULT" "$LOG" | grep -vc "^RESULT PASS" | tr -d ' ')
echo "回归：RESULT PASS $passed 节，其它结果 $others 节"
[ "$others" = "0" ] && [ "$passed" -ge 34 ] || { echo "回归没有全部通过"; cleanup; exit 1; }
if [ "$NEW_FRAMEWORK" = "1" ]; then
  awk '/^== / { l = ($0 == "== L plan feed") } l && /^RESULT PASS$/ { ok = 1 } END { exit !ok }' "$LOG" ||
    { echo "回归缺少L计划接收测试通过记录"; cleanup; exit 1; }
fi
grep -q "^drill exit 0" "$LOG" || { echo "整日演练没有正常结束"; cleanup; exit 1; }
grep -q "^drill tracebacks 0" "$LOG" || { echo "整日演练里有异常（$(grep '^drill tracebacks' "$LOG")）"; cleanup; exit 1; }
$PY36 inqmt/compile36_check.py inqmt/qn_core_full.py
cp "$REPO/data/raw/trade_calendar.csv" /tmp/qn_cal.csv
$PY36 inqmt/import_check.py "$PWD/inqmt/qn_core_full.py" 2>&1 | tail -1 | tee /tmp/qn_import_check.txt
grep -q "import failures 0" /tmp/qn_import_check.txt || { echo "Python 3.6 导入检查不通过"; cleanup; exit 1; }

echo "== 3. 生成策略文件，接管测试"
QN_EQ_SEED=$PWD/eqseed_prod_full venv/bin/python build_qmt_strategy.py live "$QMT_ACCOUNT_ID" 0 inqmt/A_SYSTEM_QMT_NATIVE_live.py
QN_EQ_SEED=$PWD/eqseed_prod_full venv/bin/python build_qmt_strategy.py shadow "$QMT_ACCOUNT_ID" "$QN_PAPER_CASH" inqmt/A_SYSTEM_QMT_NATIVE_shadow.py
if [ "$NEW_FRAMEWORK" = "1" ] || [ -n "$QN_EQ_SEED_ACE_M_L" ]; then
  EQ_SEED_SHA=$(sed -n '4s/^# eq_ace_m_l_seed \([0-9a-f]*\)\./\1/p' inqmt/A_SYSTEM_QMT_NATIVE_live.py)
  [ ${#EQ_SEED_SHA} -eq 64 ] && grep -qx "== eq_ace_m_l_seed $EQ_SEED_SHA" "$LOG" || {
    echo "ACE+M+L方案甲种子与回归记录不一致或缺少记录，拒绝部署"
    rm -f inqmt/A_SYSTEM_QMT_NATIVE_live.py inqmt/A_SYSTEM_QMT_NATIVE_shadow.py; cleanup; exit 1; }
fi
QN_OLD_SQLITE=1 $PY36 inqmt/test_takeover.py inqmt/A_SYSTEM_QMT_NATIVE_shadow.py inqmt/A_SYSTEM_QMT_NATIVE_live.py > /tmp/qn_takeover.txt 2>&1 || true
grep -E "^RESULT" /tmp/qn_takeover.txt
grep -q "^RESULT PASS" /tmp/qn_takeover.txt || { echo "接管测试不通过"; cleanup; exit 1; }
RELEASE=$(sed -n '2s/.*release \([^,]*\),.*/\1/p' inqmt/A_SYSTEM_QMT_NATIVE_live.py)
echo "新版本 ${RELEASE}，$(wc -c < inqmt/A_SYSTEM_QMT_NATIVE_live.py | tr -d ' ') 字节"
# 回归必须跑的是同一份代码：策略文件第3行的完整代码摘要（运行时、核心包、模拟券商、宿主、包装）和回归日志第2行一致
CODE=$(sed -n '3s/^# code \([0-9a-f]*\), seed .*/\1/p' inqmt/A_SYSTEM_QMT_NATIVE_live.py)
SEED=$(sed -n '3s/^# code [0-9a-f]*, seed \([0-9a-f]*\)\./\1/p' inqmt/A_SYSTEM_QMT_NATIVE_live.py)
grep -q "^== artifact WARNING" "$LOG" && { echo "回归时影子版和实盘版不是同一份代码"; rm -f inqmt/A_SYSTEM_QMT_NATIVE_live.py inqmt/A_SYSTEM_QMT_NATIVE_shadow.py; cleanup; exit 1; }
grep -q "^== artifact code $CODE " "$LOG" || {
  echo "回归日志不是这份代码跑的（日志：$(grep '^== artifact' "$LOG" | head -1)；要部署的：code $CODE）"
  rm -f inqmt/A_SYSTEM_QMT_NATIVE_live.py inqmt/A_SYSTEM_QMT_NATIVE_shadow.py; cleanup; exit 1; }
grep -q "^== artifact code $CODE seed $SEED" "$LOG" || \
  echo "提示：种子数据和回归时不同（交易日历、最近涨停池、生产状态快照每天更新）；接管测试用的是新种子"
echo "代码摘要 $CODE 与回归一致"

if [ "$MODE" = "--check" ]; then
  rm -f inqmt/A_SYSTEM_QMT_NATIVE_live.py inqmt/A_SYSTEM_QMT_NATIVE_shadow.py
  cleanup
  echo "检查通过（--check：没有提交、发布或改动虚拟机目录）"
  exit 0
fi

echo "== 4. 旧文件进回滚目录，新文件放进虚拟机目录"
mkdir -p "$VMDIR/rollback"
OLD=""
if [ -f "$VMDIR/A_SYSTEM_QMT_NATIVE_live.py" ]; then
  OLD=$(sed -n '2s/.*release \([^,]*\),.*/\1/p' "$VMDIR/A_SYSTEM_QMT_NATIVE_live.py")
  cp "$VMDIR/A_SYSTEM_QMT_NATIVE_live.py" "$VMDIR/rollback/A_SYSTEM_QMT_NATIVE_live_$OLD.py"
  echo "回滚文件：qmt_native_phase0/rollback/A_SYSTEM_QMT_NATIVE_live_$OLD.py"
fi
cp inqmt/A_SYSTEM_QMT_NATIVE_live.py "$VMDIR/A_SYSTEM_QMT_NATIVE_live.py"
rm -f inqmt/A_SYSTEM_QMT_NATIVE_shadow.py

echo "== 5. 核心包提交到本仓库，发布到主仓库"
if [ -n "$(git status --porcelain inqmt/qn_core_full.py)" ]; then
  git add inqmt/qn_core_full.py
  git commit -q -m "打包核心取 A_System $(git -C "$REPO" rev-parse --short HEAD)（release ${RELEASE}）"
fi
sh publish_to_a_system.sh "QMT原生版 release ${RELEASE}（工作仓库 $(git rev-parse --short HEAD)）"

cat <<EOF
== 6. 在虚拟机 QMT 里换上新版本（文件已经同步到 C:\\A_System\\qmt_native_phase0\\A_SYSTEM_QMT_NATIVE_live.py）
  1. 模型交易：A_SYSTEM 行“操作”点 ■ 停止
  2. 模型研究 → A_SYSTEM_QMT_INNER → 编辑：全选，粘贴 A_SYSTEM_QMT_NATIVE_live.py 全部内容 → 编译（编辑器里不要点“运行”）
  3. 模型交易：A_SYSTEM 行点 ▶ → 提示“已修改该策略”点确定
  4. 验收：策略日志出现 started: mode=live、release ${RELEASE}；手机收到“✅ 程序与账户已恢复正常”
回滚：同样的步骤，粘贴 qmt_native_phase0\\rollback\\A_SYSTEM_QMT_NATIVE_live_$OLD.py 的内容。
状态在 QMT 日志和注册表里，换版本后自动恢复；新副本会接管并结束旧副本的线程。
EOF
echo "DEPLOY STAGED $RELEASE"
