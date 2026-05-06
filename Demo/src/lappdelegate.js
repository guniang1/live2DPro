/**
 该文件是应用的主类，负责初始化应用并运行应用实例

 「应用主类」：LAppDelegate，负责统一初始化 Cubism SDK、初始化监听器、初始化子代理、处理指针和窗口 resize 等。
 「应用实例」：通过 LAppDelegate.getInstance() 拿到的那个单例，代表「当前正在运行的这一个应用」。
 「应用」通常就是指：从页面加载、初始化、渲染模型、响应用户操作，直到页面关闭的这一整条链路。
  所以可以简单记：应用 = 这个 Live2D 示例网页应用本身（包含入口、Delegate、子代理、画布、模型等）。
 */

import { csmVector } from '@framework/type/csmvector';
import { CubismFramework, Option } from '@framework/live2dcubismframework';
import * as LAppDefine from './lappdefine.js';
import { LAppPal } from './lapppal.js';
import { LAppSubdelegate } from './lappsubdelegate.js';
import { CubismLogError } from '@framework/utils/cubismdebug';

export let s_instance = null;

/** 文字对话框相关 DOM：不参与 Live2D 画布坐标与 onTap（含右侧聊天与左侧台词气泡） */
function isLive2dOverlayPointerTarget(target) {
  if (!target || typeof target.closest !== 'function') {
    return false;
  }
  return Boolean(target.closest('#chat-panel'));
}

/**
 * 应用主类
 * 负责 Cubism SDK 的初始化与调度。
 */
export class LAppDelegate {
  /**
   * 构造函数
   */
  constructor() {
    // this._cubismOption 是 一个 Option 的实例
    this._cubismOption = new Option();

    // this._subdelegates 是 一个 csmVector 的实例
    this._subdelegates = new csmVector();

    // this._canvases 是 一个 csmVector 的实例
    this._canvases = new csmVector();

    /** 本轮指针是否在画布区按下（用于抬起点落在 UI 上时仍收尾状态并抑制 onTap） */
    this._canvasPointerDown = false;
  }
  // 获取应用实例
  static getInstance() {
    // 如果应用实例不存在，则创建应用实例
    if (s_instance == null) {
      s_instance = new LAppDelegate();
    }
    // 返回应用实例
    return s_instance;
  }

  // 释放应用实例，当应用实例不再需要时调用
  static releaseInstance() {
    // 如果应用实例存在，则释放应用实例
    if (s_instance != null) {
      s_instance.release();
    }
    s_instance = null;
  }

  /**
   *  当鼠标按下时调用，用于处理鼠标按下事件
   *  e是鼠标事件对象
   *  遍历所有子代理，调用子代理的onPointBegan方法
   * @param {*} e 鼠标事件对象
   */
  onPointerBegan(e) {
    if (isLive2dOverlayPointerTarget(e.target)) {
      return;
    }
    this._canvasPointerDown = true;
    for (
      let ite = this._subdelegates.begin();
      ite.notEqual(this._subdelegates.end());
      ite.preIncrement()
    ) {
      ite.ptr().onPointBegan(e.pageX, e.pageY);
    }
  }
  /**
   * 当鼠标移动时调用，用于处理鼠标移动事件
   * @param {*} e 
   */
  onPointerMoved(e) {
    if (!this._canvasPointerDown) {
      return;
    }
    for (
      let ite = this._subdelegates.begin();
      ite.notEqual(this._subdelegates.end());
      ite.preIncrement()
    ) {
      ite.ptr().onPointMoved(e.pageX, e.pageY);
    }
  }

  /**
   * 当鼠标(点击之后)抬起时调用，用于处理鼠标抬起事件
   * 目前只用于处理点击身体时，模型会旋转到正面
   * @param {*} e 鼠标事件对象
   */
  onPointerEnded(e) {
    if (!this._canvasPointerDown) {
      return;
    }
    this._canvasPointerDown = false;
    const suppressTap = isLive2dOverlayPointerTarget(e.target);
    for (
      let ite = this._subdelegates.begin();
      ite.notEqual(this._subdelegates.end());
      ite.preIncrement()
    ) {
      ite.ptr().onPointEnded(e.pageX, e.pageY, suppressTap);
    }
  }

  /**
   * 当鼠标取消时调用，用于处理鼠标取消事件
   * 在发生「指针/触摸被取消」时，通知所有子代理结束触摸并释放捕获状态。
   * 和「正常抬起」的 pointerend 不同，pointercancel 表示这次触摸/指针被系统中途取消，例如：
      来电、弹窗、切换应用
      手势被识别成页面滚动
      指针移出窗口、多指冲突等
      也就是说：用户并没有「正常松手」，但这次触摸已经不再算数了。
   * @param {*} e 鼠标事件对象
   */
  onPointerCancel(e) {
    if (!this._canvasPointerDown) {
      return;
    }
    this._canvasPointerDown = false;
    for (
      let ite = this._subdelegates.begin();
      ite.notEqual(this._subdelegates.end());
      ite.preIncrement()
    ) {
      ite.ptr().onTouchCancel(e.pageX, e.pageY);
    }
  }

  /**
   * 当窗口大小改变时调用，用于处理窗口大小改变事件
   */
  onResize() {
    for (let i = 0; i < this._subdelegates.getSize(); i++) {
      this._subdelegates.at(i).onResize();
      console.log('onResize', i);
    }
  }
  /**
   * 运行应用，负责更新模型和渲染模型，动画帧循环
   */
  run() {

    // loop函数是动画帧循环的回调函数
    // 负责更新模型，渲染模型，动画帧循环
    // 循环调用loop函数，直到应用实例不存在
    const loop = () => {
      // 如果应用实例不存在，则返回
      if (s_instance == null) {
        return;
      }
      // 更新时间
      LAppPal.updateTime();

      // 更新模型
      // 遍历所有子代理，调用子代理的update方法
      // update方法负责更新模型，渲染模型，动画帧循环
      for (let i = 0; i < this._subdelegates.getSize(); i++) {
        this._subdelegates.at(i).update();
      }

      // 请求动画帧，requestAnimationFrame循环调用loop函数
      requestAnimationFrame(loop);
    };


    // 第一次调用loop函数，开始动画帧循环
    loop();
  }

  /**
   * 释放应用，负责释放应用资源
   * 调用场景：页面关闭时
   */
  release() {
    // 调用释放事件监听器方法，释放事件监听器资源
    this.releaseEventListener();
    // 调用释放子代理方法，释放子代理资源
    this.releaseSubdelegates();
    // 调用释放Cubism SDK方法，释放Cubism SDK资源
    CubismFramework.dispose();
    // 释放Cubism SDK选项
    this._cubismOption = null;
  }

  /**
   * 释放事件监听器方法，统一处理鼠标抬起、移动、按下、取消事件监听器
   * 调用场景：页面关闭时
   */
  releaseEventListener() {
    // 移除鼠标抬起事件监听器
    document.removeEventListener('pointerup', this.pointBeganEventListener);
    this.pointBeganEventListener = null;
    // 移除鼠标移动事件监听器
    document.removeEventListener('pointermove', this.pointMovedEventListener);
    this.pointMovedEventListener = null;
    // 移除鼠标抬起事件监听器
    document.removeEventListener('pointerdown', this.pointEndedEventListener);
    this.pointEndedEventListener = null;
    // 移除鼠标取消事件监听器
    document.removeEventListener('pointerdown', this.pointCancelEventListener);
    this.pointCancelEventListener = null;
  }

  /**
   * 释放子代理，负责释放子代理资源
   * 调用场景：页面关闭时
   * 和
   */
  releaseSubdelegates() {
    for (
      let ite = this._subdelegates.begin();
      ite.notEqual(this._subdelegates.end());
      ite.preIncrement()
    ) {
      ite.ptr().release();
    }
    this._subdelegates.clear();
    this._subdelegates = null;
  }

  /**
   * 初始化应用，负责初始化Cubism SDK、子代理、事件监听器
   * 调用场景：页面加载时
   */
  initialize() {
    this.initializeCubism();//初始化Cubism SDK
    this.initializeSubdelegates();//初始化子代理
    this.initializeEventListener();//初始化事件监听器
    return true;
  }

  /**
   * 初始化事件监听器方法，负责初始化鼠标抬起、移动、按下、取消事件监听器
   * 调用场景：页面加载时
   */
  initializeEventListener() {
    // this.onPointerBegan是函数
    // 当前this是LAppDelegate的实例
    // .bind(this)是绑定this对象到onPointerBegan方法
    // onPointerBegan 里用到了“当前这个 LAppDelegate 实例”的数据，所以必须保证被浏览器调用时，函数里的 this 还是这个实例。
    // 绑定 this 是因为回调里必须用到“当前 LAppDelegate 实例”（例如 _subdelegates），而浏览器调用回调时不会自动把我们的对象当作 this，所以要在注册时用 .bind(this) 把“正确的 this”锁进去。
    this.pointBeganEventListener = this.onPointerBegan.bind(this);
    this.pointMovedEventListener = this.onPointerMoved.bind(this);
    this.pointEndedEventListener = this.onPointerEnded.bind(this);
    this.pointCancelEventListener = this.onPointerCancel.bind(this);

    // 注册事件监听器
    document.addEventListener('pointerdown', this.pointBeganEventListener, {
      passive: true
    });
    document.addEventListener('pointermove', this.pointMovedEventListener, {
      passive: true
    });
    document.addEventListener('pointerup', this.pointEndedEventListener, {
      passive: true
    });
    document.addEventListener('pointercancel', this.pointCancelEventListener, {
      passive: true
    });
  }

  /**
   * 初始化Cubism SDK，负责初始化Cubism SDK
   */
  initializeCubism() {
    LAppPal.updateTime();
    this._cubismOption.logFunction = LAppPal.printMessage;
    this._cubismOption.loggingLevel = LAppDefine.CubismLoggingLevel;
    CubismFramework.startUp(this._cubismOption);
    CubismFramework.initialize();
  }

  /**
   * 初始化子代理，负责初始化子代理
   */
  initializeSubdelegates() {
    // 计算画布的宽度和高度
    let width = 100;
    let height = 100;

    // 如果画布数量大于3，则
    if (LAppDefine.CanvasNum > 3) {
      const widthunit = Math.ceil(Math.sqrt(LAppDefine.CanvasNum));
      const heightUnit = Math.ceil(LAppDefine.CanvasNum / widthunit);
      width = 100.0 / widthunit;
      height = 100.0 / heightUnit;
    } else {
      // 如果画布数量小于等于3，则计算画布的宽度和高度
      width = 100.0 / LAppDefine.CanvasNum;
    }

    // [SDK] 为画布和子代理容器"预分配容量"
    this._canvases.prepareCapacity(LAppDefine.CanvasNum);
    this._subdelegates.prepareCapacity(LAppDefine.CanvasNum);

    for (let i = 0; i < LAppDefine.CanvasNum; i++) {
      // [内置] 创建 canvas 元素
      const canvas = document.createElement('canvas');
      // [SDK] 将画布加入 csmVector 容器
      this._canvases.pushBack(canvas);
      // [内置] 设置画布样式宽高（视口单位）
      canvas.style.width = `${width}vw`;
      canvas.style.height = `${height}vh`;
      // [内置] 将画布挂载到 body
      document.body.appendChild(canvas);
    }

    for (let i = 0; i < this._canvases.getSize(); i++) {
      // [自定义] 创建子代理实例
      const subdelegate = new LAppSubdelegate();
      // [自定义] 子代理初始化，传入对应画布
      subdelegate.initialize(this._canvases.at(i));
      // [SDK] 将子代理加入容器
      this._subdelegates.pushBack(subdelegate);
    }

    for (let i = 0; i < LAppDefine.CanvasNum; i++) {
      // [自定义] 检查该画布对应的 WebGL 上下文是否丢失
      if (this._subdelegates.at(i).isContextLost()) {
        // [SDK] 输出错误日志
        CubismLogError(
          `The context for Canvas at index ${i} was lost, possibly because the acquisition limit for WebGLRenderingContext was reached.`
        );
      }
    }
  }

  getFirstSubdelegate() {
    if (this._subdelegates == null || this._subdelegates.getSize() < 1) {
      return null;
    }
    return this._subdelegates.at(0);
  }

  cycleBackground() {
    for (let i = 0; i < this._subdelegates.getSize(); i++) {
      this._subdelegates.at(i).getView().cycleBackground();
    }
  }

  /** 切换到 ModelDir 中的下一个模型（与原先画布齿轮按钮行为一致） */
  nextModel() {
    for (let i = 0; i < this._subdelegates.getSize(); i++) {
      this._subdelegates.at(i).getLive2DManager().nextScene();
    }
  }

  getCurrentModelLabel() {
    const sd = this.getFirstSubdelegate();
    if (!sd) {
      return '';
    }
    return sd.getLive2DManager().getCurrentModelDirName();
  }

}

/**
 * 将聊天流首条 chunk 中的表情/动作标识应用到当前模型（需在 LAppDelegate.initialize 之后调用）。
 */
export function applyChatLive2dActions(expression, motion) {
  const inst = LAppDelegate.getInstance();
  const sd = inst.getFirstSubdelegate();
  if (!sd) {
    return;
  }
  sd.getLive2DManager().applyChatLive2dActions(expression, motion);
}

/** 立即停止聊天口型。 */
export function stopChatLive2dLipSync() {
  const inst = LAppDelegate.getInstance();
  const sd = inst.getFirstSubdelegate();
  if (!sd) {
    return;
  }
  sd.getLive2DManager().stopLipSync();
}

/** TTS 朗读时由 Web Audio Analyser 按帧写入口型强度 0..1。 */
export function feedChatLive2dTtsAudioLipLevel(level) {
  const inst = LAppDelegate.getInstance();
  const sd = inst.getFirstSubdelegate();
  if (!sd) {
    return;
  }
  sd.getLive2DManager().feedTtsAudioLipLevel(level);
}

/** 聊天一轮结束后恢复默认表情。 */
export function resetChatLive2dExpression() {
  const inst = LAppDelegate.getInstance();
  const sd = inst.getFirstSubdelegate();
  if (!sd) {
    return;
  }
  sd.getLive2DManager().resetToDefaultExpression();
}
