/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */

import { CubismMatrix44 } from '@framework/math/cubismmatrix44';
import { InvalidMotionQueueEntryHandleValue } from '@framework/motion/cubismmotionqueuemanager';
import { csmVector } from '@framework/type/csmvector';

import * as LAppDefine from './lappdefine.js';
import { LAppModel } from './lappmodel.js';
import { LAppPal } from './lapppal.js';
import { LAppSubdelegate } from './lappsubdelegate.js';

/**
 * 示例应用中管理 Cubism 模型的类
 */
export class LAppLive2DManager {
  releaseAllModel() {
    this._models.clear();
  }

  onDrag(x, y) {
    const model = this._models.at(0);
    if (model) {
      model.setDragging(x, y);
    }
  }

  /**
   * 视图逻辑坐标 (x,y) 是否落在模型可交互区域内（与 onTap 命中规则一致）
   */
  isPointOnModel(x, y) {
    const model = this._models.at(0);
    if (!model || model.getModel() == null) {
      return false;
    }

    if (
      model.hitTest(LAppDefine.HitAreaNameHead, x, y) ||
      model.hitTest(LAppDefine.HitAreaNameBody, x, y)
    ) {
      return true;
    }

    const ratio =
      this._subdelegate.getCanvas().width /
      this._subdelegate.getCanvas().height;
    const halfW = ratio * 0.8;
    const halfH = 0.8;
    return -halfW <= x && x <= halfW && -halfH <= y && y <= halfH;
  }

  /** 按视图坐标增量平移模型矩阵 */
  translateModelByViewDelta(dx, dy) {
    const model = this._models.at(0);
    if (model && model.getModel() != null) {
      model.getModelMatrix().translateRelative(dx, dy);
    }
  }

  onTap(x, y) {
    if (LAppDefine.DebugLogEnable) {
      LAppPal.printMessage(
        `[APP] 点击坐标: {x: ${x.toFixed(2)} y: ${y.toFixed(2)}}`
      );
    }

    const model = this._models.at(0);
    if (!model) return;

    if (!this.isPointOnModel(x, y)) {
      if (LAppDefine.DebugLogEnable) {
        LAppPal.printMessage(
          `[APP] 未命中任何区域 (x:${x.toFixed(2)} y:${y.toFixed(2)})`
        );
      }
      return;
    }

    if (model.hitTest(LAppDefine.HitAreaNameHead, x, y)) {
      if (LAppDefine.DebugLogEnable) {
        LAppPal.printMessage(`[APP] 命中区域: [${LAppDefine.HitAreaNameHead}]`);
      }
      model.setRandomExpression();
    } else if (model.hitTest(LAppDefine.HitAreaNameBody, x, y)) {
      if (LAppDefine.DebugLogEnable) {
        LAppPal.printMessage(`[APP] 命中区域: [${LAppDefine.HitAreaNameBody}]`);
      }
      model.startRandomMotion(
        LAppDefine.MotionGroupTapBody,
        LAppDefine.PriorityNormal,
        this.finishedMotion,
        this.beganMotion
      );
    } else {
      // 备用：HitAreas 未命中时，按 y 坐标区分：上半部分=表情，下半部分=动作
      if (y > 0.15) {
        if (LAppDefine.DebugLogEnable) {
          LAppPal.printMessage(
            `[APP] 备用命中: 头部区域 (x:${x.toFixed(2)} y:${y.toFixed(2)})`
          );
        }
        model.setRandomExpression();
      } else {
        if (LAppDefine.DebugLogEnable) {
          LAppPal.printMessage(
            `[APP] 备用命中: 身体区域 (x:${x.toFixed(2)} y:${y.toFixed(2)})`
          );
        }
        model.startRandomMotion(
          LAppDefine.MotionGroupTapBody,
          LAppDefine.PriorityNormal,
          this.finishedMotion,
          this.beganMotion
        );
      }
    }
  }

  onUpdate() {
    const { width, height } = this._subdelegate.getCanvas();

    const projection = new CubismMatrix44();
    const model = this._models.at(0);

    if (!model || !model.getModel()) {
      return;
    }

    if (model.getModel().getCanvasWidth() > 1.0 && width < height) {
      model.getModelMatrix().setWidth(2.0);
      projection.scale(1.0, width / height);
    } else {
      projection.scale(height / width, 1.0);
    }

    if (this._viewMatrix != null) {
      projection.multiplyByMatrix(this._viewMatrix);
    }

    model.update();
    model.draw(projection);
  }

  nextScene() {
    const no = (this._sceneIndex + 1) % LAppDefine.getModelDirSize();
    this.changeScene(no);
  }

  /** 当前模型目录名（与 lappdefine ModelDir 一致） */
  getCurrentModelDirName() {
    return LAppDefine.ModelDir[this._sceneIndex] ?? '';
  }

  changeScene(index) {
    this._sceneIndex = index;

    if (LAppDefine.DebugLogEnable) {
      LAppPal.printMessage(`[APP] 模型索引: ${this._sceneIndex}`);
    }

    if (!LAppDefine.ModelDir || LAppDefine.ModelDir.length === 0) {
      if (LAppDefine.DebugLogEnable) {
        LAppPal.printMessage('[APP] 没有可用的模型');
      }
      this.releaseAllModel();
      return;
    }

    const model = LAppDefine.ModelDir[index];
    const remoteMap = LAppDefine.getRemoteAssetUrlMap(model);

    this.releaseAllModel();
    const instance = new LAppModel();
    instance.setSubdelegate(this._subdelegate);
    if (remoteMap) {
      instance.setRemoteAssetUrlMap(remoteMap);
      const entryJson = LAppDefine.getRemoteEntryModelRelativePath(model);
      instance.loadAssets('', entryJson);
    } else {
      const modelPath = LAppDefine.ResourcesPath + model + '/';
      const modelJsonName = `${model}.model3.json`;
      instance.loadAssets(modelPath, modelJsonName);
    }
    this._models.pushBack(instance);
  }

  setViewMatrix(m) {
    for (let i = 0; i < 16; i++) {
      this._viewMatrix.getArray()[i] = m.getArray()[i];
    }
  }

  addModel(sceneIndex = 0) {
    this._sceneIndex = sceneIndex;
    this.changeScene(this._sceneIndex);
  }

  constructor() {
    this._subdelegate = null;
    this._viewMatrix = new CubismMatrix44();
    this._models = new csmVector();
    this._sceneIndex = 0;
  }

  release() { }

  initialize(subdelegate) {
    this._subdelegate = subdelegate;
    this.changeScene(this._sceneIndex);
  }

  beganMotion = (self) => {
    LAppPal.printMessage('动作开始：');
    console.log(self);
  };

  finishedMotion = (self) => {
    LAppPal.printMessage('动作结束：');
    console.log(self);
  };

  /**
   * 根据 WebSocket 首条 chunk 的 expression / motion 标识驱动 Live2D（与后端 catalog 一致）。
   */
  applyChatLive2dActions(expression, motion) {
    const model = this._models.at(0);
    if (!model) {
      return;
    }
    const exp =
      typeof expression === 'string' ? expression.trim() : '';
    const mot = typeof motion === 'string' ? motion.trim() : '';
    if (exp) {
      model.setExpression(exp);
    }
    if (mot) {
      const handle = model.startMotionByBasename(
        mot,
        LAppDefine.PriorityNormal,
        this.finishedMotion,
        this.beganMotion
      );
      if (handle !== InvalidMotionQueueEntryHandleValue) {
        model.setHoldChatBodyMotionPose(true);
      }
    }
  }

  // 在 applyChatLive2dActions 方法后添加
  /**
   * 聊天口型由 TTS 音频 RMS 驱动（ws.js Web Audio）；此方法保留兼容，不再根据文本对口型。
   * @param {string} text
   */
  applyLipSync(text) {
    void text;
  }

  /** TTS 播放侧按帧传入归一化音量 0..1。 */
  feedTtsAudioLipLevel(level) {
    const model = this._models.at(0);
    if (model && typeof model.setTtsAudioLipLevel === 'function') {
      model.setTtsAudioLipLevel(level);
    }
  }

  /** 立即停止口型（含 TTS 音频驱动）。 */
  stopLipSync() {
    const model = this._models.at(0);
    if (!model) {
      return;
    }
    if (typeof model.clearTtsAudioLip === 'function') {
      model.clearTtsAudioLip(true);
    }
    if (typeof model.stopTextLipSync === 'function') {
      model.stopTextLipSync();
    }
  }

  /** 聊天一轮结束后恢复默认表情。 */
  resetToDefaultExpression() {
    const model = this._models.at(0);
    if (!model || typeof model.resetToDefaultExpression !== 'function') {
      return;
    }
    model.resetToDefaultExpression();
  }

}