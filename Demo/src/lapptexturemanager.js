/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */

import { csmVector } from '@framework/type/csmvector';
import { LAppGlManager } from './lappglmanager.js';

/**
 * 远程 PNG（如 MinIO 预签名 URL）与页面不同源时，必须 anonymous CORS 才能在 WebGL texImage2D 中使用；
 * 对象存储需对该 Origin 返回 Access-Control-Allow-Origin。
 */
function applyCrossOriginForWebGLTexture(img, src) {
  if (!src || typeof src !== 'string') {
    return;
  }
  try {
    const abs = new URL(src, window.location.href);
    if (
      (abs.protocol === 'http:' || abs.protocol === 'https:') &&
      abs.origin !== window.location.origin
    ) {
      img.crossOrigin = 'anonymous';
    }
  } catch {
    // data: / 异常 URL：不设置
  }
}

/**
 * 纹理管理类
 * 负责图片加载与管理。
 */
export class LAppTextureManager {
  constructor() {
    this._textures = new csmVector();
  }

  release() {
    for (
      let ite = this._textures.begin();
      ite.notEqual(this._textures.end());
      ite.preIncrement()
    ) {
      this._glManager.getGl().deleteTexture(ite.ptr().id);
    }
    this._textures = null;
  }

  createTextureFromPngFile(fileName, usePremultiply, callback) {
    for (
      let ite = this._textures.begin();
      ite.notEqual(this._textures.end());
      ite.preIncrement()
    ) {
      if (
        ite.ptr().fileName == fileName &&
        ite.ptr().usePremultply == usePremultiply
      ) {
        ite.ptr().img = new Image();
        applyCrossOriginForWebGLTexture(ite.ptr().img, fileName);
        ite
          .ptr()
          .img.addEventListener('load', () => callback(ite.ptr()), {
            passive: true
          });
        ite.ptr().img.src = fileName;
        return;
      }
    }

    const img = new Image();
    applyCrossOriginForWebGLTexture(img, fileName);
    img.addEventListener(
      'load',
      () => {
        const tex = this._glManager.getGl().createTexture();

        this._glManager
          .getGl()
          .bindTexture(this._glManager.getGl().TEXTURE_2D, tex);

        this._glManager
          .getGl()
          .texParameteri(
            this._glManager.getGl().TEXTURE_2D,
            this._glManager.getGl().TEXTURE_MIN_FILTER,
            this._glManager.getGl().LINEAR_MIPMAP_LINEAR
          );
        this._glManager
          .getGl()
          .texParameteri(
            this._glManager.getGl().TEXTURE_2D,
            this._glManager.getGl().TEXTURE_MAG_FILTER,
            this._glManager.getGl().LINEAR
          );

        if (usePremultiply) {
          this._glManager
            .getGl()
            .pixelStorei(
              this._glManager.getGl().UNPACK_PREMULTIPLY_ALPHA_WEBGL,
              1
            );
        }

        this._glManager
          .getGl()
          .texImage2D(
            this._glManager.getGl().TEXTURE_2D,
            0,
            this._glManager.getGl().RGBA,
            this._glManager.getGl().RGBA,
            this._glManager.getGl().UNSIGNED_BYTE,
            img
          );

        this._glManager
          .getGl()
          .generateMipmap(this._glManager.getGl().TEXTURE_2D);

        this._glManager
          .getGl()
          .bindTexture(this._glManager.getGl().TEXTURE_2D, null);

        const textureInfo = new TextureInfo();
        if (textureInfo != null) {
          textureInfo.fileName = fileName;
          textureInfo.width = img.width;
          textureInfo.height = img.height;
          textureInfo.id = tex;
          textureInfo.img = img;
          textureInfo.usePremultply = usePremultiply;
          if (this._textures != null) {
            this._textures.pushBack(textureInfo);
          }
        }

        callback(textureInfo);
      },
      { passive: true }
    );
    img.src = fileName;
  }

  releaseTextures() {
    for (let i = 0; i < this._textures.getSize(); i++) {
      this._glManager.getGl().deleteTexture(this._textures.at(i).id);
      this._textures.set(i, null);
    }
    this._textures.clear();
  }

  releaseTextureByTexture(texture) {
    for (let i = 0; i < this._textures.getSize(); i++) {
      if (this._textures.at(i).id != texture) {
        continue;
      }
      this._glManager.getGl().deleteTexture(this._textures.at(i).id);
      this._textures.set(i, null);
      this._textures.remove(i);
      break;
    }
  }

  releaseTextureByFilePath(fileName) {
    for (let i = 0; i < this._textures.getSize(); i++) {
      if (this._textures.at(i).fileName == fileName) {
        this._glManager.getGl().deleteTexture(this._textures.at(i).id);
        this._textures.set(i, null);
        this._textures.remove(i);
        break;
      }
    }
  }

  setGlManager(glManager) {
    this._glManager = glManager;
  }
}

export class TextureInfo {
  img = null;
  id = null;
  width = 0;
  height = 0;
  usePremultply = false;
  fileName = null;
}
