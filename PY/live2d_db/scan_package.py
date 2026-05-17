"""
扫描 Demo/public/Resources/<package_key> 目录，将文件写入 live2d_model_asset 表。

默认扫描 Xiaozi，与仓库中 Demo/public/Resources/Xiaozi 对齐。

用法（在 PY 目录下）:
  python -m live2d_db.scan_package --user-id 1 --package Xiaozi
  python -m live2d_db.scan_package --dry-run

环境变量: MYSQL_* 与 DbConfig.from_env() 一致。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Optional

from .connection import connection_ctx
from .db_config import DbConfig
from .entities import Live2dModelAsset
from .package_normalize import parse_model3_index, pick_entry_model3
from .repositories import Live2dModelAssetRepository


def infer_asset_type(filename: str) -> str:
    n = filename.lower()
    if n.endswith(".model3.json"):
        return "model3"
    if n.endswith(".motion3.json"):
        return "motion3"
    if n.endswith(".exp3.json"):
        return "exp3"
    if n.endswith(".physics3.json"):
        return "physics3"
    if n.endswith(".cdi3.json"):
        return "cdi3"
    if n.endswith(".vtube.json"):
        return "vtube"
    return "json_other"


def default_resources_root() -> Path:
    # PY/live2d_db -> CubismDemo/Demo/public/Resources
    return Path(__file__).resolve().parents[2] / "Demo" / "public" / "Resources"


def scan_and_sync(
    package_key: str,
    *,
    user_id: Optional[int] = None,
    resources_root: Optional[Path] = None,
    public_prefix: str = "/Resources",
    dry_run: bool = False,
) -> tuple[int, int]:
    """
    删除该用户在该 package_key 下已有行，再按磁盘扫描插入。
    返回 (删除行数, 插入行数)。
    非 dry-run 时必须提供 user_id（须存在于 user 表，以满足外键）。
    """
    if not dry_run and user_id is None:
        raise ValueError("写入数据库时必须指定 user_id（与 user.user_id 对应）")
    base = resources_root if resources_root is not None else default_resources_root()
    root = base / package_key
    if not root.is_dir():
        raise FileNotFoundError(f"目录不存在: {root}")

    entry_path = pick_entry_model3(root, package_key)
    entry_rel: str | None = None
    expr_index: dict[str, dict[str, str]] = {}
    motion_index: dict[str, str] = {}
    listed_files: set[str] = set()
    if entry_path is not None:
        entry_rel = entry_path.relative_to(root).as_posix()
        expr_index, motion_index, listed_files = parse_model3_index(entry_path)

    assets: list[Live2dModelAsset] = []
    order = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        rel_posix = rel.as_posix()
        if rel_posix.startswith(".live2d") or rel_posix.endswith(".bak"):
            continue
        pub = f"{public_prefix.rstrip('/')}/{package_key}/{rel_posix}"
        st = path.stat()
        logical_name = None
        motion_group = None
        if rel_posix in expr_index:
            logical_name = expr_index[rel_posix].get("name") or None
        if rel_posix in motion_index:
            motion_group = motion_index[rel_posix]
        is_listed = 1 if rel_posix in listed_files else 0
        is_entry = 1 if entry_rel and rel_posix == entry_rel else 0
        assets.append(
            Live2dModelAsset(
                user_id=int(user_id) if user_id is not None else 0,
                package_key=package_key,
                relative_path=rel_posix,
                file_name=path.name,
                asset_type=infer_asset_type(path.name),
                public_url=pub,
                file_size=st.st_size,
                sort_order=order,
                logical_name=logical_name,
                motion_group=motion_group,
                is_listed_in_model3=is_listed,
                is_entry_model=is_entry,
                remark="scan_package",
            )
        )
        order += 1

    if dry_run:
        print(f"[dry-run] {root}: would insert {len(assets)} rows" + (f" (user_id={user_id})" if user_id else ""))
        for a in assets[:5]:
            print(f"  {a.asset_type}  {a.relative_path}")
        if len(assets) > 5:
            print(f"  ... +{len(assets) - 5} more")
        return 0, len(assets)

    deleted = 0
    inserted = 0
    with connection_ctx(DbConfig.from_env()) as conn:
        assert user_id is not None
        deleted = Live2dModelAssetRepository.delete_by_package_key(conn, package_key, user_id)
        for a in assets:
            Live2dModelAssetRepository.insert(conn, a)
            inserted += 1
    return deleted, inserted


def main() -> None:
    p = argparse.ArgumentParser(description="扫描 Resources 下模型包并写入 live2d_model_asset")
    p.add_argument("--package", default="Xiaozi", help="子目录名，如 Xiaozi")
    p.add_argument(
        "--resources",
        type=Path,
        default=None,
        help="Resources 根目录（内含 Xiaozi 等子目录），默认指向仓库 Demo/public/Resources",
    )
    p.add_argument("--public-prefix", default="/Resources", help="URL 前缀")
    p.add_argument("--user-id", type=int, default=None, help="关联 user.user_id；写入数据库时必填")
    p.add_argument("--dry-run", action="store_true", help="只打印不写库")
    args = p.parse_args()
    deleted, inserted = scan_and_sync(
        args.package,
        user_id=args.user_id,
        resources_root=args.resources,
        public_prefix=args.public_prefix,
        dry_run=args.dry_run,
    )
    if not args.dry_run:
        print(f"package={args.package}: deleted={deleted}, inserted={inserted}")


if __name__ == "__main__":
    main()
