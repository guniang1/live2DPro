"""
HitAreas 侧栏模板：上传规范化时合并进 model3，以及 API 返回填写说明。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HIT_AREAS_SIDECAR = ".live2d-hit-areas.json"
_DATA_DIR = Path(__file__).resolve().parent / "data" / "hit_areas"


def default_resources_root() -> Path:
    return Path(__file__).resolve().parents[2] / "Demo" / "public" / "Resources"


def _read_hit_areas_json(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, list):
        return _normalize_hit_area_items(data)
    if isinstance(data, dict):
        raw = data.get("hit_areas") or data.get("HitAreas")
        if isinstance(raw, list):
            return _normalize_hit_area_items(raw)
    return []


def _normalize_hit_area_items(items: list[Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        iid = str(item.get("Id") or item.get("id") or "").strip()
        name = str(item.get("Name") or item.get("name") or "").strip()
        if iid and name:
            out.append({"Id": iid, "Name": name})
    return out


def load_sidecar_hit_areas(package_dir: Path) -> list[dict[str, str]]:
    return _read_hit_areas_json(package_dir / HIT_AREAS_SIDECAR)


def load_registry_hit_areas(package_key: str) -> list[dict[str, str]]:
    pk = (package_key or "").strip()
    if not pk:
        return []
    for path in (
        _DATA_DIR / f"{pk}.json",
        default_resources_root() / pk / HIT_AREAS_SIDECAR,
    ):
        areas = _read_hit_areas_json(path)
        if areas:
            return areas
    return _read_hit_areas_json(_DATA_DIR / "default.json")


def resolve_hit_areas_for_package(
    package_dir: Path,
    package_key: str,
    model3: dict[str, Any],
) -> tuple[list[dict[str, str]], str]:
    """
    返回 (hit_areas, source)。
    source: model3 | sidecar | registry | none
    """
    existing = model3.get("HitAreas")
    if isinstance(existing, list) and len(existing) > 0:
        normalized = _normalize_hit_area_items(existing)
        if normalized:
            return normalized, "model3"

    sidecar = load_sidecar_hit_areas(package_dir)
    if sidecar:
        return sidecar, "sidecar"

    registry = load_registry_hit_areas(package_key)
    if registry:
        return registry, "registry"

    return [], "none"


def get_hit_areas_template_for_api(package_key: str) -> dict[str, Any]:
    pk = (package_key or "").strip() or "default"
    registered = load_registry_hit_areas(pk)
    return {
        "package_key": pk,
        "sidecar_filename": HIT_AREAS_SIDECAR,
        "sidecar_example": {
            "hit_areas": [
                {"Id": "ArtMesh0", "Name": "Head"},
                {"Id": "ArtMesh10", "Name": "Body"},
            ]
        },
        "model3_hit_areas_example": [
            {"Id": "ArtMesh0", "Name": "Head"},
            {"Id": "ArtMesh10", "Name": "Body"},
        ],
        "registered_template": {"hit_areas": registered} if registered else {},
        "hint": (
            "在 zip 根目录放置 "
            + HIT_AREAS_SIDECAR
            + "，或在 PY/live2d_db/data/hit_areas/"
            + pk
            + ".json 登记；Name 须为 Head/Body。Drawable Id 可在前端 DebugLogEnable 控制台查看。"
        ),
    }
