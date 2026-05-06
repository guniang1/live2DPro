from kokoro import KModel
import torch
import soundfile as sf
from pathlib import Path
import os
import json

# ========== 核心配置（适配你的本地文件） ==========
# 模型路径
LOCAL_MODEL_DIR = Path("E:/CubismDemo/kokoro-model")
MODEL_WEIGHT_PATH = LOCAL_MODEL_DIR / "kokoro-v1_1-zh.pth"
CONFIG_PATH = LOCAL_MODEL_DIR / "config.json"
# 你的本地音色文件（改成你实际的文件名，比如 zf_001.pt）
VOICE_FILE = "zf_001.pt"
VOICE_PATH = LOCAL_MODEL_DIR / "voices" / VOICE_FILE
# 输出音频路径
OUTPUT_WAV = Path("E:/CubismDemo/PY/utils/kokoro_test.wav")

# ========== 强制禁用所有网络请求 ==========
os.environ["HTTP_PROXY"] = ""
os.environ["HTTPS_PROXY"] = ""
os.environ["KOKORO_AUTO_DOWNLOAD"] = "False"
os.environ["HF_HUB_DISABLE_DOWNLOAD"] = "1"

# ========== 设备配置 ==========
device = "cuda" if torch.cuda.is_available() else "cpu"
print(f"使用设备：{device}")

# ========== 检查所有本地文件 ==========
# 检查核心文件
required_files = [MODEL_WEIGHT_PATH, CONFIG_PATH, VOICE_PATH]
for f in required_files:
    if not f.exists():
        raise FileNotFoundError(f"❌ 缺失文件：{f}")

# 列出所有本地音色（方便你切换）
all_voices = [v.name for v in (LOCAL_MODEL_DIR/"voices").glob("*.pt")]
print(f"✅ 本地可用音色：{all_voices}")
print(f"✅ 本次使用音色：{VOICE_FILE}")

# ========== 手动加载配置和模型（完全本地） ==========
# 1. 读取配置
with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    cfg = json.load(f)

# 2. 初始化模型（跳过所有仓库关联）
model = KModel(repo_id="").to(device).eval()

# 3. 加载模型权重
state_dict = torch.load(MODEL_WEIGHT_PATH, map_location=device)
model.load_state_dict(state_dict, strict=False)

# ========== 加载本地音色 ==========
voice_data = torch.load(VOICE_PATH, map_location=device)
# 适配音色数据格式
if isinstance(voice_data, dict):
    pack = voice_data.get("weight", voice_data)
else:
    pack = voice_data
pack = pack.to(device)

# ========== 生成语音（纯本地逻辑） ==========
# 要生成的文本
text = "你好！这是纯本地Kokoro模型生成的语音，无任何网络请求～"
# 语速（0.8-1.2 为宜）
speed = 1.0

# 核心生成逻辑（绕过pipeline的自动下载）
with torch.no_grad():
    # 文本编码
    x = model.encode_text(text, lang="z")
    # 生成音频
    audio = model.generate(
        x,
        pack=pack,
        speed=speed,
        noise_scale=0.667,
        noise_scale_w=0.8,
        length_scale=1.0
    )

# ========== 保存音频 ==========
# 转换音频格式（确保是单通道、浮点型）
audio = audio.squeeze().cpu().numpy()
sample_rate = 24000  # Kokoro固定采样率
sf.write(str(OUTPUT_WAV), audio, sample_rate)

print(f"\n🎉 语音生成成功！")
print(f"📂 文件路径：{OUTPUT_WAV}")
print(f"🔊 采样率：{sample_rate}Hz | 语速：{speed}")
print("💡 提示：双击WAV文件即可试听，修改VOICE_FILE可切换音色～")