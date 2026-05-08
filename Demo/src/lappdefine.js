/**
 * 该文件是应用的定义文件，负责定义应用的常量，比如画布大小、模型大小等
 * 
 * CanvasSize是画布宽高像素值，或动态屏幕尺寸（'auto'）
 * CanvasNum是画布数量
 * 
 * ViewScale是画面缩放比例
 * ViewMaxScale是画面最大缩放比例
 * ViewMinScale是画面最小缩放比例
 * 
 * ViewLogicalLeft/Right/Bottom/Top：镜头默认可见区域
 * 单位为逻辑坐标（中心为 0）；在 LAppView 中会按视口宽高比(ratio=width/height)换算：
 * 左右边界 = 本值×ratio，上下边界 = 本值，从而与浏览器视口比例一致。
 *
 * ViewLogicalMaxLeft/Right/Bottom/Top：
 * 用来限制**镜头（视口）**能平移到的范围，可以超出**默认可见区域**：
 * Framework 会用这四个边界把平移结果限制在这个矩形里，防止镜头被拖得太远。
 * 当前这个 Demo 没有做“拖拽平移镜头”，只做了“拖拽移动模型”，所以这四个值虽然被传给了 setMaxScreenRect，但没有任何地方会用到它们，改大改小都看不出区别。。。
 * 
 * ResourcesPath是资源路径
 * getBackImageName() 返回当前默认背景路径（与轮换列表首项一致）
 * GearImageName是齿轮图标文件名
 * PowerImageName是关闭按钮文件名
 * ModelDir是模型所在目录名数组，目录名需与 model3.json 名称一致
 * ModelDirSize是模型所在目录数
 * MotionGroupIdle是待机动作组名
 * MotionGroupTapBody是点击身体时动作组名
 * HitAreaNameHead是头部命中区域名称
 * HitAreaNameBody是身体命中区域名称
 * 
 * PriorityNone是优先级常量
 * PriorityIdle是待机优先级
 * PriorityNormal是普通优先级
 * PriorityForce是强制优先级
 * 
 * MOCConsistencyValidationEnable是MOC一致性校验选项
 * MotionConsistencyValidationEnable是动作一致性校验选项
 * DebugLogEnable是调试日志显示选项
 * DebugTouchLogEnable是触摸日志显示选项
 * CubismLoggingLevel是Framework日志级别
 * RenderTargetWidth是默认渲染目标宽度
 * RenderTargetHeight是默认渲染目标高度
 */

import { LogLevel } from '@framework/live2dcubismframework';// 日志级别

/**
 * 示例应用使用的常量
 */

// 画布宽高像素值，或动态屏幕尺寸（'auto'）
export const CanvasSize = 'auto';

// 画布数量
export const CanvasNum = 1;

// 画面
export const ViewScale = 1.0;
export const ViewMaxScale = 2.0;
export const ViewMinScale = 0.8;

export const ViewLogicalLeft = -1.0;
export const ViewLogicalRight = 1.0;
export const ViewLogicalBottom = -1.0;
export const ViewLogicalTop = 1.0;

export const ViewLogicalMaxLeft = -2.0;
export const ViewLogicalMaxRight = 2.0;
export const ViewLogicalMaxBottom = -2.0;
export const ViewLogicalMaxTop = 2.0;

// ResourcesPath是资源路径（public 目录在开发/构建时被映射到根路径，故使用 /Resources/）
export const ResourcesPath = '/Resources/';

// 背景轮换：paths 可为相对 Resources 的路径，或以 http(s):// 开头的绝对 URL。
// remoteRandom：首屏/切换来自 GET /api/background-images/random；displayName：对话 scene_location 用的逻辑名。
export const backgroundCycle = { paths: null, remoteRandom: false, displayName: '' };

// background_order.json 缺失或为空时的默认轮换顺序（路径相对于 Resources 根目录）
export const BackgroundCyclePathsFallback = [
  'background/_blackboard_1.png',
  'background/_blackboard_2.png',
  'background/_blackboard_3.png',
  'background/_school_rooftop_1.jpg',
  'background/_school_rooftop_2.jpg',
  'background/_school_rooftop_3.jpg'
];

/** 默认背景图路径：与当前轮换列表首项同步（已设置 paths 用 paths[0]，否则用 Fallback[0]） */
export function getBackImageName() {
  const p = backgroundCycle.paths;
  if (p && p.length > 0) {
    return p[0];
  }
  return BackgroundCyclePathsFallback[0];
}

// 齿轮图标
export const GearImageName = 'icon_gear.png';

// 关闭按钮
export const PowerImageName = 'CloseNormal.png';

// 模型定义 ---------------------------------------------
// 模型所在目录名数组，目录名需与 model3.json 名称一致
// 使用可变对象以便在运行时更新
export const ModelDir = [
  // 'Hiyori',
  // 'Mark',
  // 'Natori',
  // 'Rice',
  // 'Mao',
  // 'Wanko',
  'Xiaozi',
  'Xiaogou'
];

export function setModelDir(newDirs) {
  ModelDir.length = 0;
  if (newDirs && Array.isArray(newDirs)) {
    ModelDir.push(...newDirs);
  }
}

export function getModelDirSize() {
  return ModelDir.length;
}

/** @typedef {Record<string, string>} RemotePathToUrlMap */

/** package_key -> relative_path -> MinIO/S3 public_url（由 main.js 登录后拉取） */
const _remoteAssetMaps = Object.create(null);
/** package_key -> 入口 model3 在 zip 内的 relative_path */
const _remoteEntryModel = Object.create(null);

export function clearRemotePackageManifests() {
  for (const k of Object.keys(_remoteAssetMaps)) {
    delete _remoteAssetMaps[k];
  }
  for (const k of Object.keys(_remoteEntryModel)) {
    delete _remoteEntryModel[k];
  }
}

/**
 * @param {string} packageKey
 * @param {RemotePathToUrlMap} pathToUrlMap
 * @param {string | null | undefined} entryRelativePath is_entry_model 对应行的 relative_path
 */
export function setRemotePackageManifest(packageKey, pathToUrlMap, entryRelativePath) {
  const pk = String(packageKey || '').trim();
  if (!pk || !pathToUrlMap || typeof pathToUrlMap !== 'object') {
    return;
  }
  _remoteAssetMaps[pk] = pathToUrlMap;
  if (entryRelativePath) {
    _remoteEntryModel[pk] = String(entryRelativePath)
      .replace(/\\/g, '/')
      .replace(/^\/+/, '');
  } else {
    delete _remoteEntryModel[pk];
  }
}

/** @returns {RemotePathToUrlMap | null} */
export function getRemoteAssetUrlMap(packageKey) {
  const pk = String(packageKey || '').trim();
  const m = _remoteAssetMaps[pk];
  if (!m || typeof m !== 'object') {
    return null;
  }
  return Object.keys(m).length > 0 ? m : null;
}

/** 远程包入口 model3 文件名（zip 内相对路径）；无记录时假定与目录名一致 */
export function getRemoteEntryModelRelativePath(packageKey) {
  const pk = String(packageKey || '').trim();
  if (_remoteEntryModel[pk]) {
    return _remoteEntryModel[pk];
  }
  return `${pk}.model3.json`;
}

// 与外部定义文件（json）一致
export const MotionGroupIdle = 'Idle'; // 待机
export const MotionGroupTapBody = 'TapBody'; // 点击身体时

// 与外部定义文件（json）一致
export const HitAreaNameHead = 'Head';
export const HitAreaNameBody = 'Body';

/** 长按模型后进入平移（毫秒） */
export const LongPressModelMs = 450;
/** 长按前允许的最大移动（CSS 像素），超过则取消长按并改为脸部跟随 */
export const LongPressSlopPx = 12;

// 动作优先级常量,用于控制动作的优先级
export const PriorityNone = 0;// 无优先级
export const PriorityIdle = 1;// 待机优先级
export const PriorityNormal = 2;// 普通优先级
export const PriorityForce = 3;// 强制优先级

// MOC3 一致性校验选项
export const MOCConsistencyValidationEnable = true;
// motion3.json 一致性校验选项
export const MotionConsistencyValidationEnable = true;

// 调试日志显示选项
export const DebugLogEnable = true;
export const DebugTouchLogEnable = false;

// Framework 日志级别
export const CubismLoggingLevel = LogLevel.LogLevel_Verbose;

// 默认渲染目标尺寸
export const RenderTargetWidth = 1900;
export const RenderTargetHeight = 1000;