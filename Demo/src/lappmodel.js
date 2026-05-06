/**
 * Copyright(c) Live2D Inc. All rights reserved.
 * 版权归 Live2D Inc. 所有。
 *
 * Use of this source code is governed by the Live2D Open Software license
 * 本源码的使用受 Live2D 开源软件许可协议约束，
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 * 详见 https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html。
 */

// 导入 Cubism 默认参数 ID
import { CubismDefaultParameterId } from '@framework/cubismdefaultparameterid';
// 导入模型设置 JSON 解析类
import { CubismModelSettingJson } from '@framework/cubismmodelsettingjson';
// 导入呼吸效果相关（呼吸参数数据、CubismBreath）
import { BreathParameterData, CubismBreath } from '@framework/effect/cubismbreath';
// 导入眨眼效果类
import { CubismEyeBlink } from '@framework/effect/cubismeyeblink';
// 导入 Cubism 框架核心
import { CubismFramework } from '@framework/live2dcubismframework';
// 导入 4x4 矩阵类
import { CubismMatrix44 } from '@framework/math/cubismmatrix44';
// 导入用户模型基类
import { CubismUserModel } from '@framework/model/cubismusermodel';
// 导入动作抽象基类
import {
  ACubismMotion
} from '@framework/motion/acubismmotion';
// 导入动作类
import { CubismMotion } from '@framework/motion/cubismmotion';
// 导入无效动作队列句柄常量
import {
  InvalidMotionQueueEntryHandleValue
} from '@framework/motion/cubismmotionqueuemanager';
// 导入 Map 容器类型
import { csmMap } from '@framework/type/csmmap';
// 导入 Vector 容器类型
import { csmVector } from '@framework/type/csmvector';
// 导入调试工具（断言、错误日志、信息日志）
import {
  CSM_ASSERT,
  CubismLogError,
  CubismLogInfo
} from '@framework/utils/cubismdebug';
// 导入 MOC 模型一致性检查
import { CubismMoc } from '@framework/model/cubismmoc';

// 导入本应用定义常量
import * as LAppDefine from './lappdefine.js';
// 导入平台抽象层（日志等）
import { LAppPal } from './lapppal.js';
// 导入纹理信息类型
import { TextureInfo } from './lapptexturemanager.js';
// 导入 WAV 音频文件处理类
import { LAppWavFileHandler } from './lappwavfilehandler.js';
// 导入子委托类（GL、纹理等管理）
import { LAppSubdelegate } from './lappsubdelegate.js';

/** 模型加载步骤枚举：按顺序执行各阶段 */
const LoadStep = {
  LoadAssets: 0,           // 加载资源（配置文件等）
  LoadModel: 1,            // 加载模型
  WaitLoadModel: 2,        // 等待模型加载完成
  LoadExpression: 3,      // 加载表情
  WaitLoadExpression: 4,   // 等待表情加载完成
  LoadPhysics: 5,          // 加载物理
  WaitLoadPhysics: 6,      // 等待物理加载完成
  LoadPose: 7,             // 加载姿势
  WaitLoadPose: 8,         // 等待姿势加载完成
  SetupEyeBlink: 9,        // 设置眨眼
  SetupBreath: 10,         // 设置呼吸
  LoadUserData: 11,        // 加载用户数据
  WaitLoadUserData: 12,    // 等待用户数据加载完成
  SetupEyeBlinkIds: 13,    // 设置眨眼参数 ID
  SetupLipSyncIds: 14,     // 设置口型同步参数 ID
  SetupLayout: 15,         // 设置布局
  LoadMotion: 16,         // 加载动作
  WaitLoadMotion: 17,      // 等待动作加载完成
  CompleteInitialize: 18,  // 初始化完成
  CompleteSetupModel: 19,  // 模型设置完成
  LoadTexture: 20,         // 加载纹理
  WaitLoadTexture: 21,     // 等待纹理加载完成
  CompleteSetup: 22        // 全部设置完成
};

/**
 * 用户实际使用的模型实现类
 * 继承 CubismUserModel，负责模型加载、更新、渲染等
 */
export class LAppModel extends CubismUserModel {
  constructor() {
    super();

    /**
     * 为 true 时：身体动作队列结束后不自动播放 Idle，保留末帧姿势。
     * 由聊天下发的动作成功启动后置位，直到下一轮对话再次成功启动动作。
     */
    this._suppressIdleAfterChatBodyMotion = false;

    this._modelSetting = null;       // 模型配置（.model3.json 解析结果）
    this._modelHomeDir = null;       // 模型资源所在目录（本地时为 /Resources/{pkg}/）
    this._remotePathToUrl = null;    // 远程：relative_path -> public_url
    this._userTimeSeconds = 0.0;    // 用户侧累计时间（秒）

    this._eyeBlinkIds = new csmVector();   // 眨眼相关参数 ID 列表
    this._lipSyncIds = new csmVector();    // 口型同步参数 ID 列表

    this._motions = new csmMap();          // 动作名 -> 动作对象
    this._expressions = new csmMap();      // 表情名 -> 表情动作

    this._hitArea = new csmVector();       // 命中区域
    this._userArea = new csmVector();      // 用户自定义区域

    this._idParamAngleX = CubismFramework.getIdManager().getId(
      CubismDefaultParameterId.ParamAngleX
    );
    this._idParamAngleY = CubismFramework.getIdManager().getId(
      CubismDefaultParameterId.ParamAngleY
    );
    this._idParamAngleZ = CubismFramework.getIdManager().getId(
      CubismDefaultParameterId.ParamAngleZ
    );
    this._idParamEyeBallX = CubismFramework.getIdManager().getId(
      CubismDefaultParameterId.ParamEyeBallX
    );
    this._idParamEyeBallY = CubismFramework.getIdManager().getId(
      CubismDefaultParameterId.ParamEyeBallY
    );
    this._idParamBodyAngleX = CubismFramework.getIdManager().getId(
      CubismDefaultParameterId.ParamBodyAngleX
    );
    this._idParamMouthOpenY = CubismFramework.getIdManager().getId(
      CubismDefaultParameterId.ParamMouthOpenY
    );

    if (LAppDefine.MOCConsistencyValidationEnable) {
      this._mocConsistency = true;
    }

    if (LAppDefine.MotionConsistencyValidationEnable) {
      this._motionConsistency = true;
    }

    this._state = LoadStep.LoadAssets;
    this._expressionCount = 0;
    this._textureCount = 0;
    this._motionCount = 0;
    this._allMotionCount = 0;
    this._wavFileHandler = new LAppWavFileHandler();
    this._textLipSyncEndTimeMs = 0;
    this._ttsAudioLipTarget = 0;
    this._ttsAudioLipSmoothed = 0;
    this._consistency = false;
  }

  /**
   * @param {boolean} enabled 是否禁止在身体动作结束后自动接 Idle
   */
  setHoldChatBodyMotionPose(enabled) {
    this._suppressIdleAfterChatBodyMotion = !!enabled;
  }

  /** 加载模型资源：读取配置文件并启动模型设置流程 */
  loadAssets(dir, fileName) {
    this._modelHomeDir = dir;

    const url = this._resolveAssetUrl(fileName);
    fetch(url)
      .then(response => response.arrayBuffer())
      .then(arrayBuffer => {
        const setting = new CubismModelSettingJson(
          arrayBuffer,
          arrayBuffer.byteLength
        );
        this._state = LoadStep.LoadModel;
        this.setupModel(setting);
      })
      .catch(() => {
        CubismLogError(`Failed to load file ${url}`);
      });
  }

  /**
   * 使用后端 manifest 的 relative_path -> 可 fetch URL（应为 presigned 或网关代理地址，勿用裸 MinIO 直链）。
   * 设置后所有资源路径优先查表，否则回退为 ``modelHomeDir + 相对路径``（本地 /Resources/）。
   */
  setRemoteAssetUrlMap(map) {
    this._remotePathToUrl =
      map && typeof map === 'object' ? { ...map } : null;
  }

  _normalizeAssetRelPath(relPath) {
    return String(relPath || '')
      .replace(/\\/g, '/')
      .replace(/^\.\/+/, '')
      .replace(/^\/+/, '');
  }

  _resolveAssetUrl(relPath) {
    const raw = String(relPath || '');
    const norm = this._normalizeAssetRelPath(raw);
    if (this._remotePathToUrl) {
      const hit =
        this._remotePathToUrl[norm] ??
        this._remotePathToUrl[raw] ??
        this._remotePathToUrl[decodeURIComponent(norm)];
      if (hit) {
        return hit;
      }
    }
    return `${this._modelHomeDir ?? ''}${raw}`;
  }

  /** 根据模型设置依次加载 .moc3、表情、物理、姿势等并完成初始化 */
  setupModel(setting) {
    // 标记正在更新（加载中）
    this._updating = true;
    // 尚未完成初始化
    this._initialized = false;

    // 保存模型设置引用
    this._modelSetting = setting;

    // 若配置中指定了模型文件名（.moc3）
    if (this._modelSetting.getModelFileName() != '') {
      // 取得模型文件名
      const modelFileName = this._modelSetting.getModelFileName();

      fetch(this._resolveAssetUrl(modelFileName))
        .then(response => {
          if (response.ok) {
            return response.arrayBuffer();
          } else if (response.status >= 400) {
            CubismLogError(
              `Failed to load file ${this._resolveAssetUrl(modelFileName)}`
            );
            return new ArrayBuffer(0);
          }
        })
        .then(arrayBuffer => {
          // 用 ArrayBuffer 加载模型（可选做 MOC 一致性校验）
          this.loadModel(arrayBuffer, this._mocConsistency);
          // 下一步：加载表情
          this._state = LoadStep.LoadExpression;
          loadCubismExpression();
        });

      // 当前处于等待模型加载完成
      this._state = LoadStep.WaitLoadModel;
    } else {
      // 未配置模型文件则提示
      LAppPal.printMessage('模型数据不存在。');
    }

    /** 内部函数：加载所有表情文件（.exp3） */
    const loadCubismExpression = () => {
      if (this._modelSetting.getExpressionCount() > 0) {
        const count = this._modelSetting.getExpressionCount();

        for (let i = 0; i < count; i++) {
          const expressionName = this._modelSetting.getExpressionName(i);
          const expressionFileName =
            this._modelSetting.getExpressionFileName(i);

          fetch(this._resolveAssetUrl(expressionFileName))
            .then(response => {
              if (response.ok) {
                return response.arrayBuffer();
              } else if (response.status >= 400) {
                CubismLogError(
                  `Failed to load file ${this._resolveAssetUrl(expressionFileName)}`
                );
                return new ArrayBuffer(0);
              }
            })
            .then(arrayBuffer => {
              // 将表情数据加载为表情动作
              const motion = this.loadExpression(
                arrayBuffer,
                arrayBuffer.byteLength,
                expressionName
              );

              // 若该名称已有旧表情则先释放
              if (this._expressions.getValue(expressionName) != null) {
                ACubismMotion.delete(
                  this._expressions.getValue(expressionName)
                );
                this._expressions.setValue(expressionName, null);
              }

              this._expressions.setValue(expressionName, motion);
              this._expressionCount++;

              // 全部表情加载完后进入物理加载
              if (this._expressionCount >= count) {
                this._state = LoadStep.LoadPhysics;
                loadCubismPhysics();
              }
            });
        }
        this._state = LoadStep.WaitLoadExpression;
      } else {
        this._state = LoadStep.LoadPhysics;
        loadCubismPhysics();
      }
    };

    /** 内部函数：加载物理文件（.physics3.json） */
    const loadCubismPhysics = () => {
      if (this._modelSetting.getPhysicsFileName() != '') {
        const physicsFileName = this._modelSetting.getPhysicsFileName();

        fetch(this._resolveAssetUrl(physicsFileName))
          .then(response => {
            if (response.ok) {
              return response.arrayBuffer();
            } else if (response.status >= 400) {
              CubismLogError(
                `Failed to load file ${this._resolveAssetUrl(physicsFileName)}`
              );
              return new ArrayBuffer(0);
            }
          })
          .then(arrayBuffer => {
            this.loadPhysics(arrayBuffer, arrayBuffer.byteLength);
            this._state = LoadStep.LoadPose;
            loadCubismPose();
          });
        this._state = LoadStep.WaitLoadPhysics;
      } else {
        this._state = LoadStep.LoadPose;
        loadCubismPose();
      }
    };

    /** 内部函数：加载姿势文件（.pose3.json） */
    const loadCubismPose = () => {
      if (this._modelSetting.getPoseFileName() != '') {
        const poseFileName = this._modelSetting.getPoseFileName();

        fetch(this._resolveAssetUrl(poseFileName))
          .then(response => {
            if (response.ok) {
              return response.arrayBuffer();
            } else if (response.status >= 400) {
              CubismLogError(
                `Failed to load file ${this._resolveAssetUrl(poseFileName)}`
              );
              return new ArrayBuffer(0);
            }
          })
          .then(arrayBuffer => {
            this.loadPose(arrayBuffer, arrayBuffer.byteLength);
            this._state = LoadStep.SetupEyeBlink;
            setupEyeBlink();
          });
        this._state = LoadStep.WaitLoadPose;
      } else {
        this._state = LoadStep.SetupEyeBlink;
        setupEyeBlink();
      }
    };

    /** 内部函数：根据设置创建眨眼控制器 */
    const setupEyeBlink = () => {
      if (this._modelSetting.getEyeBlinkParameterCount() > 0) {
        this._eyeBlink = CubismEyeBlink.create(this._modelSetting);
        this._state = LoadStep.SetupBreath;
      }
      setupBreath();
    };

    /** 内部函数：创建呼吸效果并设置头部、身体等呼吸参数 */
    const setupBreath = () => {
      this._breath = CubismBreath.create();

      const breathParameters = new csmVector();
      // 头部 X 轴角度呼吸
      breathParameters.pushBack(
        new BreathParameterData(this._idParamAngleX, 0.0, 15.0, 6.5345, 0.5)
      );
      // 头部 Y 轴角度呼吸
      breathParameters.pushBack(
        new BreathParameterData(this._idParamAngleY, 0.0, 8.0, 3.5345, 0.5)
      );
      // 头部 Z 轴角度呼吸
      breathParameters.pushBack(
        new BreathParameterData(this._idParamAngleZ, 0.0, 10.0, 5.5345, 0.5)
      );
      // 身体 X 轴角度呼吸
      breathParameters.pushBack(
        new BreathParameterData(this._idParamBodyAngleX, 0.0, 4.0, 15.5345, 0.5)
      );
      // 呼吸专用参数
      breathParameters.pushBack(
        new BreathParameterData(
          CubismFramework.getIdManager().getId(
            CubismDefaultParameterId.ParamBreath
          ),
          0.5,
          0.5,
          3.2345,
          1
        )
      );

      this._breath.setParameters(breathParameters);
      this._state = LoadStep.LoadUserData;
      loadUserData();
    };

    /** 内部函数：加载用户数据文件（.userdata3.json） */
    const loadUserData = () => {
      if (this._modelSetting.getUserDataFile() != '') {
        const userDataFile = this._modelSetting.getUserDataFile();

        fetch(this._resolveAssetUrl(userDataFile))
          .then(response => {
            if (response.ok) {
              return response.arrayBuffer();
            } else if (response.status >= 400) {
              CubismLogError(
                `Failed to load file ${this._resolveAssetUrl(userDataFile)}`
              );
              return new ArrayBuffer(0);
            }
          })
          .then(arrayBuffer => {
            this.loadUserData(arrayBuffer, arrayBuffer.byteLength);
            this._state = LoadStep.SetupEyeBlinkIds;
            setupEyeBlinkIds();
          });

        this._state = LoadStep.WaitLoadUserData;
      } else {
        this._state = LoadStep.SetupEyeBlinkIds;
        setupEyeBlinkIds();
      }
    };

    /** 内部函数：从设置中收集眨眼参数 ID 列表 */
    const setupEyeBlinkIds = () => {
      const eyeBlinkIdCount = this._modelSetting.getEyeBlinkParameterCount();

      for (let i = 0; i < eyeBlinkIdCount; ++i) {
        this._eyeBlinkIds.pushBack(
          this._modelSetting.getEyeBlinkParameterId(i)
        );
      }

      this._state = LoadStep.SetupLipSyncIds;
      setupLipSyncIds();
    };

    /** 内部函数：从设置中收集口型同步参数 ID 列表 */
    const setupLipSyncIds = () => {
      const lipSyncIdCount = this._modelSetting.getLipSyncParameterCount();

      for (let i = 0; i < lipSyncIdCount; ++i) {
        this._lipSyncIds.pushBack(this._modelSetting.getLipSyncParameterId(i));
      }
      this._state = LoadStep.SetupLayout;
      setupLayout();
    };

    /** 内部函数：从设置读取布局并应用到模型矩阵 */
    const setupLayout = () => {
      const layout = new csmMap();

      if (this._modelSetting == null || this._modelMatrix == null) {
        CubismLogError('Failed to setupLayout().');
        return;
      }

      this._modelSetting.getLayoutMap(layout);
      this._modelMatrix.setupFromLayout(layout);
      this._state = LoadStep.LoadMotion;
      loadCubismMotion();
    };

    /** 内部函数：预加载所有动作组，若无动作组则直接完成初始化并加载纹理 */
    const loadCubismMotion = () => {
      this._state = LoadStep.WaitLoadMotion;
      this._model.saveParameters();
      this._allMotionCount = 0;
      this._motionCount = 0;
      const group = [];

      const motionGroupCount = this._modelSetting.getMotionGroupCount();

      for (let i = 0; i < motionGroupCount; i++) {
        group[i] = this._modelSetting.getMotionGroupName(i);
        this._allMotionCount += this._modelSetting.getMotionCount(group[i]);
      }

      for (let i = 0; i < motionGroupCount; i++) {
        this.preLoadMotionGroup(group[i]);
      }

      // 没有任何动作组时直接进入纹理加载并完成初始化
      if (motionGroupCount == 0) {
        this._state = LoadStep.LoadTexture;
        this._motionManager.stopAllMotions();
        this._updating = false;
        this._initialized = true;
        this.createRenderer();
        this.setupTextures();
        this.getRenderer().startUp(this._subdelegate.getGlManager().getGl());
      }
    };
  }

  /** 根据模型设置加载所有纹理（PNG），绑定到渲染器并在全部加载完成后标记 CompleteSetup */
  setupTextures() {
    const usePremultiply = true;  // 使用预乘 Alpha

    if (this._state == LoadStep.LoadTexture) {
      const textureCount = this._modelSetting.getTextureCount();

      for (
        let modelTextureNumber = 0;
        modelTextureNumber < textureCount;
        modelTextureNumber++
      ) {
        if (this._modelSetting.getTextureFileName(modelTextureNumber) == '') {
          console.log('纹理文件名为空');
          continue;
        }

        let texturePath =
          this._modelSetting.getTextureFileName(modelTextureNumber);
        texturePath = this._resolveAssetUrl(texturePath);

        const onLoad = (textureInfo) => {
          this.getRenderer().bindTexture(modelTextureNumber, textureInfo.id);
          this._textureCount++;

          if (this._textureCount >= textureCount) {
            this._state = LoadStep.CompleteSetup;
            // 调试：打印模型所有 Drawable ID，用于配置 HitAreas（Id 需与其中之一对应）
            if (LAppDefine.DebugLogEnable && this.getModel()) {
              const drawableCount = this.getModel().getDrawableCount();
              const ids = [];
              for (let i = 0; i < drawableCount; i++) {
                ids.push(this.getModel().getDrawableId(i).getString().s);
              }
              LAppPal.printMessage(
                `[APP] 模型 Drawable IDs（用于 HitAreas 配置）: ${ids.join(', ')}`
              );
            }
          }
        };

        this._subdelegate
          .getTextureManager()
          .createTextureFromPngFile(texturePath, usePremultiply, onLoad);
        this.getRenderer().setIsPremultipliedAlpha(usePremultiply);
      }

      this._state = LoadStep.WaitLoadTexture;
    }
  }

  /** 重新创建渲染器并重新设置纹理（用于上下文丢失恢复等） */
  reloadRenderer() {
    this.deleteRenderer();
    this.createRenderer();
    this.setupTextures();
  }

  /** 每帧更新：拖拽、动作、眨眼、表情、呼吸、物理、口型、姿势，最后更新模型 */
  update() {
    if (this._state != LoadStep.CompleteSetup) return;

    const deltaTimeSeconds = LAppPal.getDeltaTime();
    this._userTimeSeconds += deltaTimeSeconds;

    this._dragManager.update(deltaTimeSeconds);
    this._dragX = this._dragManager.getX();
    this._dragY = this._dragManager.getY();

    let motionUpdated = false;

    this._model.loadParameters();
    if (this._motionManager.isFinished()) {
      if (!this._suppressIdleAfterChatBodyMotion) {
        this.startRandomMotion(
          LAppDefine.MotionGroupIdle,
          LAppDefine.PriorityIdle
        );
      }
    } else {
      motionUpdated = this._motionManager.updateMotion(
        this._model,
        deltaTimeSeconds
      );
    }
    this._model.saveParameters();

    // 即使在播放动作（Idle 等）时也允许自动眨眼；
    // 否则 motionUpdated 常为 true，会导致完全不眨眼。
    if (this._eyeBlink != null) {
      this._eyeBlink.updateParameters(this._model, deltaTimeSeconds);
    }

    if (this._expressionManager != null) {
      this._expressionManager.updateMotion(this._model, deltaTimeSeconds);
    }

    this._model.addParameterValueById(this._idParamAngleX, this._dragX * 30);
    this._model.addParameterValueById(this._idParamAngleY, this._dragY * 30);
    this._model.addParameterValueById(
      this._idParamAngleZ,
      this._dragX * this._dragY * -30
    );

    this._model.addParameterValueById(
      this._idParamBodyAngleX,
      this._dragX * 10
    );

    this._model.addParameterValueById(this._idParamEyeBallX, this._dragX);
    this._model.addParameterValueById(this._idParamEyeBallY, this._dragY);

    if (this._breath != null) {
      this._breath.updateParameters(this._model, deltaTimeSeconds);
    }

    if (this._physics != null) {
      this._physics.evaluate(this._model, deltaTimeSeconds);
    }

    let lipSyncValue = 0.0;

    if (this._lipsync) {
      this._wavFileHandler.update(deltaTimeSeconds);
      lipSyncValue = Math.max(lipSyncValue, this._wavFileHandler.getRms());
    }

    // 聊天 TTS：由前端 Web Audio RMS 驱动（见 ws.js），不用文本节奏模拟口型
    const ttsTau = 18.0;
    const ttsK = Math.min(1.0, deltaTimeSeconds * ttsTau);
    this._ttsAudioLipSmoothed +=
      (this._ttsAudioLipTarget - this._ttsAudioLipSmoothed) * ttsK;
    lipSyncValue = Math.max(lipSyncValue, this._ttsAudioLipSmoothed);

    if (lipSyncValue > 0.0) {
      if (this._lipSyncIds.getSize() > 0) {
        for (let i = 0; i < this._lipSyncIds.getSize(); ++i) {
          this._model.addParameterValueById(
            this._lipSyncIds.at(i),
            lipSyncValue,
            0.8
          );
        }
      } else {
        // 某些模型未在 model3.json 配置 LipSync 参数，回退到默认嘴巴参数。
        this._model.addParameterValueById(this._idParamMouthOpenY, lipSyncValue, 0.8);
      }
    }

    if (this._pose != null) {
      this._pose.updateParameters(this._model, deltaTimeSeconds);
    }

    this._model.update();
  }

  /**
   * 预留接口：聊天口型已改为由 TTS 音频 RMS 驱动，不再根据文本模拟。
   * @param {string} text
   */
  triggerTextLipSync(text) {
    void text;
  }

  /** 实时口型目标 0..1（由 WebSocket TTS 播放侧按帧写入）。 */
  setTtsAudioLipLevel(level) {
    const v = Number(level);
    this._ttsAudioLipTarget = Number.isFinite(v)
      ? Math.max(0, Math.min(1, v))
      : 0;
  }

  /** 结束 TTS 口型；immediate 时当前帧直接闭嘴（打断/会话结束）。 */
  clearTtsAudioLip(immediate = false) {
    this._ttsAudioLipTarget = 0;
    if (immediate) {
      this._ttsAudioLipSmoothed = 0;
    }
  }

  /** @deprecated 与文本口型一并停用，保留空实现以兼容旧调用。 */
  stopTextLipSync() {
    this._textLipSyncEndTimeMs = 0;
  }

  /** 按组名与编号启动指定动作，可设优先级与开始/结束回调；若未预加载则异步加载后播放 */
  startMotion(group, no, priority, onFinishedMotionHandler, onBeganMotionHandler) {
    if (priority == LAppDefine.PriorityForce) {
      this._motionManager.setReservePriority(priority);
    } else if (!this._motionManager.reserveMotion(priority)) {
      if (this._debugMode) {
        LAppPal.printMessage('[APP] 无法启动动作。');
      }
      return InvalidMotionQueueEntryHandleValue;
    }

    const motionFileName = this._modelSetting.getMotionFileName(group, no);
    const name = `${group}_${no}`;
    let motion = this._motions.getValue(name);
    let autoDelete = false;

    if (motion == null) {
      fetch(this._resolveAssetUrl(motionFileName))
        .then(response => {
          if (response.ok) {
            return response.arrayBuffer();
          } else if (response.status >= 400) {
            CubismLogError(
              `Failed to load file ${this._resolveAssetUrl(motionFileName)}`
            );
            return new ArrayBuffer(0);
          }
        })
        .then(arrayBuffer => {
          motion = this.loadMotion(
            arrayBuffer,
            arrayBuffer.byteLength,
            null,
            onFinishedMotionHandler,
            onBeganMotionHandler,
            this._modelSetting,
            group,
            no,
            this._motionConsistency
          );
        });

      if (motion) {
        motion.setEffectIds(this._eyeBlinkIds, this._lipSyncIds);
        autoDelete = true;
      } else {
        CubismLogError("Can't start motion {0} .", motionFileName);
        this._motionManager.setReservePriority(LAppDefine.PriorityNone);
        return InvalidMotionQueueEntryHandleValue;
      }
    } else {
      motion.setBeganMotionHandler(onBeganMotionHandler);
      motion.setFinishedMotionHandler(onFinishedMotionHandler);
    }

    const voice = this._modelSetting.getMotionSoundFileName(group, no);
    if (voice.localeCompare('') != 0) {
      this._wavFileHandler.start(this._resolveAssetUrl(voice));
    }

    if (this._debugMode) {
      LAppPal.printMessage(`[APP] 启动动作: [${group}_${no}]`);
    }
    return this._motionManager.startMotionPriority(
      motion,
      autoDelete,
      priority
    );
  }

  /** 在指定动作组中随机选一个动作并启动 */
  startRandomMotion(group, priority, onFinishedMotionHandler, onBeganMotionHandler) {
    if (this._modelSetting.getMotionCount(group) == 0) {
      return InvalidMotionQueueEntryHandleValue;
    }

    const no = Math.floor(
      Math.random() * this._modelSetting.getMotionCount(group)
    );

    return this.startMotion(
      group,
      no,
      priority,
      onFinishedMotionHandler,
      onBeganMotionHandler
    );
  }

  /**
   * 按「.motion3.json」文件名（不含扩展名）与后端 catalog 的 motion 标识一致，在模型中查找并播放。
   */
  startMotionByBasename(
    basename,
    priority,
    onFinishedMotionHandler,
    onBeganMotionHandler
  ) {
    if (!this._modelSetting || !basename) {
      return InvalidMotionQueueEntryHandleValue;
    }
    const normalized = String(basename).trim();
    if (!normalized) {
      return InvalidMotionQueueEntryHandleValue;
    }
    const motionBasenameFromPath = (file) => {
      const name = file.split(/[/\\]/).pop() || '';
      return name
        .replace(/\.motion3\.json$/i, '')
        .replace(/\.motion3$/i, '');
    };
    const groupCount = this._modelSetting.getMotionGroupCount();
    for (let gi = 0; gi < groupCount; gi++) {
      const group = this._modelSetting.getMotionGroupName(gi);
      const count = this._modelSetting.getMotionCount(group);
      for (let i = 0; i < count; i++) {
        const file = this._modelSetting.getMotionFileName(group, i);
        const base = motionBasenameFromPath(file);
        if (base === normalized) {
          return this.startMotion(
            group,
            i,
            priority,
            onFinishedMotionHandler,
            onBeganMotionHandler
          );
        }
      }
    }
    if (this._debugMode) {
      LAppPal.printMessage(`[APP] 未找到动作: [${basename}]`);
    }
    return InvalidMotionQueueEntryHandleValue;
  }

  /** 根据表情 ID 播放对应表情动作 */
  setExpression(expressionId) {
    const motion = this._expressions.getValue(expressionId);

    if (this._debugMode) {
      LAppPal.printMessage(`[APP] 表情: [${expressionId}]`);
    }

    if (motion != null) {
      this._expressionManager.startMotion(motion, false);
    } else {
      if (this._debugMode) {
        LAppPal.printMessage(`[APP] 表情 [${expressionId}] 为空`);
      }
    }
  }

  /** 随机选择一个已加载的表情并播放 */
  setRandomExpression() {
    if (this._expressions.getSize() == 0) {
      return;
    }

    const no = Math.floor(Math.random() * this._expressions.getSize());

    for (let i = 0; i < this._expressions.getSize(); i++) {
      if (i == no) {
        const name = this._expressions._keyValues[i].first;
        this.setExpression(name);
        return;
      }
    }
  }

  /**
   * 恢复到默认表情：优先匹配常见默认名称，否则回退到第一个已加载表情。
   */
  resetToDefaultExpression() {
    if (this._expressions.getSize() === 0) {
      return false;
    }

    const preferredNames = ['default', 'normal', 'idle', 'neutral', 'base'];
    for (let i = 0; i < this._expressions.getSize(); i++) {
      const name = String(this._expressions._keyValues[i].first || '');
      if (!name) {
        continue;
      }
      const normalized = name.trim().toLowerCase();
      if (preferredNames.includes(normalized)) {
        this.setExpression(name);
        return true;
      }
    }

    const fallbackName = this._expressions._keyValues[0].first;
    if (fallbackName) {
      this.setExpression(fallbackName);
      return true;
    }
    return false;
  }

  /** 动作事件触发时的回调，用于日志等 */
  motionEventFired(eventValue) {
    CubismLogInfo('{0} is fired on LAppModel!!', eventValue.s);
  }

  /** 检测指定名称的命中区域是否包含点 (x, y)；透明度小于 1 时直接返回 false；支持同一 Name 对应多个 Drawable */
  hitTest(hitArenaName, x, y) {
    if (this._opacity < 1) {
      return false;
    }
    if (this._modelSetting == null) {
      return false;
    }

    const count = this._modelSetting.getHitAreasCount();

    for (let i = 0; i < count; i++) {
      if (this._modelSetting.getHitAreaName(i) == hitArenaName) {
        const drawId = this._modelSetting.getHitAreaId(i);
        if (this.isHit(drawId, x, y)) {
          return true;
        }
      }
    }

    return false;
  }

  /** 预加载指定动作组内的所有动作文件，全部加载完成后进入纹理加载并完成初始化 */
  preLoadMotionGroup(group) {
    for (let i = 0; i < this._modelSetting.getMotionCount(group); i++) {
      const motionFileName = this._modelSetting.getMotionFileName(group, i);
      const name = `${group}_${i}`;
      if (this._debugMode) {
        LAppPal.printMessage(
          `[APP] 加载动作: ${motionFileName} => [${name}]`
        );
      }

      fetch(this._resolveAssetUrl(motionFileName))
        .then(response => {
          if (response.ok) {
            return response.arrayBuffer();
          } else if (response.status >= 400) {
            CubismLogError(
              `Failed to load file ${this._resolveAssetUrl(motionFileName)}`
            );
            return new ArrayBuffer(0);
          }
        })
        .then(arrayBuffer => {
          const tmpMotion = this.loadMotion(
            arrayBuffer,
            arrayBuffer.byteLength,
            name,
            null,
            null,
            this._modelSetting,
            group,
            i,
            this._motionConsistency
          );

          if (tmpMotion != null) {
            tmpMotion.setEffectIds(this._eyeBlinkIds, this._lipSyncIds);

            if (this._motions.getValue(name) != null) {
              ACubismMotion.delete(this._motions.getValue(name));
            }

            this._motions.setValue(name, tmpMotion);

            this._motionCount++;
          } else {
            this._allMotionCount--;
          }

          if (this._motionCount >= this._allMotionCount) {
            this._state = LoadStep.LoadTexture;
            this._motionManager.stopAllMotions();
            this._updating = false;
            this._initialized = true;
            this.createRenderer();
            this.setupTextures();
            this.getRenderer().startUp(
              this._subdelegate.getGlManager().getGl()
            );
          }
        });
    }
  }

  /** 释放所有已加载的动作 */
  releaseMotions() {
    this._motions.clear();
  }

  /** 释放所有已加载的表情 */
  releaseExpressions() {
    this._expressions.clear();
  }

  /** 执行实际绘制：设置渲染状态与视口后绘制模型 */
  doDraw() {
    if (this._model == null) return;

    const canvas = this._subdelegate.getCanvas();
    const viewport = [0, 0, canvas.width, canvas.height];

    this.getRenderer().setRenderState(
      this._subdelegate.getFrameBuffer(),
      viewport
    );
    this.getRenderer().drawModel();
  }

  /** 使用给定矩阵绘制模型（仅在 CompleteSetup 时绘制，会乘上模型矩阵） */
  draw(matrix) {
    if (this._model == null) {
      return;
    }

    if (this._state == LoadStep.CompleteSetup) {
      matrix.multiplyByMatrix(this._modelMatrix);
      this.getRenderer().setMvpMatrix(matrix);
      this.doDraw();
    }
  }

  /** 从文件异步检测 MOC3 是否具有一致性，结果写入 _consistency 并返回 */
  async hasMocConsistencyFromFile() {
    CSM_ASSERT(this._modelSetting.getModelFileName().localeCompare(``));

    if (this._modelSetting.getModelFileName() != '') {
      const modelFileName = this._modelSetting.getModelFileName();

      const response = await fetch(this._resolveAssetUrl(modelFileName));
      const arrayBuffer = await response.arrayBuffer();

      this._consistency = CubismMoc.hasMocConsistency(arrayBuffer);

      if (!this._consistency) {
        CubismLogInfo('Inconsistent MOC3.');
      } else {
        CubismLogInfo('Consistent MOC3.');
      }

      return this._consistency;
    } else {
      LAppPal.printMessage('模型数据不存在。');
    }
  }

  /** 设置子委托（用于 GL、纹理、画布等） */
  setSubdelegate(subdelegate) {
    this._subdelegate = subdelegate;
  }
}
