#!/bin/sh
# 把导出副本里测试过、已提交的改动落到同步工作区并推送到 GitHub（2026-11-14 项目优化第5步）。
#
#   sh tools/publish_dev_copy.sh 导出副本路径
#
# 在同步工作区（本脚本所在仓库，编辑它就是部署）执行：
# - 同步工作区有未提交的代码改动就停（虚拟机同步来的 data/ 改动不算）；
# - 只允许快进（导出副本必须包含同步工作区的全部提交）；
# - 新提交改了、同步工作区里也被改过的文件（比如虚拟机更新的参考表）不覆盖，停下来；
# - 新提交里“停止跟踪”的文件（git rm --cached）不能从工作区删掉，否则 Syncthing 会把删除同步到虚拟机：
#   一律用 reset --mixed 落地，再检出新增、修改、改名的文件，删除的文件只删真正被删掉的那些；前后核对 data/ 下没有文件不见；
# - 推送前检查新提交里没有资金账号（从 QMT 工作目录的 local.env 读，可用 QN_LOCAL_ENV 指定）；
# - 2026-11-15 起每次发布前自动在导出副本跑全量测试和生产链对照标准（tools/verify_dev_copy.sh，逐字节比对），
#   没通过就不发布；同一个提交已经检查通过的不重跑。
set -e
DEV=${1:?"用法: sh tools/publish_dev_copy.sh 导出副本路径"}
cd "$(dirname "$0")/.."
LOCAL_ENV=${QN_LOCAL_ENV:-$HOME/A_System_Rebuild/2026-10-19/qmt_native_full/local.env}
[ -f "$LOCAL_ENV" ] && . "$LOCAL_ENV"
[ -n "$QMT_ACCOUNT_ID" ] || { echo "读不到资金账号（${LOCAL_ENV}），无法做推送前检查"; exit 2; }

dirty=$(git status --porcelain --untracked-files=no | grep -v ' data/' || true)
[ -z "$dirty" ] || { echo "同步工作区有未提交的代码改动，先处理："; echo "$dirty"; exit 1; }
git fetch -q "$DEV" main
OLD=$(git rev-parse HEAD)
NEW=$(git rev-parse FETCH_HEAD)
[ "$OLD" != "$NEW" ] || { echo "没有新提交"; exit 0; }
git merge-base --is-ancestor "$OLD" "$NEW" || { echo "导出副本不包含同步工作区的全部提交，不能快进"; exit 1; }
# 只查新增的行：删除的内容本来就在历史里，停止跟踪大批行情时也不用把几百万行旧数据过一遍
n=$(git diff --no-renames "$OLD" "$NEW" | grep '^+' | grep -c "$QMT_ACCOUNT_ID" || true)
[ "$n" = "0" ] || { echo "新提交里出现资金账号，拒绝发布"; exit 1; }
# 发布前自动检查：导出副本的这个提交跑过全量测试和对照标准才发布（PUBLISH_VERIFY_CMD 只给本脚本的测试用）
[ "$(git -C "$DEV" rev-parse HEAD)" = "$NEW" ] || { echo "导出副本当前不在 main 最新提交上，先切回 main"; exit 1; }
VERIFIED=$(cut -d' ' -f1 "$(git -C "$DEV" rev-parse --absolute-git-dir)/a_system_verified" 2>/dev/null || true)
if [ "$VERIFIED" != "$NEW" ]; then
  if [ -n "$PUBLISH_VERIFY_CMD" ]; then
    sh -c "$PUBLISH_VERIFY_CMD" || { echo "发布前检查没通过，未发布"; exit 1; }
  else
    sh "$DEV/tools/verify_dev_copy.sh" "$DEV" || { echo "发布前检查没通过，未发布"; exit 1; }
  fi
fi

list_data() { find data -type f 2>/dev/null | LC_ALL=C sort; }
BEFORE=/tmp/publish_before.$$; AFTER=/tmp/publish_after.$$
list_data > "$BEFORE"
git reset -q --mixed "$NEW"
# 新增、修改的文件按新提交检出（改名算删旧加新）；真正删除的文件（新提交里没有、也不是改成忽略）才从工作区删
changed=$(git -c core.quotePath=false diff --no-renames --name-only --diff-filter=AMT "$OLD" "$NEW")
# 先查冲突：新提交改了、同步工作区里也被改过（比如虚拟机更新了某张参考表）的文件，不能被覆盖
conflicts=$(echo "$changed" | while IFS= read -r path; do
  [ -n "$path" ] && [ -e "$path" ] || continue
  git diff --quiet "$NEW" -- "$path" && continue          # 已经是新内容
  git diff --quiet "$OLD" -- "$path" || echo "$path"      # 既不是新内容也不是旧内容：本地改过
done)
if [ -n "$conflicts" ]; then
  git reset -q --mixed "$OLD"; rm -f "$BEFORE"
  echo "这些文件新提交改了、同步工作区里也改过，先人工合并（已恢复原状，未推送）："; echo "$conflicts"; exit 1
fi
[ -z "$changed" ] || echo "$changed" | tr '\n' '\0' | xargs -0 git checkout "$NEW" --
git -c core.quotePath=false diff --no-renames --name-only --diff-filter=D "$OLD" "$NEW" | while IFS= read -r path; do
  [ -e "$path" ] || continue
  if git check-ignore -q "$path"; then
    echo "停止跟踪、保留在本地：$path" >> /tmp/publish_untracked.$$
  else
    rm -f "$path"
    rmdir -p "$(dirname "$path")" 2>/dev/null || true   # 删空了的目录一起删
  fi
done
kept=$(cat /tmp/publish_untracked.$$ 2>/dev/null | wc -l | tr -d ' '); rm -f /tmp/publish_untracked.$$
list_data > "$AFTER"
lost=$(LC_ALL=C comm -23 "$BEFORE" "$AFTER")
echo "data/ 文件 $(wc -l < "$BEFORE" | tr -d ' ') → $(wc -l < "$AFTER" | tr -d ' ') 个；停止跟踪但保留在本地 ${kept} 个"
rm -f "$BEFORE" "$AFTER"
[ -z "$lost" ] || { echo "这些 data/ 文件不见了，停下来检查（未推送）："; echo "$lost" | head -20; exit 1; }
left=$(git status --porcelain --untracked-files=no | grep -v ' data/' || true)
[ -z "$left" ] || { echo "落地后还有和新提交不一致的文件（未推送）："; echo "$left"; exit 1; }
git log --oneline "$OLD..$NEW"
git push origin HEAD:main 2>&1 | tail -1
