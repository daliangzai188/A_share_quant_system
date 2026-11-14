#!/usr/bin/env python3
"""行情数据的独立快照（2026-11-15）。

行情、加工结果和研究底座不进 git（2026-11-14 起），只在 Mac 和虚拟机之间由 Syncthing 双向同步：一边删错或写坏，
另一边几秒钟就跟着变。这里在同步目录之外另存只读快照，每份带每个文件的 sha256 清单，并能完整恢复、逐个核对。

  python tools/data_snapshot.py snapshot                 给 data/ 拍一份快照
  python tools/data_snapshot.py verify [快照名]           按清单重算快照里每个文件的校验码（默认最新一份）
  python tools/data_snapshot.py restore-check [快照名]    恢复演练：把快照完整恢复到临时目录、逐个核对校验码，
                                                         再和现在的 data/ 比较（列出快照之后改过、新增、不见的文件），
                                                         最后删掉临时目录
  python tools/data_snapshot.py restore 快照名 目标目录    把快照恢复到一个新目录（不往同步工作区里写；
                                                         要换回同步工作区，确认后人工处理，否则会同步到虚拟机）
  python tools/data_snapshot.py list                     列出快照
  python tools/data_snapshot.py prune --keep N           只保留最近 N 份（硬链接共享的文件不受影响）

快照放在 ~/A_System_data_snapshots/（环境变量 A_SYSTEM_SNAPSHOT_ROOT 可改），每份一个目录 YYYYMMDD-HHMMSS/：
  files/            data/ 下全部文件，只读；和上一份内容相同的文件用硬链接，不重复占空间
  MANIFEST.sha256   每行“sha256  字节数  相对路径”，按路径排序
  SNAPSHOT.json     来源目录、git 提交号、文件数、总字节数、清单本身的 sha256
拍快照时文件还在被写（比如虚拟机同步过来新数据）会重读，三次都不一致就按快照里实际存下的内容记清单并列出来；
最好在收盘流水线跑完以后、或周末拍。只读 data/，不改同步工作区里的任何文件。
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path(os.environ.get("A_SYSTEM_SNAPSHOT_ROOT", Path.home() / "A_System_data_snapshots"))
MANIFEST = "MANIFEST.sha256"
SUMMARY = "SNAPSHOT.json"
CHUNK = 1024 * 1024


def _skip(name: str) -> bool:
    """Syncthing 的临时文件和 macOS 的目录缓存不算数据。"""
    return name == ".DS_Store" or name.startswith((".syncthing.", "~syncthing~")) or name.endswith(".tmp")


def data_files(source: Path) -> list[str]:
    found = []
    for folder, dirs, names in os.walk(source):
        dirs[:] = sorted(d for d in dirs if d not in (".stversions", ".stfolder"))
        for name in names:
            path = Path(folder) / name
            if _skip(name) or path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(source).as_posix()
            if "\n" in rel:
                raise ValueError("文件名里有换行，清单写不下：%r" % rel)
            found.append(rel)
    return sorted(found)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_and_hash(source: Path, target: Path) -> str:
    digest = hashlib.sha256()
    with source.open("rb") as reader, target.open("wb") as writer:
        for chunk in iter(lambda: reader.read(CHUNK), b""):
            digest.update(chunk)
            writer.write(chunk)
    shutil.copystat(source, target)
    return digest.hexdigest()


def read_manifest(snapshot: Path) -> dict[str, tuple[str, int]]:
    entries = {}
    for line in (snapshot / MANIFEST).read_text(encoding="utf-8").splitlines():
        digest, size, rel = line.split("  ", 2)
        entries[rel] = (digest, int(size))
    return entries


def snapshots(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".") and (p / SUMMARY).exists())


def pick(root: Path, name: str | None) -> Path:
    found = snapshots(root)
    if name:
        path = root / name
        if path not in found:
            raise SystemExit("没有这份快照：%s（%s）" % (name, root))
        return path
    if not found:
        raise SystemExit("还没有快照：%s" % root)
    return found[-1]


def _git_head(source: Path) -> str:
    try:
        return subprocess.run(["git", "-C", str(source.parent), "rev-parse", "HEAD"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""   # 来源不在 git 仓库里也能拍快照，只是不记提交号


def take_snapshot(source: Path, root: Path, *, now: dt.datetime | None = None) -> Path:
    source = source.resolve()
    if not source.is_dir():
        raise SystemExit("数据目录不存在：%s" % source)
    root.mkdir(parents=True, exist_ok=True)
    if root.resolve() == source or source in root.resolve().parents:
        raise SystemExit("快照目录不能放在数据目录里面：%s" % root)
    name = (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")
    if (root / name).exists():
        raise SystemExit("同名快照已存在：%s" % (root / name))
    previous = snapshots(root)
    old = read_manifest(previous[-1]) if previous else {}
    work = root / (".incomplete-" + name)
    shutil.rmtree(work, ignore_errors=True)
    lines, linked, copied, unstable, total = [], 0, 0, [], 0
    for rel in data_files(source):
        src, dst = source / rel, work / "files" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        before = sha256_file(src)
        prev = previous[-1] / "files" / rel if previous else None
        if rel in old and old[rel][0] == before and prev.is_file() and prev.stat().st_size == old[rel][1]:
            os.link(prev, dst)             # 内容和上一份相同：硬链接（拍完后按清单整份重算一遍，坏的会查出来）
            digest, linked = before, linked + 1
        else:
            for _ in range(3):
                digest = copy_and_hash(src, dst)
                if digest == before:
                    break
                before = sha256_file(src)       # 复制时文件在变：重读再来
            else:
                unstable.append(rel)
            os.chmod(dst, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            copied += 1
        size = dst.stat().st_size
        total += size
        lines.append("%s  %d  %s" % (digest, size, rel))
    manifest = "\n".join(lines) + ("\n" if lines else "")
    (work / MANIFEST).write_text(manifest, encoding="utf-8")
    summary = {
        "schema_version": 1,
        "name": name,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": str(source),
        "git_head": _git_head(source),
        "file_count": len(lines),
        "total_bytes": total,
        "manifest_sha256": hashlib.sha256(manifest.encode("utf-8")).hexdigest(),
        "previous": previous[-1].name if previous else "",
        "linked_from_previous": linked,
        "copied": copied,
        "changed_while_copying": unstable,
    }
    (work / SUMMARY).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    work.rename(root / name)
    return root / name


def verify_snapshot(snapshot: Path) -> list[str]:
    """按清单核对快照：清单本身没被改、每个文件都在且字节数和校验码相同、没有清单外的文件。"""
    problems = []
    summary = json.loads((snapshot / SUMMARY).read_text(encoding="utf-8"))
    manifest = (snapshot / MANIFEST).read_text(encoding="utf-8")
    if hashlib.sha256(manifest.encode("utf-8")).hexdigest() != summary["manifest_sha256"]:
        problems.append("清单本身被改过")
    entries = read_manifest(snapshot)
    files = snapshot / "files"
    for rel, (digest, size) in entries.items():
        path = files / rel
        if not path.is_file():
            problems.append("缺文件 " + rel)
        elif path.stat().st_size != size or sha256_file(path) != digest:
            problems.append("内容不对 " + rel)
    present = set(data_files(files)) if files.is_dir() else set()
    problems += ["清单外的文件 " + rel for rel in sorted(present - set(entries))]
    if len(entries) != summary["file_count"]:
        problems.append("文件数和 SNAPSHOT.json 不同")
    return problems


def restore(snapshot: Path, target: Path, *, source: Path | None = None) -> list[str]:
    """把快照恢复到一个新目录（普通可写文件），再按清单逐个核对；返回问题清单。"""
    target = target.resolve()
    if target.exists() and any(target.iterdir()):
        raise SystemExit("目标目录不是空的：%s" % target)
    for guard in filter(None, [source, REPO / "data"]):
        guard = guard.resolve()
        if target == guard or guard in target.parents:
            raise SystemExit("不往数据目录里恢复（会同步到虚拟机）：%s" % target)
    entries = read_manifest(snapshot)
    problems = []
    for rel, (digest, size) in entries.items():
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if copy_and_hash(snapshot / "files" / rel, dst) != digest or dst.stat().st_size != size:
            problems.append("恢复后内容不对 " + rel)
        os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    return problems


def compare_with_live(snapshot: Path, source: Path) -> dict[str, list[str]]:
    entries = read_manifest(snapshot)
    live = data_files(source)
    changed = [rel for rel in live if rel in entries and sha256_file(source / rel) != entries[rel][0]]
    return {
        "changed": changed,
        "added": [rel for rel in live if rel not in entries],
        "missing": sorted(set(entries) - set(live)),
    }


def prune(root: Path, keep: int) -> list[str]:
    """只保留最近 keep 份。文件只读不影响删除（删的是目录项）；硬链接共享的内容留给还在的快照，权限位不动。"""
    for leftover in list(root.glob(".incomplete-*")) + list(root.glob(".restore-check-*")):
        shutil.rmtree(leftover, ignore_errors=True)
    removed = []
    for path in snapshots(root)[:-keep] if keep > 0 else []:
        shutil.rmtree(path)
        removed.append(path.name)
    return removed


def _size(n: int) -> str:
    return "%.1f MB" % (n / 1e6) if n < 1e9 else "%.2f GB" % (n / 1e9)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="行情数据的独立快照（清单、核对、恢复演练）")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="快照目录（默认 %(default)s）")
    parser.add_argument("--source", type=Path, default=REPO / "data", help="数据目录（默认本仓库的 data/）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot")
    for name in ("verify", "restore-check"):
        sub.add_parser(name).add_argument("name", nargs="?")
    one = sub.add_parser("restore")
    one.add_argument("name")
    one.add_argument("target", type=Path)
    sub.add_parser("list")
    sub.add_parser("prune").add_argument("--keep", type=int, required=True)
    args = parser.parse_args(argv)

    if args.command == "snapshot":
        path = take_snapshot(args.source, args.root)
        summary = json.loads((path / SUMMARY).read_text(encoding="utf-8"))
        print("快照 %s：%d 个文件，%s（新复制 %d 个，和上一份相同用硬链接 %d 个）"
              % (path, summary["file_count"], _size(summary["total_bytes"]), summary["copied"],
                 summary["linked_from_previous"]))
        for rel in summary["changed_while_copying"]:
            print("  复制时一直在变（按快照里存下的内容记清单）：" + rel)
        problems = verify_snapshot(path)
        print("核对：" + ("全部一致" if not problems else "；".join(problems[:20])))
        return 1 if problems else 0
    if args.command == "verify":
        path = pick(args.root, args.name)
        problems = verify_snapshot(path)
        print("%s：%s" % (path.name, "按清单核对全部一致" if not problems else "；".join(problems[:20])))
        return 1 if problems else 0
    if args.command == "restore-check":
        path = pick(args.root, args.name)
        work = Path(tempfile.mkdtemp(prefix=".restore-check-", dir=args.root))
        try:
            problems = verify_snapshot(path) + restore(path, work / "data", source=args.source)
            entries = read_manifest(path)
            print("%s：恢复 %d 个文件到临时目录，%s" % (path.name, len(entries),
                                                  "逐个核对校验码全部一致" if not problems else "；".join(problems[:20])))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if args.source.is_dir():
            diff = compare_with_live(path, args.source)
            print("和现在的 %s 比：快照之后改过 %d 个、新增 %d 个、不见了 %d 个"
                  % (args.source, len(diff["changed"]), len(diff["added"]), len(diff["missing"])))
            for kind, label in (("missing", "不见了"), ("changed", "改过")):
                for rel in diff[kind][:20]:
                    print("  %s：%s" % (label, rel))
        return 1 if problems else 0
    if args.command == "restore":
        problems = restore(pick(args.root, args.name), args.target, source=args.source)
        print("恢复到 %s：%s" % (args.target, "逐个核对校验码全部一致" if not problems else "；".join(problems[:20])))
        return 1 if problems else 0
    if args.command == "list":
        for path in snapshots(args.root):
            summary = json.loads((path / SUMMARY).read_text(encoding="utf-8"))
            print("%s  %6d 个文件  %10s  git %s" % (path.name, summary["file_count"], _size(summary["total_bytes"]),
                                                summary["git_head"][:7]))
        return 0
    removed = prune(args.root, args.keep)
    print("删掉 %d 份旧快照：%s" % (len(removed), " ".join(removed) or "无"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
