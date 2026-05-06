/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */

/**
 * Cubism 平台抽象层，封装与平台相关的功能。
 *
 * 集中处理文件读取、时间获取等依赖平台的接口。
 */
export class LAppPal {
  /**
   * 将文件以字节形式读取
   *
   * @param filePath 要读取的文件路径
   * @return { buffer: 读取的字节数据, size: 文件大小 }
   */
  static loadFileAsBytes(filePath, callback) {
    fetch(filePath)
      .then(response => response.arrayBuffer())
      .then(arrayBuffer => callback(arrayBuffer, arrayBuffer.byteLength));
  }

  /**
   * 获取增量时间（与上一帧的差值）
   * @return 增量时间 [秒]
   */
  static getDeltaTime() {
    return this.deltaTime;
  }

  static updateTime() {
    this.currentFrame = Date.now();
    this.deltaTime = (this.currentFrame - this.lastFrame) / 1000;
    this.lastFrame = this.currentFrame;
  }

  /**
   * 输出消息
   * @param message 字符串
   */
  static printMessage(message) {
    console.log(message);
  }

  static lastUpdate = Date.now();

  static currentFrame = 0.0;
  static lastFrame = 0.0;
  static deltaTime = 0.0;
}
