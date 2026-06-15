from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).absolute().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.minute_data_importer import MinuteBarImporter
from src.utils.config import load_json_config
from src.utils.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="导入并标准化分钟 K 线 CSV。")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径。")
    parser.add_argument("--input", action="append", default=[], help="输入 CSV 文件路径，可重复传入。")
    parser.add_argument("--input-dir", default="", help="输入 CSV 目录。")
    parser.add_argument("--pattern", default="*.csv", help="配合 --input-dir 使用的文件匹配规则。")
    parser.add_argument("--output", default="", help="输出标准分钟 K 文件路径，默认使用配置。")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已有标准分钟 K 文件。")
    parser.add_argument("--write-template", action="store_true", help="只生成分钟 K 导入模板。")
    return parser.parse_args()


def collect_input_paths(args: argparse.Namespace) -> list[Path]:
    paths = [Path(path) for path in args.input]
    if args.input_dir:
        input_dir = Path(args.input_dir)
        if not input_dir.is_absolute():
            input_dir = PROJECT_ROOT / input_dir
        paths.extend(sorted(input_dir.glob(args.pattern)))
    return paths


def main() -> None:
    args = parse_args()
    config = load_json_config(args.config)
    logging_config = config.get("logging", {})
    setup_logger(
        log_dir=PROJECT_ROOT / logging_config.get("log_dir", "logs"),
        log_file=logging_config.get("log_file", "a_share_quant.log"),
        level=logging_config.get("level", "INFO"),
    )

    importer = MinuteBarImporter(config_path=args.config)
    if args.write_template:
        path = importer.write_template()
        print(f"分钟 K 导入模板已生成: {path}")
        return

    input_paths = collect_input_paths(args)
    outputs = importer.import_files(
        input_paths=input_paths,
        output_path=args.output or None,
        overwrite=args.overwrite,
    )
    print("分钟 K 导入完成：")
    for name, path in outputs.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()
