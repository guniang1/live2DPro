import { getChatWsUrl } from "../api/wsConfig.js";

function connectChat() {
  const ws = new WebSocket(getChatWsUrl());
  ws.onopen = () => {
    ws.send(JSON.stringify({ message: "你好，请介绍一下自己" }));
  };
  // 在收到后端消息时添加口型同步
  ws.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);

      // 如果有文本内容，启动口型同步
      if (data.text && data.text.trim()) {
        const manager = LAppLive2DManager.getInstance();
        manager.applyLipSync(data.text);
      }

      // 原有的消息处理逻辑...
      if (data.text) {
        appendChatMessage('assistant', data.text);
      }

      // 处理表情和动作
      if (data.expression || data.motion) {
        const manager = LAppLive2DManager.getInstance();
        manager.applyChatLive2dActions(data.expression, data.motion);
      }

    } catch (error) {
      console.error('WebSocket消息处理错误:', error);
    }
  };

  ws.onerror = (err) => console.error(err);
}


export default {
  connectChat
}