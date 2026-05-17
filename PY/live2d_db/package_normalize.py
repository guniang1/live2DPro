"""
Live2D 模型包第一层规范化：目录整理、model3.json 修补、表情分层。

目标布局对齐 Demo 附录 H（Xiaozi 类标准包）：
  {package_key}.model3.json
  expressions/*.exp3.json     # 全部 exp3（写入 model3 Expressions，供对话 LLM）
  motions/*.motion3.json      # 身体动作（须在 model3 Motions 登记）
  textures/ 或 {Name}.4096/   # 贴图

用法见 scripts/normalize_live2d_package.py 与 docs/功能/live2d/第一层资源规范化.md
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .hit_areas_templates import resolve_hit_areas_for_package

CONFIG_FILENAME = ".live2d-package.json"
SCHEMA_VERSION = 1

@dataclass
class NormalizeConfig:
    package_key: str
    entry_model3: str = ""
    expressions_subdir: str = "expressions"
    motions_subdir: str = "motions"
    lip_sync_param_ids: list[str] = field(default_factory=lambda: ["ParamMouthOpenY"])
    hit_areas: list[dict[str, str]] = field(default_factory=list)
    remove_paths: list[str] = field(default_factory=list)
    remove_globs: list[str] = field(default_factory=list)
    motions: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    junk_dirs: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any], package_key: str) -> NormalizeConfig:
        expr = data.get("expression_layout") or {}
        return cls(
            package_key=str(data.get("package_key") or package_key).strip() or package_key,
            entry_model3=str(data.get("entry_model3") or f"{package_key}.model3.json").strip(),
            expressions_subdir=str(
                expr.get("expressions_subdir")
                or expr.get("emotion_subdir")
                or "expressions"
            )
            .strip()
            .strip("/"),
            motions_subdir=str(
                expr.get("motions_subdir")
                or expr.get("motion_subdir")
                or "motions"
            )
            .strip()
            .strip("/"),
            lip_sync_param_ids=_as_str_list(
                data.get("lip_sync_param_ids") or ["ParamMouthOpenY"]
            ),
            hit_areas=_as_hit_areas(data.get("hit_areas")),
            remove_paths=_as_str_list(data.get("remove_paths")),
            remove_globs=_as_str_list(data.get("remove_globs")),
            motions=_as_motions(data.get("motions")),
            junk_dirs=_as_str_list(data.get("junk_dirs")),
        )


@dataclass
class NormalizeReport:
    package_key: str
    package_dir: str
    dry_run: bool
    removed: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)
    model3_patched: bool = False
    emotion_count: int = 0
    motion_count: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    hit_areas_source: str = ""
    standard_issues: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "package_key": self.package_key,
            "package_dir": self.package_dir,
            "dry_run": self.dry_run,
            "removed": self.removed,
            "moved": self.moved,
            "model3_patched": self.model3_patched,
            "emotion_count": self.emotion_count,
            "motion_count": self.motion_count,
            "warnings": self.warnings,
            "errors": self.errors,
            "hit_areas_source": self.hit_areas_source,
            "standard_issues": self.standard_issues,
        }


def default_resources_root() -> Path:
    return Path(__file__).resolve().parents[2] / "Demo" / "public" / "Resources"


def load_config(package_dir: Path) -> NormalizeConfig | None:
    cfg_path = package_dir / CONFIG_FILENAME
    if not cfg_path.is_file():
        return None
    with open(cfg_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{cfg_path} 须为 JSON 对象")
    pk = str(data.get("package_key") or package_dir.name).strip()
    return NormalizeConfig.from_dict(data, pk)


def save_config(package_dir: Path, config: NormalizeConfig) -> Path:
    payload = {
        "schema": SCHEMA_VERSION,
        "package_key": config.package_key,
        "entry_model3": config.entry_model3,
        "expression_layout": {
            "expressions_subdir": config.expressions_subdir,
            "motions_subdir": config.motions_subdir,
        },
        "lip_sync_param_ids": config.lip_sync_param_ids,
        "hit_areas": config.hit_areas,
        "remove_paths": config.remove_paths,
        "remove_globs": config.remove_globs,
        "motions": config.motions,
        "junk_dirs": config.junk_dirs,
    }
    path = package_dir / CONFIG_FILENAME
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def _as_str_list(val: Any) -> list[str]:
    if not val:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    return [str(val).strip()] if str(val).strip() else []


def _as_hit_areas(val: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(val, list):
        return out
    for item in val:
        if not isinstance(item, dict):
            continue
        iid = str(item.get("Id") or item.get("id") or "").strip()
        name = str(item.get("Name") or item.get("name") or "").strip()
        if iid and name:
            out.append({"Id": iid, "Name": name})
    return out


def _as_motions(val: Any) -> dict[str, list[dict[str, str]]]:
    if not isinstance(val, dict):
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    for group, arr in val.items():
        if not isinstance(arr, list):
            continue
        items: list[dict[str, str]] = []
        for item in arr:
            if isinstance(item, dict):
                fp = str(item.get("File") or "").strip().lstrip("/")
                if fp:
                    items.append({"File": fp})
            elif isinstance(item, str) and item.strip():
                items.append({"File": item.strip().lstrip("/")})
        if items:
            out[str(group)] = items
    return out


def _name_from_exp_path(rel: str) -> str:
    name = PurePosixPath(rel).name
    lower = name.lower()
    if lower.endswith(".exp3.json"):
        return name[: -len(".exp3.json")]
    return PurePosixPath(rel).stem


def _relocate_legacy_expression_dirs(
    package_dir: Path,
    cfg: NormalizeConfig,
    report: NormalizeReport,
    dry_run: bool,
) -> None:
    """将历史 wardrobe/、motion/（单数）中的 exp3 迁入 expressions/。"""
    sub = cfg.expressions_subdir
    for legacy_name in ("wardrobe", "motion"):
        legacy = package_dir / legacy_name
        if not legacy.is_dir():
            continue
        for item in sorted(legacy.rglob("*.exp3.json")):
            if not item.is_file():
                continue
            dest = package_dir / sub / item.name
            rel = f"{legacy_name}/{item.relative_to(legacy).as_posix()}"
            report.moved.append(f"{rel} -> {sub}/{item.name}")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    dest.unlink()
                shutil.move(str(item), str(dest))
        if not dry_run and legacy.is_dir():
            shutil.rmtree(legacy, ignore_errors=True)
            report.removed.append(f"{legacy_name}/")


def _relocate_motion3_files(
    package_dir: Path,
    cfg: NormalizeConfig,
    report: NormalizeReport,
    dry_run: bool,
) -> None:
    motions_dir = package_dir / cfg.motions_subdir
    if not dry_run:
        motions_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(package_dir.rglob("*.motion3.json")):
        try:
            rel = path.relative_to(package_dir)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] == cfg.motions_subdir:
            continue
        dest = motions_dir / path.name
        dest_rel = f"{cfg.motions_subdir}/{path.name}"
        if path.resolve() == dest.resolve():
            continue
        report.moved.append(f"{rel.as_posix()} -> {dest_rel}")
        if not dry_run:
            if dest.exists():
                dest.unlink()
            shutil.move(str(path), str(dest))


def _relocate_all_exp3_to_expressions(
    package_dir: Path,
    cfg: NormalizeConfig,
    report: NormalizeReport,
    dry_run: bool,
) -> list[dict[str, str]]:
    """包内全部 .exp3.json 归入 expressions/ 并返回 model3 Expressions 条目。"""
    expr_dir = package_dir / cfg.expressions_subdir
    if not dry_run:
        expr_dir.mkdir(parents=True, exist_ok=True)

    by_name: dict[str, str] = {}
    for path in sorted(package_dir.rglob("*.exp3.json")):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(package_dir)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] in ("wardrobe", "motion"):
            continue

        name = _name_from_exp_path(path.name)
        dest_rel = f"{cfg.expressions_subdir}/{path.name}"
        dest = package_dir / dest_rel

        if path.resolve() != dest.resolve():
            report.moved.append(f"{rel.as_posix()} -> {dest_rel}")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if dest.exists():
                    dest.unlink()
                shutil.move(str(path), str(dest))

        if name in by_name and by_name[name] != dest_rel:
            report.warnings.append(f"exp3 标识名重复，后者覆盖: {name}")
        by_name[name] = dest_rel

    return [{"Name": n, "File": f} for n, f in sorted(by_name.items())]


def _glob_remove(package_dir: Path, pattern: str) -> list[Path]:
    pat = pattern.replace("\\", "/").strip()
    if pat.endswith("/**"):
        base = pat[: -len("/**")]
        root = package_dir / base
        if not root.exists():
            return []
        return sorted(root.rglob("*")) + ([root] if root.is_dir() else [])
    if "/" in pat:
        return [p for p in package_dir.glob(pat) if p.exists()]
    return [p for p in package_dir.rglob(pat) if p.exists()]


def _delete_path_safe(target: Path) -> None:
    """删除文件或目录；路径已不存在时静默跳过（避免 glob 重复命中）。"""
    if not target.exists():
        return
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    else:
        try:
            target.unlink()
        except FileNotFoundError:
            pass


def _remove_path(package_dir: Path, rel: str, report: NormalizeReport, dry_run: bool) -> None:
    rel = rel.strip().lstrip("/")
    if not rel:
        return
    target = package_dir / rel
    if not target.exists():
        return
    report.removed.append(rel)
    if dry_run:
        return
    _delete_path_safe(target)


def _remove_glob_pattern(package_dir: Path, glob_pat: str, report: NormalizeReport, dry_run: bool) -> None:
    matches = _glob_remove(package_dir, glob_pat)
    if not matches:
        return
    # 先删深层路径，避免先删父目录导致子路径 unlink 报 FileNotFoundError
    ordered = sorted(set(matches), key=lambda p: len(p.parts), reverse=True)
    for p in ordered:
        rel = p.relative_to(package_dir).as_posix()
        if rel in report.removed:
            continue
        if not p.exists():
            continue
        report.removed.append(rel)
        if not dry_run:
            _delete_path_safe(p)


def _patch_model3(
    model3: dict[str, Any],
    config: NormalizeConfig,
    emotion_entries: list[dict[str, str]],
) -> dict[str, Any]:
    refs = model3.setdefault("FileReferences", {})
    if not isinstance(refs, dict):
        refs = {}
        model3["FileReferences"] = refs
    refs["Expressions"] = [
        {"Name": e["Name"], "File": e["File"]} for e in emotion_entries
    ]
    if config.motions:
        refs["Motions"] = config.motions
    groups = model3.get("Groups")
    if not isinstance(groups, list):
        groups = []
        model3["Groups"] = groups

    lip_ids = config.lip_sync_param_ids or ["ParamMouthOpenY"]
    eye_ids = ["ParamEyeLOpen", "ParamEyeROpen"]
    has_eye = False
    has_lip = False
    for g in groups:
        if not isinstance(g, dict):
            continue
        if g.get("Name") == "EyeBlink":
            has_eye = True
            if not g.get("Ids"):
                g["Ids"] = list(eye_ids)
        if g.get("Name") == "LipSync":
            has_lip = True
            g["Ids"] = list(lip_ids)
    if not has_eye:
        groups.append({"Target": "Parameter", "Name": "EyeBlink", "Ids": list(eye_ids)})
    if not has_lip:
        groups.append({"Target": "Parameter", "Name": "LipSync", "Ids": list(lip_ids)})

    if config.hit_areas:
        model3["HitAreas"] = list(config.hit_areas)
    elif not isinstance(model3.get("HitAreas"), list):
        model3["HitAreas"] = []

    return model3


def normalize_package(
    package_dir: Path,
    *,
    config: NormalizeConfig | None = None,
    dry_run: bool = False,
) -> NormalizeReport:
    package_dir = package_dir.resolve()
    pk = package_dir.name
    cfg = config or load_config(package_dir)
    if cfg is None:
        cfg = NormalizeConfig(package_key=pk, entry_model3=f"{pk}.model3.json")

    report = NormalizeReport(
        package_key=cfg.package_key,
        package_dir=str(package_dir),
        dry_run=dry_run,
    )

    entry = package_dir / cfg.entry_model3
    if not entry.is_file():
        report.errors.append(f"入口 model3 不存在: {cfg.entry_model3}")
        return report

    _relocate_legacy_expression_dirs(package_dir, cfg, report, dry_run)

    for rel in cfg.remove_paths:
        _remove_path(package_dir, rel, report, dry_run)

    for glob_pat in cfg.remove_globs:
        _remove_glob_pattern(package_dir, glob_pat, report, dry_run)

    for junk in cfg.junk_dirs:
        _remove_path(package_dir, junk, report, dry_run)

    with open(entry, encoding="utf-8") as f:
        model3 = json.load(f)

    if not cfg.hit_areas:
        areas, src = resolve_hit_areas_for_package(package_dir, cfg.package_key, model3)
        if areas:
            cfg.hit_areas = areas
            report.hit_areas_source = src

    emotion_entries = _relocate_all_exp3_to_expressions(package_dir, cfg, report, dry_run)
    report.emotion_count = len(emotion_entries)

    _relocate_motion3_files(package_dir, cfg, report, dry_run)
    motions_dir = package_dir / cfg.motions_subdir
    if motions_dir.is_dir():
        report.motion_count = sum(
            1 for p in motions_dir.glob("*.motion3.json") if p.is_file()
        )

    emotion_entries.sort(key=lambda x: x["Name"])
    patched = _patch_model3(model3, cfg, emotion_entries)

    idle = (patched.get("FileReferences") or {}).get("Motions") or {}
    if not idle:
        report.warnings.append(
            "未配置 Motions（Idle/TapBody）；待机与点击身体动作将不可用，请在 Cubism 导出后写入 .live2d-package.json 的 motions"
        )
    if not cfg.hit_areas and not (isinstance(patched.get("HitAreas"), list) and patched["HitAreas"]):
        report.warnings.append(
            "未配置 HitAreas；点头/点身体无效。可在 zip 内放置 .live2d-hit-areas.json 或登记 data/hit_areas 模板"
        )
    if cfg.hit_areas and not report.hit_areas_source:
        report.hit_areas_source = "config"

    if not dry_run:
        with open(entry, "w", encoding="utf-8", newline="\n") as f:
            json.dump(patched, f, ensure_ascii=False, indent="\t")
            f.write("\n")
        report_path = package_dir / ".live2d-normalize-report.json"
        with open(report_path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
            f.write("\n")

    report.model3_patched = True
    return report


def parse_model3_index(model3_path: Path) -> tuple[dict[str, dict[str, str]], dict[str, str], set[str]]:
    """解析入口 model3，返回 (expressions_by_file, motions_by_file, listed_files)。"""
    with open(model3_path, encoding="utf-8") as f:
        payload = json.load(f)
    refs = payload.get("FileReferences", {}) if isinstance(payload, dict) else {}
    expressions_by_file: dict[str, dict[str, str]] = {}
    motions_by_file: dict[str, str] = {}
    listed_files: set[str] = set()

    for item in refs.get("Expressions", []) or []:
        if not isinstance(item, dict):
            continue
        file_path = str(item.get("File") or "").strip().lstrip("/")
        if not file_path:
            continue
        expressions_by_file[file_path] = {"name": str(item.get("Name") or "").strip()}
        listed_files.add(file_path)

    motions = refs.get("Motions", {}) or {}
    if isinstance(motions, dict):
        for group, arr in motions.items():
            if not isinstance(arr, list):
                continue
            for item in arr:
                if not isinstance(item, dict):
                    continue
                file_path = str(item.get("File") or "").strip().lstrip("/")
                if not file_path:
                    continue
                motions_by_file[file_path] = str(group)
                listed_files.add(file_path)

    for key in ("Moc", "Physics", "DisplayInfo"):
        v = refs.get(key)
        if isinstance(v, str) and v.strip():
            listed_files.add(v.strip().lstrip("/"))
    for tex in refs.get("Textures", []) or []:
        if isinstance(tex, str) and tex.strip():
            listed_files.add(tex.strip().lstrip("/"))
    return expressions_by_file, motions_by_file, listed_files


def pick_entry_model3(package_dir: Path, package_key: str) -> Path | None:
    expect = package_dir / f"{package_key}.model3.json"
    if expect.is_file():
        return expect
    candidates = sorted(package_dir.glob("*.model3.json"))
    return candidates[0] if candidates else None


def build_default_moka_config() -> NormalizeConfig:
    return NormalizeConfig(
        package_key="moka",
        entry_model3="moka.model3.json",
        lip_sync_param_ids=["ParamMouthOpenY"],
        hit_areas=[],
        remove_paths=[
            "textures/moca.moc3",
            "textures/moca.model3.json",
            "textures/moca.physics3.json",
            "textures/moca.cdi3.json",
            "textures/moca.vtube.json",
            "textures/items_pinned_to_model.json",
        ],
        remove_globs=["textures/textures/**"],
        junk_dirs=[],
        motions={},
    )


# ---------- zip 上传规范化 ----------


def _safe_zip_member(name: str) -> str | None:
    normalized = name.replace("\\", "/").strip("/")
    if not normalized:
        return None
    pp = PurePosixPath(normalized)
    if pp.is_absolute():
        return None
    parts = [part for part in pp.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    if parts[0].lower() == "__macosx":
        return None
    return "/".join(parts)


def _detect_zip_root_prefix(entries: list[str]) -> str | None:
    if not entries:
        return None
    first_seg = entries[0].split("/", 1)[0]
    for path in entries:
        parts = path.split("/", 1)
        if len(parts) < 2 or parts[0] != first_seg:
            return None
    return first_seg


def should_skip_upload_asset(rel_path: str) -> bool:
    rel = rel_path.replace("\\", "/").strip().lstrip("/")
    if not rel:
        return True
    low = rel.lower()
    if low.startswith("__macosx/") or low.endswith(".ds_store"):
        return True
    name = PurePosixPath(rel).name
    if name == ".live2d-normalize-report.json":
        return True
    if name.startswith(".") and name.endswith(".bak"):
        return True
    return False


def infer_config(package_dir: Path, package_key: str) -> NormalizeConfig:
    loaded = load_config(package_dir)
    if loaded is not None:
        return loaded
    if package_key.lower() == "moka":
        return build_default_moka_config()
    return NormalizeConfig(
        package_key=package_key,
        entry_model3=f"{package_key}.model3.json",
        remove_globs=[
            "textures/moca.*",
            "textures/textures/**",
        ],
    )


def _lip_sync_uses_eye_params(model3: dict[str, Any]) -> bool:
    groups = model3.get("Groups")
    if not isinstance(groups, list):
        return False
    for g in groups:
        if not isinstance(g, dict) or g.get("Name") != "LipSync":
            continue
        ids = g.get("Ids") or []
        if not isinstance(ids, list):
            return False
        for pid in ids:
            s = str(pid)
            if "Eye" in s and "Open" in s:
                return True
    return False


def assess_package_standardness(
    package_dir: Path,
    cfg: NormalizeConfig,
) -> tuple[bool, list[str]]:
    """返回 (是否已规范, 问题列表)。"""
    issues: list[str] = []
    entry = package_dir / cfg.entry_model3
    if not entry.is_file():
        entry = pick_entry_model3(package_dir, cfg.package_key)
    if entry is None or not entry.is_file():
        issues.append("缺少入口 .model3.json")
        return False, issues

    for p in package_dir.iterdir():
        if p.is_file() and p.name.lower().endswith(".exp3.json"):
            issues.append("根目录存在未归类的 .exp3.json")
            break

    for rel in cfg.remove_paths:
        if (package_dir / rel).exists():
            issues.append(f"存在应删除的冗余文件: {rel}")

    for glob_pat in cfg.remove_globs:
        if list(_glob_remove(package_dir, glob_pat)):
            issues.append(f"存在应清理的路径模式: {glob_pat}")

    try:
        with open(entry, encoding="utf-8") as f:
            model3 = json.load(f)
    except (json.JSONDecodeError, OSError):
        issues.append("入口 model3.json 无法解析")
        return False, issues

    if _lip_sync_uses_eye_params(model3):
        issues.append("LipSync 误绑定眼睛开合参数")

    refs = model3.get("FileReferences") or {}
    for item in refs.get("Expressions") or []:
        if not isinstance(item, dict):
            continue
        fp = str(item.get("File") or "").strip().replace("\\", "/")
        if fp and not fp.startswith(f"{cfg.expressions_subdir}/"):
            issues.append(f"表情未在 {cfg.expressions_subdir}/ 下: {fp}")
            break

    for legacy in ("wardrobe", "motion"):
        if (package_dir / legacy).is_dir():
            issues.append(f"存在已废弃目录 {legacy}/，应仅保留 expressions/ 与 motions/")

    for path in package_dir.rglob("*.exp3.json"):
        try:
            rel = path.relative_to(package_dir)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] == cfg.motions_subdir:
            issues.append(f"exp3 不应位于 {cfg.motions_subdir}/: {rel.as_posix()}")
            break

    for path in package_dir.rglob("*.motion3.json"):
        try:
            rel = path.relative_to(package_dir)
        except ValueError:
            continue
        if rel.parts and rel.parts[0] != cfg.motions_subdir:
            issues.append(f"动作文件未在 {cfg.motions_subdir}/ 下: {rel.as_posix()}")
            break

    return len(issues) == 0, issues


def _extract_zip_to_dir(payload: bytes, dest: Path) -> tuple[str | None, list[str]]:
    paths: list[str] = []
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            safe = _safe_zip_member(info.filename)
            if not safe:
                continue
            paths.append(safe)
            out = dest / safe
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(zf.read(info))
    root_prefix = _detect_zip_root_prefix(paths)
    return root_prefix, paths


def _repack_dir_to_zip(package_dir: Path, root_prefix: str | None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(package_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(package_dir).as_posix()
            if should_skip_upload_asset(rel):
                continue
            arcname = f"{root_prefix}/{rel}" if root_prefix else rel
            zf.writestr(arcname, path.read_bytes())
    return buf.getvalue()


def normalize_zip_bytes(
    payload: bytes,
    *,
    package_key_hint: str | None = None,
) -> tuple[bytes, NormalizeReport, str]:
    """
    解压 → 执行 normalize_package（含 LLM 分类）→ 重新打包。
    上传路径固定调用，无开关跳过。
    返回 (zip_bytes, report, package_key)。
    """
    with tempfile.TemporaryDirectory(prefix="live2d_norm_") as tmp:
        tmp_path = Path(tmp)
        root_prefix, _paths = _extract_zip_to_dir(payload, tmp_path)

        if root_prefix:
            package_dir = tmp_path / root_prefix
        else:
            package_dir = tmp_path

        if not package_dir.is_dir():
            report = NormalizeReport(
                package_key=package_key_hint or "",
                package_dir=str(package_dir),
                dry_run=False,
                errors=["zip 中无有效文件"],
            )
            return payload, report, package_key_hint or ""

        pkg = (package_key_hint or "").strip() or root_prefix or package_dir.name
        cfg = infer_config(package_dir, pkg)
        pkg = cfg.package_key

        report = NormalizeReport(
            package_key=pkg,
            package_dir=str(package_dir),
            dry_run=False,
        )

        _ok, issues = assess_package_standardness(package_dir, cfg)
        report.standard_issues = issues
        if issues:
            report.warnings.append("检测到非标准项: " + "; ".join(issues))

        norm = normalize_package(package_dir, config=cfg, dry_run=False)
        report.removed = norm.removed
        report.moved = norm.moved
        report.model3_patched = norm.model3_patched
        report.emotion_count = norm.emotion_count
        report.motion_count = norm.motion_count
        report.warnings.extend(norm.warnings)
        report.errors.extend(norm.errors)
        report.hit_areas_source = norm.hit_areas_source

        if report.errors:
            return payload, report, pkg

        new_payload = _repack_dir_to_zip(package_dir, root_prefix)
        return new_payload, report, pkg
