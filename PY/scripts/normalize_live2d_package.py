"""
Live2D 模型包第一层规范化 CLI。

示例（在 PY 目录下）:
  python scripts/normalize_live2d_package.py --package moka
  python scripts/normalize_live2d_package.py --package moka --dry-run
  python scripts/normalize_live2d_package.py --package moka --init-config
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 保证可从 PY 目录直接运行
_PY_ROOT = Path(__file__).resolve().parents[1]
if str(_PY_ROOT) not in sys.path:
    sys.path.insert(0, str(_PY_ROOT))

from live2d_db.package_normalize import (  # noqa: E402
    CONFIG_FILENAME,
    build_default_moka_config,
    default_resources_root,
    load_config,
    normalize_package,
    save_config,
)


def main() -> None:
    p = argparse.ArgumentParser(description="Live2D 模型包第一层资源规范化")
    p.add_argument("--package", required=True, help="Resources 下目录名，如 moka")
    p.add_argument(
        "--resources",
        type=Path,
        default=None,
        help="Resources 根目录，默认 Demo/public/Resources",
    )
    p.add_argument("--dry-run", action="store_true", help="只报告不写入")
    p.add_argument(
        "--init-config",
        action="store_true",
        help=f"仅生成 {CONFIG_FILENAME}（moka 使用内置模板，其它包生成空模板）",
    )
    args = p.parse_args()

    base = args.resources if args.resources else default_resources_root()
    package_dir = base / args.package
    if not package_dir.is_dir():
        raise SystemExit(f"目录不存在: {package_dir}")

    if args.init_config:
        if args.package.lower() == "moka":
            cfg = build_default_moka_config()
        else:
            from live2d_db.package_normalize import NormalizeConfig

            cfg = NormalizeConfig(
                package_key=args.package,
                entry_model3=f"{args.package}.model3.json",
            )
        path = save_config(package_dir, cfg)
        print(f"已写入配置: {path}")
        return

    cfg = load_config(package_dir)
    if cfg is None and args.package.lower() == "moka":
        cfg = build_default_moka_config()
        save_config(package_dir, cfg)
        print(f"已自动生成 {CONFIG_FILENAME}（moka 默认模板）")

    report = normalize_package(package_dir, config=cfg, dry_run=args.dry_run)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    if report.errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
