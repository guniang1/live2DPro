/**
 * 遍历 public/Resources/background，按文件名排序后重写 background_order.json。
 * 在 npm start / build 前由 package.json 调用（浏览器端无法枚举本地目录）。
 */
'use strict';

const fs = require('fs');
const path = require('path');

const BG_DIR = path.resolve(process.cwd(), 'public/Resources/background');
const OUT_FILE = path.join(BG_DIR, 'background_order.json');
const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.webp']);

function collectImages(dir, rootDir, acc) {
  if (!fs.existsSync(dir)) {
    console.warn('[generate-background-order] 目录不存在，跳过:', dir);
    return;
  }
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const ent of entries) {
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) {
      collectImages(full, rootDir, acc);
    } else if (ent.isFile()) {
      if (ent.name === 'background_order.json') {
        continue;
      }
      const ext = path.extname(ent.name).toLowerCase();
      if (!IMAGE_EXTS.has(ext)) {
        continue;
      }
      const rel = path.relative(rootDir, full).split(path.sep).join('/');
      acc.push(rel);
    }
  }
}

const images = [];
collectImages(BG_DIR, BG_DIR, images);
images.sort((a, b) => a.localeCompare(b, undefined, { sensitivity: 'base' }));

const payload = JSON.stringify({ images }, null, 2) + '\n';
fs.mkdirSync(BG_DIR, { recursive: true });
fs.writeFileSync(OUT_FILE, payload, 'utf8');

console.log(
  `[generate-background-order] 已写入 ${images.length} 条 -> ${path.relative(process.cwd(), OUT_FILE)}`
);
