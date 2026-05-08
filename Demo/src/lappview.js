/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */

import { CubismMatrix44 } from '@framework/math/cubismmatrix44';
import { CubismViewMatrix } from '@framework/math/cubismviewmatrix';

import * as LAppDefine from './lappdefine.js';
import { LAppPal } from './lapppal.js';
import { LAppSprite } from './lappsprite.js';
import { TouchManager } from './touchmanager.js';
import { LAppSubdelegate } from './lappsubdelegate.js';

/** @param {string} rel */
function filenameStemFromBgPath(rel) {
  const s = String(rel || '').replace(/\\/g, '/');
  const base = s.split('/').filter(Boolean).pop() || s;
  const dot = base.lastIndexOf('.');
  return dot > 0 ? base.slice(0, dot) : base;
}

function syncLocalBackgroundDisplayName(relOrAbs) {
  const s = String(relOrAbs || '').trim();
  if (
    !s ||
    s.startsWith('http://') ||
    s.startsWith('https://') ||
    LAppDefine.backgroundCycle.remoteRandom
  ) {
    return;
  }
  LAppDefine.backgroundCycle.displayName = filenameStemFromBgPath(s);
}

/** @param {string} relOrAbs @param {string} resourcesPath */
function resolveBackgroundFullUrl(relOrAbs, resourcesPath) {
  const s = String(relOrAbs || '').trim();
  if (!s) {
    return resourcesPath + LAppDefine.getBackImageName();
  }
  if (s.startsWith('http://') || s.startsWith('https://')) {
    return s;
  }
  return resourcesPath + s;
}

/**
 * 绘制类
 */
export class LAppView {
  constructor() {
    this._programId = null;
    this._back = null;
    this._gear = null;
    this._currentBgIndex = 0;
    this._backgroundFullUrl = '';
    this._touchManager = new TouchManager();
    this._deviceToScreen = new CubismMatrix44();
    this._viewMatrix = new CubismViewMatrix();

    this._longPressTimer = null;
    this._pointerDown = false;
    this._pendingLongPress = false;
    this._modelTranslateActive = false;
    this._modelDragDidMove = false;
    this._longPressFired = false;
    this._pointerStartedOnModel = false;
    this._prevViewX = 0.0;
    this._prevViewY = 0.0;
    this._lastDeviceX = 0.0;
    this._lastDeviceY = 0.0;
  }

  _clearLongPressTimer() {
    if (this._longPressTimer != null) {
      clearTimeout(this._longPressTimer);
      this._longPressTimer = null;
    }
  }

  initialize(subdelegate) {
    this._subdelegate = subdelegate;
    const { width, height } = subdelegate.getCanvas();

    // 以浏览器视口为标准：按宽高比 ratio 换算逻辑边界
    const ratio = width / height;
    const left = LAppDefine.ViewLogicalLeft * ratio;
    const right = LAppDefine.ViewLogicalRight * ratio;
    const bottom = LAppDefine.ViewLogicalBottom;
    const top = LAppDefine.ViewLogicalTop;

    this._viewMatrix.setScreenRect(left, right, bottom, top);
    this._viewMatrix.scale(LAppDefine.ViewScale, LAppDefine.ViewScale);

    this._deviceToScreen.loadIdentity();
    if (width > height) {
      const screenW = Math.abs(right - left);
      this._deviceToScreen.scaleRelative(screenW / width, -screenW / width);
    } else {
      const screenH = Math.abs(top - bottom);
      this._deviceToScreen.scaleRelative(screenH / height, -screenH / height);
    }
    this._deviceToScreen.translateRelative(-width * 0.5, -height * 0.5);

    this._viewMatrix.setMaxScale(LAppDefine.ViewMaxScale);
    this._viewMatrix.setMinScale(LAppDefine.ViewMinScale);
    this._viewMatrix.setMaxScreenRect(
      LAppDefine.ViewLogicalMaxLeft * ratio,
      LAppDefine.ViewLogicalMaxRight * ratio,
      LAppDefine.ViewLogicalMaxBottom,
      LAppDefine.ViewLogicalMaxTop
    );
  }

  release() {
    this._clearLongPressTimer();
    this._viewMatrix = null;
    this._touchManager = null;
    this._deviceToScreen = null;

    if (this._gear) {
      this._gear.release();
    }
    this._gear = null;

    this._back.release();
    this._back = null;

    this._subdelegate.getGlManager().getGl().deleteProgram(this._programId);
    this._programId = null;
  }

  render() {
    this._subdelegate.getGlManager().getGl().useProgram(this._programId);

    if (this._back) {
      this._back.render(this._programId);
    }
    if (this._gear) {
      this._gear.render(this._programId);
    }

    this._subdelegate.getGlManager().getGl().flush();

    const lapplive2dmanager = this._subdelegate.getLive2DManager();
    if (lapplive2dmanager != null) {
      lapplive2dmanager.setViewMatrix(this._viewMatrix);
      lapplive2dmanager.onUpdate();
    }
  }

  initializeSprite() {
    const width = this._subdelegate.getCanvas().width;
    const height = this._subdelegate.getCanvas().height;
    const textureManager = this._subdelegate.getTextureManager();
    const resourcesPath = LAppDefine.ResourcesPath;

    const paths = LAppDefine.backgroundCycle.paths;
    const backImageName =
      paths && paths.length > 0
        ? paths[this._currentBgIndex]
        : LAppDefine.getBackImageName();
    this._backgroundFullUrl = resolveBackgroundFullUrl(
      backImageName,
      resourcesPath
    );
    syncLocalBackgroundDisplayName(backImageName);

    const initBackGroundTexture = (textureInfo) => {
      const x = width * 0.5;
      const y = height * 0.5;
      const fwidth = textureInfo.width * 2.0;
      const fheight = height * 0.95;
      this._back = new LAppSprite(x, y, fwidth, fheight, textureInfo.id);
      this._back.setSubdelegate(this._subdelegate);
    };

    textureManager.createTextureFromPngFile(
      this._backgroundFullUrl,
      false,
      initBackGroundTexture
    );

    // 切换模型已改为页面上的 HTML 按钮（见 index.html），不再绘制画布齿轮

    if (this._programId == null) {
      this._programId = this._subdelegate.createShader();
    }
  }

  getBackgroundCycleLabel() {
    if (LAppDefine.backgroundCycle.remoteRandom && LAppDefine.backgroundCycle.displayName) {
      return LAppDefine.backgroundCycle.displayName;
    }
    const paths = LAppDefine.backgroundCycle.paths;
    if (!paths || paths.length === 0) {
      return LAppDefine.getBackImageName().split('/').pop() || '';
    }
    const rel = paths[this._currentBgIndex];
    if (String(rel).startsWith('http://') || String(rel).startsWith('https://')) {
      try {
        const u = new URL(rel);
        const seg = u.pathname.split('/').filter(Boolean).pop();
        return seg || rel;
      } catch {
        return rel;
      }
    }
    return rel.split('/').pop() || rel;
  }

  applyBackgroundFullUrl(fullUrl) {
    const next = String(fullUrl || '').trim();
    if (!next) {
      return;
    }
    const textureManager = this._subdelegate.getTextureManager();
    const width = this._subdelegate.getCanvas().width;
    const height = this._subdelegate.getCanvas().height;
    const fullOld = this._backgroundFullUrl;

    textureManager.createTextureFromPngFile(next, false, (textureInfo) => {
      const x = width * 0.5;
      const y = height * 0.5;
      const fwidth = textureInfo.width * 2.0;
      const fheight = height * 0.95;

      if (this._back) {
        this._back.releaseGeometryOnly();
        textureManager.releaseTextureByFilePath(fullOld);
      }

      this._back = new LAppSprite(x, y, fwidth, fheight, textureInfo.id);
      this._back.setSubdelegate(this._subdelegate);
      this._currentBgIndex = 0;
      this._backgroundFullUrl = next;
    });
  }

  cycleBackground() {
    const paths = LAppDefine.backgroundCycle.paths;
    if (!paths || paths.length < 2) {
      return;
    }

    const resourcesPath = LAppDefine.ResourcesPath;
    const nextIndex = (this._currentBgIndex + 1) % paths.length;
    const nextRel = paths[nextIndex];
    const fullNext = resolveBackgroundFullUrl(nextRel, resourcesPath);
    const fullOld = this._backgroundFullUrl;

    const textureManager = this._subdelegate.getTextureManager();
    const width = this._subdelegate.getCanvas().width;
    const height = this._subdelegate.getCanvas().height;

    textureManager.createTextureFromPngFile(fullNext, false, (textureInfo) => {
      const x = width * 0.5;
      const y = height * 0.5;
      const fwidth = textureInfo.width * 2.0;
      const fheight = height * 0.95;

      if (this._back) {
        this._back.releaseGeometryOnly();
        textureManager.releaseTextureByFilePath(fullOld);
      }

      this._back = new LAppSprite(x, y, fwidth, fheight, textureInfo.id);
      this._back.setSubdelegate(this._subdelegate);
      this._currentBgIndex = nextIndex;
      this._backgroundFullUrl = fullNext;
      syncLocalBackgroundDisplayName(nextRel);
    });
  }

  onTouchesBegan(pointX, pointY) {
    const posX = pointX * window.devicePixelRatio;
    const posY = pointY * window.devicePixelRatio;

    this._clearLongPressTimer();
    this._pointerDown = true;
    this._modelTranslateActive = false;
    this._modelDragDidMove = false;
    this._longPressFired = false;
    this._pendingLongPress = false;
    this._pointerStartedOnModel = false;

    this._touchManager.touchesBegan(posX, posY);

    this._lastDeviceX = posX;
    this._lastDeviceY = posY;

    const lapplive2dmanager = this._subdelegate.getLive2DManager();
    const vx = this.transformViewX(posX);
    const vy = this.transformViewY(posY);

    if (lapplive2dmanager.isPointOnModel(vx, vy)) {
      this._pointerStartedOnModel = true;
      this._pendingLongPress = true;

      this._longPressTimer = setTimeout(() => {
        this._longPressTimer = null;
        if (!this._pointerDown || !this._pendingLongPress) {
          return;
        }
        this._pendingLongPress = false;
        this._modelTranslateActive = true;
        this._longPressFired = true;
        lapplive2dmanager.onDrag(0.0, 0.0);
        const px = this.transformViewX(this._lastDeviceX);
        const py = this.transformViewY(this._lastDeviceY);
        this._prevViewX = px;
        this._prevViewY = py;
      }, LAppDefine.LongPressModelMs);
    }
  }

  onTouchesMoved(pointX, pointY) {
    const posX = pointX * window.devicePixelRatio;
    const posY = pointY * window.devicePixelRatio;

    this._lastDeviceX = posX;
    this._lastDeviceY = posY;

    const lapplive2dmanager = this._subdelegate.getLive2DManager();
    const slopDevice =
      LAppDefine.LongPressSlopPx * window.devicePixelRatio;

    if (this._modelTranslateActive) {
      const viewX = this.transformViewX(posX);
      const viewY = this.transformViewY(posY);
      const dx = viewX - this._prevViewX;
      const dy = viewY - this._prevViewY;
      this._prevViewX = viewX;
      this._prevViewY = viewY;

      lapplive2dmanager.translateModelByViewDelta(dx, dy);
      if (dx * dx + dy * dy > 1e-12) {
        this._modelDragDidMove = true;
      }

      this._touchManager.touchesMoved(posX, posY);
      return;
    }

    if (this._pendingLongPress && this._pointerStartedOnModel) {
      const sx = this._touchManager.getStartX();
      const sy = this._touchManager.getStartY();
      const dist = Math.sqrt(
        (posX - sx) * (posX - sx) + (posY - sy) * (posY - sy)
      );
      if (dist > slopDevice) {
        this._clearLongPressTimer();
        this._pendingLongPress = false;
      } else {
        this._touchManager.touchesMoved(posX, posY);
        return;
      }
    }

    const viewX = this.transformViewX(this._touchManager.getX());
    const viewY = this.transformViewY(this._touchManager.getY());

    this._touchManager.touchesMoved(posX, posY);

    lapplive2dmanager.onDrag(viewX, viewY);
  }

  /**
   * @param {boolean} [suppressTap] 为 true 时不调用 onTap（DOM 覆盖层上的交互）
   */
  onTouchesEnded(pointX, pointY, suppressTap = false) {
    const posX = pointX * window.devicePixelRatio;
    const posY = pointY * window.devicePixelRatio;

    this._clearLongPressTimer();
    this._pointerDown = false;

    const lapplive2dmanager = this._subdelegate.getLive2DManager();

    lapplive2dmanager.onDrag(0.0, 0.0);

    const x = this.transformViewX(posX);
    const y = this.transformViewY(posY);

    if (LAppDefine.DebugTouchLogEnable) {
      LAppPal.printMessage(`[APP] 触摸结束 x: ${x} y: ${y}`);
    }

    const skipTap =
      suppressTap || this._modelDragDidMove || this._longPressFired;
    if (!skipTap) {
      lapplive2dmanager.onTap(x, y);
    }

    this._modelTranslateActive = false;
    this._pendingLongPress = false;
    this._modelDragDidMove = false;
    this._longPressFired = false;
    this._pointerStartedOnModel = false;
  }

  onWheel(pointX, pointY, deltaY) {
    const posX = pointX * window.devicePixelRatio;
    const posY = pointY * window.devicePixelRatio;

    const viewX = this.transformViewX(posX);
    const viewY = this.transformViewY(posY);
    const scale = deltaY < 0 ? 1.08 : 0.92;

    this._viewMatrix.adjustScale(viewX, viewY, scale);
  }

  transformViewX(deviceX) {
    const screenX = this._deviceToScreen.transformX(deviceX);
    return this._viewMatrix.invertTransformX(screenX);
  }

  transformViewY(deviceY) {
    const screenY = this._deviceToScreen.transformY(deviceY);
    return this._viewMatrix.invertTransformY(screenY);
  }

  transformScreenX(deviceX) {
    return this._deviceToScreen.transformX(deviceX);
  }

  transformScreenY(deviceY) {
    return this._deviceToScreen.transformY(deviceY);
  }
}
