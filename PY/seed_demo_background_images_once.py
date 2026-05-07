"""
一次性：扫描 Demo/public/Resources/background 目录，将背景同步到 MinIO，并写入 MySQL 表 background_image。

表字段：id, name（无扩展名）, url（MinIO 公开 URL）, create_time

前置：
  1. 已执行 live2d_db/migrations/20260508_background_image.sql
  2. PY/.env：MYSQL_*、MINIO_*（与 minio_storage.py 一致）

对象键默认：Resources/background/<文件名>（与前端 Resources 相对路径一致）
  可通过环境变量 BACKGROUND_IMAGE_OBJECT_PREFIX 修改前缀（勿首尾 /）。

同名不同扩展名（如 foo.jpg 与 foo.png）只保留一条：扩展名优先级 .jpg > .jpeg > .png > .webp > .gif。

用法：
  cd PY && python seed_demo_background_images_once.py
  python seed_demo_background_images_once.py --no-upload   # 仅写库（假定 MinIO 上已有同名对象）
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from pathlib import Path

_PY_ROOT = Path(__file__).resolve().parent
if str(_PY_ROOT) not in sys.path:
    sys.path.insert(0, str(_PY_ROOT))

from dotenv import load_dotenv

load_dotenv(_PY_ROOT / ".env")

from live2d_db.connection import connection_ctx
from live2d_db.minio_storage import get_bucket_name, object_public_url, upload_file

_REPO_ROOT = _PY_ROOT.parent
_BG_DIR = _REPO_ROOT / "Demo" / "public" / "Resources" / "background"

_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
_EXT_RANK = {".jpg": 0, ".jpeg": 1, ".png": 2, ".webp": 3, ".gif": 4}


def _object_prefix() -> str:
    return (os.environ.get("BACKGROUND_IMAGE_OBJECT_PREFIX") or "Resources/background").strip().strip("/")


def _object_key(file_name: str) -> str:
    return f"{_object_prefix()}/{file_name}"


def _content_type(file_name: str) -> str:
    mt, _ = mimetypes.guess_type(file_name)
    return mt or "application/octet-stream"


def _ext_rank(suffix: str) -> int:
    return _EXT_RANK.get(suffix.lower(), 99)


def _scan_background_files() -> list[Path]:
    """列出目录内图片文件；按 stem 去重，扩展名按优先级保留一个。"""
    if not _BG_DIR.is_dir():
        raise SystemExit(f"背景目录不存在: {_BG_DIR}")

    by_stem: dict[str, Path] = {}
    for p in sorted(_BG_DIR.iterdir(), key=lambda x: x.name):
        if not p.is_file() or p.name == "background_order.json":
            continue
        suf = p.suffix.lower()
        if suf not in _IMAGE_SUFFIXES:
            continue
        stem = p.stem
        prev = by_stem.get(stem)
        if prev is None:
            by_stem[stem] = p
            continue
        r_new, r_old = _ext_rank(suf), _ext_rank(prev.suffix.lower())
        if r_new < r_old or (r_new == r_old and p.name < prev.name):
            print(f"[warn] 同名「{stem}」多文件，保留 {p.name}，跳过 {prev.name}")
            by_stem[stem] = p
        else:
            print(f"[warn] 同名「{stem}」多文件，保留 {prev.name}，跳过 {p.name}")

    return sorted(by_stem.values(), key=lambda x: x.stem)


def main() -> None:
    parser = argparse.ArgumentParser(description="背景图入库 MinIO + MySQL background_image")
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="不上传本地文件，只根据约定对象键生成 url 并写入 MySQL",
    )
    args = parser.parse_args()

    files = _scan_background_files()
    bucket = get_bucket_name()
    prefix = _object_prefix()

    rows: list[tuple[str, str]] = []
    uploaded = 0

    for local in files:
        file_name = local.name
        stem = local.stem
        key = _object_key(file_name)

        if not args.no_upload:
            upload_file(local, key, content_type=_content_type(file_name))
            uploaded += 1

        url = object_public_url(bucket, key)
        rows.append((stem, url))

    sql_clear = "DELETE FROM background_image"
    sql_insert = """
        INSERT INTO background_image (name, url)
        VALUES (%s, %s)
    """

    with connection_ctx() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_clear)
            cur.executemany(sql_insert, rows)

    print(
        f"完成：background_image 写入 {len(rows)} 条；"
        f"上传 {uploaded} 个对象到 bucket={bucket} 前缀={prefix}/"
    )


if __name__ == "__main__":
    main()
