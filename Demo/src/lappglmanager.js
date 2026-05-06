/**
 WebGL管理器，负责管理WebGL上下文
 调用场景：页面加载时
 */

/**
 * Cubism SDK 示例中用于管理 WebGL 的类
 */
export class LAppGlManager {
  constructor() {
    this._gl = null;
  }

  initialize(canvas) {
    // 初始化 gl 上下文
    this._gl = canvas.getContext('webgl2');

    if (!this._gl) {
      // gl 初始化失败
      alert('无法初始化 WebGL，当前浏览器不支持。');
      this._gl = null;
      return false;
    }
    return true;
  }

  /**
   * 释放资源
   */
  release() {}

  getGl() {
    return this._gl;
  }
}
