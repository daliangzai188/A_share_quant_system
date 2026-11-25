#!/bin/sh
# 一条命令发布（2026-11-15）：测试、对照标准、打包、QMT回归和校验全部自动跑，任何一步没通过就停在那一步。
#
#   sh tools/release.sh 导出副本路径          代码：导出副本跑全量测试和生产链对照标准（逐字节比对）→ 落到同步工作区并推送
#   sh tools/release.sh 导出副本路径 --qmt    再加 QMT 原生版：按同步工作区打核心包、生成策略文件 → 完整回归（约55分钟）
#                                             → 核对回归、Python 3.6 编译导入、接管测试 → 旧实盘文件进回滚目录、新文件
#                                             放进虚拟机目录 → 核心包提交发布；最后打印 QMT 里要点的步骤
#   sh tools/release.sh 导出副本路径 --qmt-check   同上，但只检查不放文件、不提交
#
# 在同步工作区（本脚本所在仓库）执行。QMT 里粘贴、编译、重启仍要人在虚拟机上做（券商平台只能这样换策略）。
# 不在 ⑬（约15:15）到次日 09:00 之间换QMT版本，除非确认重启后会补跑当天流水线。
set -e
DEV=${1:?"用法: sh tools/release.sh 导出副本路径 [--qmt|--qmt-check]"}
MODE=${2:-}
cd "$(dirname "$0")/.."
echo "======== 代码：发布前检查并落地 ========"
sh tools/publish_dev_copy.sh "$DEV"
[ -n "$MODE" ] || exit 0
WORK=${QMT_WORK_REPO:-$HOME/A_System_Rebuild/2026-10-19/qmt_native_full}
[ -f "$WORK/deploy.sh" ] || { echo "找不到 QMT 工作目录（${WORK}，可用 QMT_WORK_REPO 指定）"; exit 2; }
echo "======== QMT 原生版：打包、完整回归、部署 ========"
case "$MODE" in
  --qmt) sh "$WORK/deploy.sh" auto ;;
  --qmt-check) sh "$WORK/deploy.sh" auto --check ;;
  *) echo "不认识的参数：$MODE"; exit 2 ;;
esac
