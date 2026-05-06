/**
 * 语音识别：浏览器采集 PCM，经 WebSocket 发往 FastAPI（router/asr_ws.py 的 /ws/asr，阿里云 DashScope）
 * 地址与 api/wsConfig.js 中 getWsBase() 一致（默认 ws://localhost:8000）
 */

import { getAsrWsUrl } from "../api/wsConfig.js";

/** 将 Float32 单声道缓冲重采样为 16kHz（与 DashScope fun-asr-realtime 一致） */
function downsampleTo16k(float32, inputSampleRate) {
    if (inputSampleRate === 16000) {
        return float32; // 已是 16kHz，无需重采样
    }
    const ratio = inputSampleRate / 16000; // 输入采样率相对 16k 的倍数
    const outLen = Math.floor(float32.length / ratio); // 输出样本个数
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) {
        const start = Math.floor(i * ratio); // 当前输出对应输入区间起点
        const end = Math.min(Math.floor((i + 1) * ratio), float32.length); // 区间终点（不含）
        let sum = 0; // 区间内样本累加（简单平均降采样）
        let n = 0;
        for (let j = start; j < end; j++) {
            sum += float32[j];
            n++;
        }
        out[i] = n ? sum / n : 0; // 区间内平均，避免除零
    }
    return out;
}

/** 将 Float32 样本转为 16bit 小端 PCM（服务端 send_audio_frame 所需） */
function floatTo16BitPCM(float32) {
    const out = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
        const s = Math.max(-1, Math.min(1, float32[i])); // 钳制到 [-1, 1]
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff; // 负半轴用 0x8000 幅度，正半轴用 0x7fff
    }
    return out;
}

class SpeechRecognizer {
    constructor() {
        this.onResult = null;
        this._isRecognizing = false;
        this._ws = null;
        this._mediaStream = null;
        this._audioContext = null;
        this._source = null;
        this._processor = null;
    }

    /** 断开音频节点、关闭 AudioContext、停止麦克风轨道 */
    _cleanupAudio() {
        try {
            this._processor?.disconnect();
        } catch (_) {}
        this._processor = null;
        try {
            this._source?.disconnect();
        } catch (_) {}
        this._source = null;
        if (this._audioContext) {
            this._audioContext.close().catch(() => {});
            this._audioContext = null;
        }
        if (this._mediaStream) {
            this._mediaStream.getTracks().forEach((t) => t.stop());
            this._mediaStream = null;
        }
    }

    /** 关闭 Vosk WebSocket 并置空引用 */
    _cleanupWs() {
        if (this._ws) {
            try {
                this._ws.close();
            } catch (_) {}
            this._ws = null;
        }
    }

    /**
     * 开始识别（连接 FastAPI /ws/asr，默认 main.py:8000）
     * @param {Function} onResult - (text, isFinal)
     * @returns {Promise<boolean>}
     */
    start(onResult) {
        if (this._isRecognizing) return Promise.resolve(false);
        this.onResult = onResult || null;

        return new Promise((resolve) => {
            let settled = false;
            const finish = (ok) => {
                if (settled) return;
                settled = true;
                resolve(ok);
            };

            let ws;
            try {
                ws = new WebSocket(getAsrWsUrl());
                ws.binaryType = "arraybuffer";
            } catch (e) {
                console.error("WebSocket 创建失败:", e);
                alert(`无法创建 WebSocket，请确认已运行 python main.py（8000）\n${e.message}`);
                finish(false);
                return;
            }

            this._ws = ws;

            ws.onopen = async () => {
                try {
                    this._mediaStream = await navigator.mediaDevices.getUserMedia({
                        audio: {
                            channelCount: 1,
                            echoCancellation: true,
                            noiseSuppression: true,
                        },
                    });
                    const ctx = new (window.AudioContext || window.webkitAudioContext)();
                    this._audioContext = ctx;
                    this._source = ctx.createMediaStreamSource(this._mediaStream);
                    const bufferSize = 4096;
                    const processor = ctx.createScriptProcessor(bufferSize, 1, 1);
                    this._processor = processor;

                    processor.onaudioprocess = (ev) => {
                        if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
                        const input = ev.inputBuffer.getChannelData(0);
                        const copy = new Float32Array(input);
                        const down = downsampleTo16k(copy, ctx.sampleRate);
                        const pcm = floatTo16BitPCM(down);
                        this._ws.send(pcm.buffer);
                    };

                    const mute = ctx.createGain();
                    mute.gain.value = 0;
                    this._source.connect(processor);
                    processor.connect(mute);
                    mute.connect(ctx.destination);
                    this._isRecognizing = true;
                    finish(true);
                } catch (e) {
                    console.error("麦克风或音频管线失败:", e);
                    alert(`无法启动麦克风：${e.message}`);
                    this._cleanupAudio();
                    this._cleanupWs();
                    this._isRecognizing = false;
                    finish(false);
                }
            };

            ws.onmessage = (ev) => {
                let data;
                try {
                    data = JSON.parse(ev.data);
                } catch {
                    return;
                }
                if (data.error) {
                    console.error("Vosk:", data.error);
                    alert(data.error);
                    this.stop();
                    return;
                }
                const text = (data.text || "").trim();
                const isFinal = data.partial === false;
                if (this.onResult) {
                    this.onResult(text, isFinal);
                }
            };

            ws.onerror = () => {
                console.error("Vosk WebSocket 错误（请确认已运行 python main.py）");
                if (!settled) {
                    alert(
                        `无法连接语音识别（${getAsrWsUrl()}）。请确认已运行 python main.py 且已配置 DASHSCOPE_API_KEY，或设置 VITE_ASR_WS_URL`
                    );
                    this._cleanupWs();
                    finish(false);
                }
            };

            ws.onclose = () => {
                this._cleanupAudio();
                this._isRecognizing = false;
                if (!settled) {
                    finish(false);
                }
            };
        });
    }

    /** 发送 flush 并释放音频与 WebSocket */
    stop() {
        if (!this._isRecognizing && !this._ws) return;
        try {
            if (this._ws && this._ws.readyState === WebSocket.OPEN) {
                this._ws.send(JSON.stringify({ cmd: "flush" }));
            }
        } catch (_) {}
        this._cleanupAudio();
        this._cleanupWs();
        this._isRecognizing = false;
        this.onResult = null;
    }

    isRecognizingNow() {
        return this._isRecognizing;
    }

    isSupported() {
        return !!(
            navigator.mediaDevices &&
            typeof navigator.mediaDevices.getUserMedia === "function" &&
            typeof WebSocket === "function"
        );
    }
}

export default new SpeechRecognizer();
