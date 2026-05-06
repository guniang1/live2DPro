"""
一次性脚本：按「校园偶像 萨摩耶狗狗」按键说明重命名 Xiaogou/expressions 下数字文件名，
并同步 Xiaogou.model3.json、Xiaogou.vtube.json 中的路径与 Name。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "Demo" / "public" / "Resources" / "Xiaogou"
EXP = ROOT / "expressions"

# 编号(文件名) -> 语义化标识（与 txt 中 F/N 行说明对应；9 未在表内单独说明）
SEMANTIC: dict[str, str] = {
    "1": "脱外套",
    "2": "舞台吉他",
    "3": "毛线帽子",
    "4": "鸭舌帽子",
    "5": "狗狗垂耳",
    "6": "狗狗立耳",
    "7": "舞台掀刘海",
    "8": "耶耶喝水",
    "9": "备用表情9",
    "10": "耶耶扑克",
    "11": "打游戏动作",
    "12": "舞台麦克风",
    "13": "星星眼",
    "14": "眼镜切换",
    "15": "生气符号",
    "16": "唱歌耳麦",
    "17": "眯眼害羞脸",
    "18": "闭眼",
    "19": "爱心眼吐舌",
    "20": "哭哭脸",
    "21": "鼓嘴赌气",
    "22": "耶耶水壶背带切换",
}


def main() -> None:
    if not EXP.is_dir():
        raise SystemExit(f"目录不存在: {EXP}")

    # 两阶段重命名，避免 Windows 上覆盖
    for k in SEMANTIC:
        src = EXP / f"{k}.exp3.json"
        if not src.is_file():
            raise SystemExit(f"缺少文件: {src}")
        src.rename(EXP / f"__tmp_{k}.exp3.json")
    for k, name in SEMANTIC.items():
        (EXP / f"__tmp_{k}.exp3.json").rename(EXP / f"{name}.exp3.json")

    model_path = ROOT / "Xiaogou.model3.json"
    with open(model_path, encoding="utf-8") as f:
        model3 = json.load(f)
    for item in model3["FileReferences"]["Expressions"]:
        key = item.get("Name")
        if isinstance(key, str) and key in SEMANTIC:
            sem = SEMANTIC[key]
            item["Name"] = sem
            item["File"] = f"expressions/{sem}.exp3.json"
    with open(model_path, "w", encoding="utf-8") as f:
        json.dump(model3, f, ensure_ascii=False, indent="\t")
        f.write("\n")

    vtube_path = ROOT / "Xiaogou.vtube.json"
    text = vtube_path.read_text(encoding="utf-8")
    # 长数字优先替换，避免 1 匹配到 10、11…
    for k in sorted(SEMANTIC.keys(), key=lambda x: int(x), reverse=True):
        name = SEMANTIC[k]
        text = text.replace(f"expressions/{k}.exp3.json", f"expressions/{name}.exp3.json")
    vtube_path.write_text(text, encoding="utf-8")

    print("OK:", len(SEMANTIC), "files renamed + model3 + vtube updated")


if __name__ == "__main__":
    main()
