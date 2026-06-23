from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).absolute().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.notify import notify


def main() -> int:
    parser = argparse.ArgumentParser(description="发送A_System统一Bark通知。")
    parser.add_argument("event")
    parser.add_argument("title")
    parser.add_argument("body")
    parser.add_argument("--level", default="active")
    parser.add_argument("--call", action="store_true")
    args = parser.parse_args()
    ok = notify(args.event, args.title, args.body, level=args.level, call=args.call)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
