/**
 * 录音工具，使用 MediaRecorder API
 */
class AudioRecorder {
    /**
     * 构造函数
     */
    constructor() {
        this.mediaRecorder = null; // 录音实例，用于控制录音的开始、停止等操作
        this.audioChunks = []; // 音频片段，用于存储录音的音频数据
        this.stream = null; // 音频流，用于获取麦克风权限
    }

    /**
     * 开始录音
     */
    async start() {
        // ================================ 准备环境 ================================
        // 获取麦克风权限，通过navigator.mediaDevices.getUserMedia获取麦克风权限
        // { audio: true } 表示获取麦克风权限
        // 返回一个 Promise，resolve时返回音频流，reject时返回错误
        this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        
        // 通过音频流创建录音实例对象
        // this.stream 是音频流
        // MediaRecorder 接收这个流，边录边存到 audioChunks
        this.mediaRecorder = new MediaRecorder(this.stream);

        // 清空音频片段
        this.audioChunks = [];

        // 监听音频数据可用事件
        // 必须在 start() 之前设置，否则录音开始后可能收不到数据
        // ondataavailable 是 MediaRecorder 的内置事件，当有音频数据可用时触发
        this.mediaRecorder.ondataavailable = (e) => {
            if (e.data.size > 0) 
                // 将音频数据添加到音频片段中
                this.audioChunks.push(e.data);
            // 如果音频数据大小为0，则不添加到音频片段中
        };

        // ================================ 开始录音 ================================
        // start() 是 MediaRecorder 的内置方法。
        this.mediaRecorder.start();

     
    }

    /**
     * 停止录音
     */
    stop() {
        // ================================ 准备返回数据 ================================
        // stop() 是 MediaRecorder 的内置方法。
        // 表示完成录音时，调用 onstop 事件
        return new Promise((resolve) => {
            // onstop 是 MediaRecorder 的内置事件，当录音停止时触发
            this.mediaRecorder.onstop = () => {
                // 停止音频流
                this.stream?.getTracks().forEach((t) => t.stop());
                // 将音频片段转换为 Blob 对象
                const blob = new Blob(this.audioChunks, { type: "audio/webm" });
                // 将 Blob 对象传递给 resolve 函数
                // resolve 函数是 Promise 的内置函数，当 Promise 成功时调用
                resolve(blob);
            };
            // ================================ 停止录音 ================================
            // stop() 是 MediaRecorder 的内置方法。
            this.mediaRecorder.stop();
        });
    }

    /**
     * 判断是否正在录音
     */
    isRecording() {
        // state 是 MediaRecorder 的内置属性，表示当前状态
        // 如果状态为 recording，则表示正在录音
        return this.mediaRecorder?.state === "recording";
    }
}
/**
 * 使用方式：
 * const recorder = new AudioRecorder();
 * 
 * recorder.start();
 * recorder.stop();
 * recorder.isRecording();
 */
export default new AudioRecorder();