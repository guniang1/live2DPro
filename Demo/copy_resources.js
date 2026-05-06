/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */

"use strict";
const fs = require('fs');
const path = require('path');
const publicResources = [
  {src: '../../../Core', dst: './public/Core'},

];

publicResources.forEach((e) => {
  const srcPath = path.resolve(process.cwd(), e.src);
  if (!fs.existsSync(srcPath)) return; // 源不存在则跳过（如 Samples/Resources 未提供时保留现有 public/Resources）
  const dstPath = path.resolve(process.cwd(), e.dst);
  if (fs.existsSync(dstPath)) fs.rmSync(dstPath, { recursive: true });
  fs.cpSync(srcPath, dstPath, { recursive: true });
});
