"""tools/publish_dev_copy.sh：把导出副本的提交落到同步工作区（2026-11-14 项目优化第5步）。

最怕的是把同步工作区里的行情删掉——Syncthing 会把删除同步到虚拟机。这里用临时仓库模拟：
导出副本的新提交停止跟踪一个行情文件、改名一个配置文件、删掉一个脚本；同步工作区里还有虚拟机同步来的新行情和改动。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "publish_dev_copy.sh"


def git(cwd: Path, *args: str) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@t")
    return subprocess.run(["git", "-c", "init.defaultBranch=main", *args], cwd=cwd, check=True,
                          capture_output=True, text=True, env=env).stdout


@unittest.skipUnless(shutil.which("git"), "需要 git")
class PublishDevCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dev, self.origin, self.sync = root / "dev", root / "origin.git", root / "sync"
        self.dev.mkdir()
        git(self.dev, "init", "-q")
        (self.dev / ".gitignore").write_text("data/state/\n", encoding="utf-8")
        for rel, text in {"data/raw/daily/20261008.csv": "old\n", "config/research_x.json": "{}\n",
                          "scripts/old_tool.py": "print(1)\n", "src/a.py": "A = 1\n"}.items():
            (self.dev / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.dev / rel).write_text(text, encoding="utf-8")
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", "base")
        git(root, "clone", "-q", "--bare", str(self.dev), str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.sync))
        hook = self.sync / ".git" / "hooks" / "pre-push"
        if hook.exists():
            hook.unlink()
        (self.sync / "tools").mkdir()
        shutil.copyfile(SCRIPT, self.sync / "tools" / "publish_dev_copy.sh")
        self.env_file = root / "local.env"
        self.env_file.write_text("QMT_ACCOUNT_ID=99887766\n", encoding="utf-8")
        # 导出副本的新提交：停止跟踪行情、配置改名、删脚本、改代码
        (self.dev / ".gitignore").write_text("data/state/\ndata/*\n", encoding="utf-8")
        git(self.dev, "rm", "-r", "-q", "--cached", "data")
        (self.dev / "config" / "research").mkdir()
        git(self.dev, "mv", "config/research_x.json", "config/research/research_x.json")
        git(self.dev, "rm", "-q", "scripts/old_tool.py")
        (self.dev / "src" / "a.py").write_text("A = 2\n", encoding="utf-8")
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", "split")
        # 同步工作区：虚拟机同步来的新行情和改动
        (self.sync / "data" / "raw" / "daily" / "20261009.csv").write_text("vm new\n", encoding="utf-8")
        (self.sync / "data" / "raw" / "daily" / "20261008.csv").write_text("vm changed\n", encoding="utf-8")

    def run_script(self, verify: str = "true") -> subprocess.CompletedProcess:
        # 发布前检查（全量测试+对照标准）在临时仓库里跑不了，用 PUBLISH_VERIFY_CMD 换成固定结果
        env = dict(os.environ, QN_LOCAL_ENV=str(self.env_file), PUBLISH_VERIFY_CMD=verify)
        return subprocess.run(["sh", "tools/publish_dev_copy.sh", str(self.dev)], cwd=self.sync, env=env,
                              capture_output=True, text=True)

    def test_untracked_market_data_stays_and_other_changes_land(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sync = self.sync
        self.assertEqual((sync / "data/raw/daily/20261008.csv").read_text(encoding="utf-8"), "vm changed\n")
        self.assertEqual((sync / "data/raw/daily/20261009.csv").read_text(encoding="utf-8"), "vm new\n")
        self.assertFalse((sync / "config/research_x.json").exists())
        self.assertTrue((sync / "config/research/research_x.json").exists())
        self.assertFalse((sync / "scripts/old_tool.py").exists())
        self.assertFalse((sync / "scripts").exists(), "删空的目录也要删")
        self.assertEqual((sync / "src/a.py").read_text(encoding="utf-8"), "A = 2\n")
        self.assertEqual(git(sync, "status", "--porcelain", "--untracked-files=no").strip(), "")
        self.assertEqual(git(self.origin, "rev-parse", "main"), git(self.dev, "rev-parse", "HEAD"))

    def test_reference_table_changed_on_both_sides_is_not_overwritten(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)        # 先落地 setUp 里的提交
        # 提交A：放行并新增一张参考表；落地后同步工作区有它
        (self.dev / ".gitignore").write_text("data/state/\ndata/*\n!data/ref.csv\n", encoding="utf-8")
        (self.dev / "data" / "ref.csv").write_text("v1\n", encoding="utf-8")
        git(self.dev, "add", "-A")
        git(self.dev, "commit", "-q", "-m", "A")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.sync / "data/ref.csv").read_text(encoding="utf-8"), "v1\n")
        # 提交B改了这张表，同时虚拟机也改了它：不能覆盖，原样退回
        (self.dev / "data" / "ref.csv").write_text("v2\n", encoding="utf-8")
        git(self.dev, "commit", "-q", "-am", "B")
        (self.sync / "data" / "ref.csv").write_text("vm\n", encoding="utf-8")
        head = git(self.sync, "rev-parse", "HEAD")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("data/ref.csv", result.stdout)
        self.assertEqual((self.sync / "data/ref.csv").read_text(encoding="utf-8"), "vm\n")
        self.assertEqual(git(self.sync, "rev-parse", "HEAD"), head)

    def test_refuses_when_sync_folder_has_uncommitted_code(self) -> None:
        (self.sync / "src" / "a.py").write_text("A = 9\n", encoding="utf-8")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("未提交的代码改动", result.stdout)
        self.assertEqual((self.sync / "src/a.py").read_text(encoding="utf-8"), "A = 9\n")

    def test_refuses_commit_containing_account_number(self) -> None:
        (self.dev / "src" / "a.py").write_text("ACCOUNT = '99887766'\n", encoding="utf-8")
        git(self.dev, "commit", "-q", "-am", "leak")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("资金账号", result.stdout)
        self.assertNotEqual(git(self.origin, "rev-parse", "main"), git(self.dev, "rev-parse", "HEAD"))

    def test_refuses_when_pre_publish_checks_fail(self) -> None:
        head = git(self.sync, "rev-parse", "HEAD")
        result = self.run_script(verify="false")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("发布前检查没通过", result.stdout)
        self.assertEqual(git(self.sync, "rev-parse", "HEAD"), head)
        self.assertNotEqual(git(self.origin, "rev-parse", "main"), git(self.dev, "rev-parse", "HEAD"))

    def test_verified_commit_is_not_checked_again(self) -> None:
        stamp = Path(git(self.dev, "rev-parse", "--absolute-git-dir").strip()) / "a_system_verified"
        stamp.write_text(git(self.dev, "rev-parse", "HEAD").strip() + " 2026-11-15 00:00:00\n", encoding="utf-8")
        result = self.run_script(verify="false")   # 已检查过的提交不会再调用检查
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(git(self.origin, "rev-parse", "main"), git(self.dev, "rev-parse", "HEAD"))


if __name__ == "__main__":
    unittest.main()
