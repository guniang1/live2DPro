/**
该文件是应用的入口文件，负责初始化应用并运行应用实例

LAppDelegate是应用的入口类，负责初始化应用并运行应用实例
LAppDelegate.getInstance().initialize()是初始化应用的方法
LAppDelegate.getInstance().run()是运行应用的方法
LAppDelegate.getInstance().releaseInstance()是释放应用的方法

LAppDefine是应用的定义文件，负责定义应用的常量，比如画布大小、模型大小等
这里使用的是默认值，可以根据需要修改
 */

import { LAppDelegate } from './lappdelegate.js';
import * as LAppDefine from './lappdefine.js';
import AudioRecorder from './utils/AudioRecorder.js';
// 注意：导入的实例名是 SpeechRecognition（和原代码保持一致）
import SpeechRecognition from './utils/SpeechRecognition.js';
import {
    appendChatMessage,
    prependChatHistoryRows,
    reconnectChatWebSocketsForNewPackage,
    renderChatHistoryRows,
    sendChatMessage,
} from './api/ws.js';
import { fetchChatSessionsForPanel } from './api/chatSessions.js';
import { triggerLongMemoryConsolidateNow } from './api/longMemory.js';
import { triggerRemindScanNow } from './api/remindTriggers.js';
import { getLive2dPackage, getUserId, setLive2dPackage, setUserId } from './api/wsConfig.js';
import { getAssetDownloadUrl, getModelAssets, getModelPackages } from './api/assetUpload.js';
import {
    applyOptionalSharedDownloadProxy,
    looksLikePresignedStorageUrl,
} from './api/storageFetchUrl.js';
import { fetchRandomBackgroundImage } from './api/backgroundImages.js';

/** 左侧历史与 GET /chat-sessions 对齐：每页会话轮次数（user+ai 算一行 chat_session） */
const CHAT_HISTORY_PAGE_SIZE = 50;

let chatHistoryLoadGeneration = 0;
let chatHistoryNextPage = 2;
let chatHistoryLoadedRounds = 0;
let chatHistoryHasMoreOlder = true;
let chatHistoryOlderLoading = false;
let chatHistoryPagingReady = false;

function resolveChatHistoryUserId() {
    const raw = localStorage.getItem('live2d_info');
    let auth = null;
    try {
        auth = raw ? JSON.parse(raw) : null;
    } catch (_) {
        auth = null;
    }
    const uid = auth?.user_id != null ? Number(auth.user_id) : getUserId();
    if (!uid || !Number.isInteger(uid) || uid <= 0) {
        return null;
    }
    return uid;
}

function formatChatHistoryHeader(loadedRounds, hasMoreOlder) {
    if (loadedRounds <= 0) {
        return '和 Ta 聊聊天（还没有旧记录，先说一句吧）';
    }
    const tail = hasMoreOlder ? ' · 上滑加载更早' : '';
    return `和 Ta 聊聊天（已接上 ${loadedRounds} 轮回忆${tail}）`;
}

async function loadOlderChatHistoryPage() {
    if (
        !chatHistoryPagingReady ||
        !chatHistoryHasMoreOlder ||
        chatHistoryOlderLoading
    ) {
        return;
    }
    const uid = resolveChatHistoryUserId();
    if (!uid) {
        return;
    }
    chatHistoryOlderLoading = true;
    const gen = chatHistoryLoadGeneration;
    const list = document.getElementById('chat-list');
    const headerEl = document.getElementById('chat-header');
    try {
        const rows = await fetchChatSessionsForPanel({
            userId: uid,
            packageKey: getLive2dPackage(),
            page: chatHistoryNextPage,
            size: CHAT_HISTORY_PAGE_SIZE,
        });
        if (gen !== chatHistoryLoadGeneration) {
            return;
        }
        if (!Array.isArray(rows) || rows.length === 0) {
            chatHistoryHasMoreOlder = false;
            if (headerEl) {
                headerEl.textContent = formatChatHistoryHeader(
                    chatHistoryLoadedRounds,
                    false
                );
            }
            return;
        }
        const ordered = [...rows].reverse();
        if (!list) {
            return;
        }
        const prevScrollHeight = list.scrollHeight;
        const prevScrollTop = list.scrollTop;
        prependChatHistoryRows(ordered);
        const prevBehavior = list.style.scrollBehavior;
        list.style.scrollBehavior = 'auto';
        list.scrollTop = prevScrollTop + (list.scrollHeight - prevScrollHeight);
        list.style.scrollBehavior = prevBehavior;

        chatHistoryLoadedRounds += rows.length;
        chatHistoryNextPage += 1;
        chatHistoryHasMoreOlder = rows.length >= CHAT_HISTORY_PAGE_SIZE;
        if (headerEl) {
            headerEl.textContent = formatChatHistoryHeader(
                chatHistoryLoadedRounds,
                chatHistoryHasMoreOlder
            );
        }
    } catch (e) {
        console.warn('加载更早对话失败', e);
    } finally {
        if (gen === chatHistoryLoadGeneration) {
            chatHistoryOlderLoading = false;
        }
    }
}

function setupChatHistoryScrollPagingOnce() {
    const list = document.getElementById('chat-list');
    if (!list || list.dataset.historyScrollBound === '1') {
        return;
    }
    list.dataset.historyScrollBound = '1';
    list.addEventListener(
        'scroll',
        () => {
            if (list.scrollTop > 80) {
                return;
            }
            void loadOlderChatHistoryPage();
        },
        { passive: true }
    );
}

/** 从 DB 拉取当前用户 + 当前模型包 的近期 chat_session（不按浏览器 session 过滤，避免与库里旧 session_key 对不上而一直空白）。默认仅最新一页，上滑再拉更早页。 */
async function refreshChatHistoryFromServer() {
    const gen = ++chatHistoryLoadGeneration;
    chatHistoryPagingReady = false;
    chatHistoryOlderLoading = false;
    chatHistoryNextPage = 2;
    chatHistoryLoadedRounds = 0;
    chatHistoryHasMoreOlder = true;

    const headerEl = document.getElementById('chat-header');
    const uid = resolveChatHistoryUserId();
    if (!uid) {
        return;
    }
    const prevHeader = headerEl ? headerEl.textContent : '';
    if (headerEl) {
        headerEl.textContent = '聊天（正在回忆你们说过的话…）';
    }
    try {
        const rows = await fetchChatSessionsForPanel({
            userId: uid,
            packageKey: getLive2dPackage(),
            page: 1,
            size: CHAT_HISTORY_PAGE_SIZE,
        });
        if (gen !== chatHistoryLoadGeneration) {
            return;
        }
        // 接口按 create_time DESC；面板旧在上新在下，故反转为时间正序
        const ordered = Array.isArray(rows) ? [...rows].reverse() : [];
        renderChatHistoryRows(ordered);
        chatHistoryLoadedRounds = rows.length;
        chatHistoryNextPage = 2;
        chatHistoryHasMoreOlder = rows.length >= CHAT_HISTORY_PAGE_SIZE;
        chatHistoryPagingReady = true;
        if (headerEl) {
            headerEl.textContent = formatChatHistoryHeader(
                ordered.length,
                chatHistoryHasMoreOlder
            );
        }
    } catch (e) {
        console.warn('加载历史对话失败', e);
        if (gen !== chatHistoryLoadGeneration) {
            return;
        }
        chatHistoryPagingReady = false;
        if (headerEl) {
            headerEl.textContent = prevHeader || '和 Ta 聊聊天';
        }
        const list = document.getElementById('chat-list');
        if (list) {
            const tip = document.createElement('div');
            tip.className = 'chat-item ai';
            tip.style.opacity = '0.85';
            tip.style.fontSize = '13px';
            tip.textContent = `历史加载失败：${e && e.message ? e.message : String(e)}`;
            list.appendChild(tip);
        }
    }
}

function stemFromBackgroundRel(rel) {
    const s = String(rel || '').replace(/\\/g, '/');
    const base = s.split('/').filter(Boolean).pop() || s;
    const dot = base.lastIndexOf('.');
    return dot > 0 ? base.slice(0, dot) : base;
}

async function ensureBackgroundCycleList() {
    try {
        const row = await fetchRandomBackgroundImage({ expiresIn: 86400 });
        const url = String(row.url || '').trim();
        if (url) {
            LAppDefine.backgroundCycle.paths = [url];
            LAppDefine.backgroundCycle.remoteRandom = true;
            LAppDefine.backgroundCycle.displayName = String(row.name || '');
            console.log('从后端加载随机背景:', row.name);
            return;
        }
    } catch (e) {
        console.warn('后端随机背景不可用，回退本地 background_order.json', e);
    }

    LAppDefine.backgroundCycle.remoteRandom = false;
    LAppDefine.backgroundCycle.displayName = '';

    try {
        const res = await fetch(
            `${LAppDefine.ResourcesPath}background/background_order.json`
        );
        if (res.ok) {
            const data = await res.json();
            const list = Array.isArray(data.images) ? data.images : [];
            if (list.length > 0) {
                LAppDefine.backgroundCycle.paths = list.map((p) => {
                    const s = String(p).replace(/^\//, '');
                    return s.startsWith('background/')
                        ? s
                        : `background/${s}`;
                });
            }
        }
    } catch (e) {
        console.warn('未加载 background_order.json，将使用内置轮换列表', e);
    }
    if (!LAppDefine.backgroundCycle.paths?.length) {
        LAppDefine.backgroundCycle.paths = [
            ...LAppDefine.BackgroundCyclePathsFallback
        ];
    }
    if (
        !LAppDefine.backgroundCycle.remoteRandom &&
        LAppDefine.backgroundCycle.paths?.length
    ) {
        const first = LAppDefine.backgroundCycle.paths[0];
        if (first && !String(first).startsWith('http')) {
            LAppDefine.backgroundCycle.displayName = stemFromBackgroundRel(first);
        }
    }
}

/**
 * 登录用户：按包拉取 live2d_model_asset，构建 relative_path -> 可 fetch 的 URL。
 * 私有 MinIO 下 DB 里的 public_url 多为「无签名直链」会 403：
 * 对有 object_key 的行调用 GET /live2d-model-assets/download-url 换 presigned URL。
 * 若你用网关嵌套代理，设置 VITE_DOWNLOAD_SHARED_OBJECT_BASE（见 storageFetchUrl.js）。
 */
const _PRESIGN_CONCURRENCY = 24;
const _PRESIGN_EXPIRES_SEC = 86400;

async function _resolveOneAssetFetchUrl(asset) {
    let url = String(asset.public_url ?? '').trim();
    const assetId = Number(asset.asset_id);
    const objectKey = asset.object_key && String(asset.object_key).trim();

    if (assetId > 0 && objectKey && !looksLikePresignedStorageUrl(url)) {
        try {
            const data = await getAssetDownloadUrl(assetId, _PRESIGN_EXPIRES_SEC);
            if (data && data.url) {
                url = String(data.url).trim();
            }
        } catch (e) {
            console.warn(`[live2d] presign 失败 asset_id=${assetId}，沿用 public_url`, e);
        }
    }

    return applyOptionalSharedDownloadProxy(url);
}

async function _resolveAssetUrlsWithConcurrency(assets) {
    const list = Array.isArray(assets) ? assets : [];
    const results = new Array(list.length);
    let cursor = 0;

    async function worker() {
        for (;;) {
            const i = cursor++;
            if (i >= list.length) {
                break;
            }
            results[i] = await _resolveOneAssetFetchUrl(list[i]);
        }
    }

    const n = Math.min(_PRESIGN_CONCURRENCY, list.length);
    await Promise.all(Array.from({ length: n > 0 ? n : 0 }, () => worker()));
    return results;
}

async function ensureLive2dRemoteManifests(userId, packageKeys) {
    LAppDefine.clearRemotePackageManifests();
    if (!userId || !packageKeys?.length) {
        return;
    }
    await Promise.all(
        packageKeys.map(async (pk) => {
            try {
                const assets = await getModelAssets(userId, pk);
                if (!Array.isArray(assets) || assets.length === 0) {
                    return;
                }
                const resolvedUrls = await _resolveAssetUrlsWithConcurrency(assets);

                const map = Object.create(null);
                let entry = null;
                for (let idx = 0; idx < assets.length; idx++) {
                    const a = assets[idx];
                    const rel = String(a.relative_path ?? '')
                        .replace(/\\/g, '/')
                        .replace(/^\/+/, '');
                    const fetchUrl = resolvedUrls[idx];
                    if (!rel || !fetchUrl) {
                        continue;
                    }
                    map[rel] = fetchUrl;
                    if (Number(a.is_entry_model) === 1) {
                        entry = rel;
                    }
                }
                if (Object.keys(map).length > 0) {
                    LAppDefine.setRemotePackageManifest(pk, map, entry);
                }
            } catch (e) {
                console.warn(`远程模型资源映射加载失败 [${pk}]`, e);
            }
        })
    );
}

/** @returns {{ userId: number, packageKeys: string[] } | null} */
async function fetchModelPackagesFromServer() {
    const raw = localStorage.getItem("live2d_info");
    let auth = null;
    try {
        auth = raw ? JSON.parse(raw) : null;
    } catch (e) {
        auth = null;
    }
    
    if (!auth || !auth.user_id) {
        console.warn('未登录，跳过从后端获取模型');
        return null;
    }

    try {
        const packages = await getModelPackages(auth.user_id);
        if (packages && Array.isArray(packages) && packages.length > 0) {
            const packageKeys = packages.map((p) =>
                typeof p === 'string' ? p : p.package_key
            );
            return { userId: Number(auth.user_id), packageKeys };
        }
        return null;
    } catch (e) {
        console.warn('从后端获取模型失败，将使用本地模型', e);
        return null;
    }
}

function updateBackgroundStatusLabel() {
    const el = document.getElementById('bg-status');
    const del = LAppDelegate.getInstance().getFirstSubdelegate?.();
    if (!el || !del) {
        return;
    }
    el.textContent = del.getView().getBackgroundCycleLabel();
}

function updateModelStatusLabel() {
    const el = document.getElementById('model-status');
    if (!el) {
        return;
    }
    el.textContent = LAppDelegate.getInstance().getCurrentModelLabel();
}

/**
 * 浏览器加载完成后的处理
 * 
 * load事件的默认行为是重新加载页面，因此需要使用passive: true来避免阻止默认行为
 */
window.addEventListener(
    'load',
    async () => {
        await ensureBackgroundCycleList();

        try {
            const rawAuth = localStorage.getItem('live2d_info');
            const la = rawAuth ? JSON.parse(rawAuth) : null;
            if (la?.user_id != null) {
                setUserId(Number(la.user_id));
            }
        } catch (e) {
            console.warn('同步用户 ID 到 WebSocket 配置失败', e);
        }

        const serverModels = await fetchModelPackagesFromServer();
        if (serverModels?.packageKeys?.length > 0) {
            LAppDefine.setModelDir(serverModels.packageKeys);
            await ensureLive2dRemoteManifests(
                serverModels.userId,
                serverModels.packageKeys
            );
            console.log('从后端加载模型:', serverModels.packageKeys);
        } else {
            LAppDefine.clearRemotePackageManifests();
            LAppDefine.setModelDir([]);
            console.log('后端未返回模型，不加载本地模型');
        }

        if (!LAppDelegate.getInstance().initialize()) {
            return;
        }
        LAppDelegate.getInstance().run();

        // ws.js 会在模块加载时先连一次，这里在模型真正初始化后强制同步 package。
        const currentModelLabel = LAppDelegate.getInstance().getCurrentModelLabel();
        setLive2dPackage(currentModelLabel);
        reconnectChatWebSocketsForNewPackage();
        setupChatHistoryScrollPagingOnce();
        void refreshChatHistoryFromServer();

        updateBackgroundStatusLabel();
        updateModelStatusLabel();

        const modelNextBtn = document.getElementById('model-next-btn');
        if (modelNextBtn) {
            modelNextBtn.addEventListener('click', () => {
                LAppDelegate.getInstance().nextModel();
                updateModelStatusLabel();
                setLive2dPackage(LAppDelegate.getInstance().getCurrentModelLabel());
                reconnectChatWebSocketsForNewPackage();
                void refreshChatHistoryFromServer();
            });
        }

        const bgNextBtn = document.getElementById('bg-next-btn');
        if (bgNextBtn) {
            bgNextBtn.addEventListener('click', async () => {
                if (LAppDefine.backgroundCycle.remoteRandom) {
                    bgNextBtn.disabled = true;
                    try {
                        const row = await fetchRandomBackgroundImage({
                            expiresIn: 86400,
                        });
                        const url = String(row.url || '').trim();
                        if (url) {
                            LAppDefine.backgroundCycle.paths = [url];
                            LAppDefine.backgroundCycle.displayName = String(
                                row.name || ''
                            );
                            LAppDelegate.getInstance().applyBackgroundFullUrl(url);
                        }
                    } catch (e) {
                        console.warn('切换随机背景失败', e);
                    } finally {
                        bgNextBtn.disabled = false;
                        updateBackgroundStatusLabel();
                    }
                    return;
                }
                LAppDelegate.getInstance().cycleBackground();
                updateBackgroundStatusLabel();
            });
        }

        const uploadModelBtn = document.getElementById('upload-model-btn');
        if (uploadModelBtn) {
            uploadModelBtn.addEventListener('click', () => {
                location.href = '/src/pages/assetUpload.html';
            });
        }

        const uploadTtsBtn = document.getElementById('upload-tts-btn');
        if (uploadTtsBtn) {
            uploadTtsBtn.addEventListener('click', () => {
                location.href = '/src/pages/ttsUpload.html';
            });
        }

        const uploadCharacterBtn = document.getElementById('upload-character-btn');
        if (uploadCharacterBtn) {
            uploadCharacterBtn.addEventListener('click', () => {
                location.href = '/src/pages/characterDef.html';
            });
        }

        const longMemoryBtn = document.getElementById('long-memory-consolidate-btn');
        let longMemoryConsolidateResetTimer = null;
        if (longMemoryBtn) {
            longMemoryBtn.addEventListener('click', async () => {
                const defaultLabel =
                    longMemoryBtn.getAttribute('data-default-label')?.trim() ||
                    longMemoryBtn.textContent.trim() ||
                    '总结长期记忆';

                let uid = getUserId();
                try {
                    const rawAuth = localStorage.getItem('live2d_info');
                    const la = rawAuth ? JSON.parse(rawAuth) : null;
                    if (la?.user_id != null) {
                        uid = Number(la.user_id);
                    }
                } catch (_) {
                    /* ignore */
                }
                if (!uid || !Number.isInteger(uid) || uid < 1) {
                    alert('请先登录后再总结长期记忆。');
                    return;
                }

                if (longMemoryConsolidateResetTimer) {
                    clearTimeout(longMemoryConsolidateResetTimer);
                    longMemoryConsolidateResetTimer = null;
                }

                longMemoryBtn.disabled = true;
                longMemoryBtn.setAttribute('aria-busy', 'true');
                longMemoryBtn.textContent = '总结中…';

                try {
                    const data = await triggerLongMemoryConsolidateNow({
                        userId: uid,
                        packageKey: getLive2dPackage(),
                    });
                    console.info('[long-memory consolidate-now]', data);
                    longMemoryBtn.textContent = data.updated
                        ? '已更新摘要'
                        : '暂无可总结';
                    longMemoryConsolidateResetTimer = setTimeout(() => {
                        longMemoryBtn.textContent = defaultLabel;
                        longMemoryBtn.disabled = false;
                        longMemoryBtn.removeAttribute('aria-busy');
                        longMemoryConsolidateResetTimer = null;
                    }, 3600);
                } catch (e) {
                    longMemoryBtn.textContent = defaultLabel;
                    longMemoryBtn.disabled = false;
                    longMemoryBtn.removeAttribute('aria-busy');
                    alert(
                        e && e.message
                            ? String(e.message)
                            : '触发长期记忆总结失败，请确认后端与 Ollama 可用。'
                    );
                }
            });
        }

        const remindScanBtn = document.getElementById('remind-scan-now-btn');
        const remindScanLabelEl = remindScanBtn?.querySelector('.toolbar-btn-care__label');
        let remindScanResetTimer = null;

        function setRemindScanBtnLabel(text) {
            if (remindScanLabelEl) {
                remindScanLabelEl.textContent = text;
            } else if (remindScanBtn) {
                remindScanBtn.textContent = text;
            }
        }

        if (remindScanBtn) {
            remindScanBtn.addEventListener('click', async () => {
                const defaultLabel =
                    remindScanBtn.getAttribute('data-default-label')?.trim() ||
                    remindScanLabelEl?.textContent?.trim() ||
                    '立即扫描到期项';

                if (remindScanResetTimer) {
                    clearTimeout(remindScanResetTimer);
                    remindScanResetTimer = null;
                }

                remindScanBtn.disabled = true;
                remindScanBtn.setAttribute('aria-busy', 'true');
                setRemindScanBtnLabel('扫描中…');

                try {
                    const data = await triggerRemindScanNow();
                    const pf = Number(data.pending_fetched) || 0;
                    const dv = Number(data.delivered) || 0;
                    const rel = Number(data.released_no_ws) || 0;
                    console.info('[remind scan-now]', data);
                    setRemindScanBtnLabel(
                        pf === 0 && dv === 0
                            ? '无到期待扫'
                            : `待取 ${pf} · 已推送 ${dv}${rel ? ` · 离线 ${rel}` : ''}`
                    );
                    remindScanResetTimer = setTimeout(() => {
                        setRemindScanBtnLabel(defaultLabel);
                        remindScanBtn.disabled = false;
                        remindScanBtn.removeAttribute('aria-busy');
                        remindScanResetTimer = null;
                    }, 3600);
                } catch (e) {
                    setRemindScanBtnLabel(defaultLabel);
                    remindScanBtn.disabled = false;
                    remindScanBtn.removeAttribute('aria-busy');
                    alert(
                        e && e.message
                            ? String(e.message)
                            : '关怀扫描请求失败，请确认后端可用。'
                    );
                }
            });
        }

        const live2dToolbar = document.getElementById('live2d-toolbar');
        const toolbarGearBtn = document.getElementById('toolbar-gear-btn');
        if (live2dToolbar && toolbarGearBtn) {
            toolbarGearBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                const open = live2dToolbar.classList.toggle('is-expanded');
                toolbarGearBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
                toolbarGearBtn.setAttribute(
                    'aria-label',
                    open ? '收起工具' : '展开工具'
                );
            });
            document.addEventListener('click', (e) => {
                if (!live2dToolbar.classList.contains('is-expanded')) {
                    return;
                }
                if (!live2dToolbar.contains(e.target)) {
                    live2dToolbar.classList.remove('is-expanded');
                    toolbarGearBtn.setAttribute('aria-expanded', 'false');
                    toolbarGearBtn.setAttribute('aria-label', '展开工具');
                }
            });
        }

        // 录音按钮和状态元素
        const recordBtn = document.getElementById('record-btn');
        const recordStatus = document.getElementById('record-status');
        const chatInput = document.getElementById('chat-input');
        const sendBtn = document.getElementById('send-btn');
        
        // 前置检查：元素是否存在
        if (!recordBtn || !recordStatus) {
            console.warn("未找到录音按钮或状态显示元素，请检查DOM ID是否正确");
            return;
        }

        const sendTextMessage = () => {
            if (!chatInput) return;

            const text = chatInput.value.trim();
            if (!text) return;

            if (sendChatMessage(text)) {
                appendChatMessage("user", text);
                chatInput.value = '';
                recordStatus.textContent = '已发送';
            } else {
                alert('/ws/chat 未连接，请确认后端已启动或稍后重试。');
                recordStatus.textContent = '发送失败（未连接）';
            }
        };

        if (sendBtn) {
            sendBtn.addEventListener('click', sendTextMessage);
        }

        if (chatInput) {
            chatInput.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    sendTextMessage();
                }
            });
        }

        // 按钮点击事件
        recordBtn.addEventListener('click', async () => {
            // ✅ 检查是否支持语音识别
            if (!SpeechRecognition.isSupported()) {
                alert("当前环境不支持语音识别（需要麦克风与 WebSocket）");
                return;
            }

            // 正在录音/识别 → 停止
            if (AudioRecorder.isRecording() || SpeechRecognition.isRecognizingNow()) {
                recordStatus.textContent = '处理中...';
                
                // 停止录音和识别（增加异常捕获）
                try {
                    await AudioRecorder.stop();
                    SpeechRecognition.stop();
                } catch (e) {
                    console.error("停止/识别失败:", e);
                }
                
                recordStatus.textContent = '点击';
               
            } 
            // 未录音 → 开始
            else {
                recordStatus.textContent = '请求麦克风权限...';
                
                try {
                    // 先启动录音
                    await AudioRecorder.start();
                    
                    
                    // ================================ 开始语音识别 ================================
                    const startSuccess = await SpeechRecognition.start((text, isFinal) => {
                        // 更新识别状态显示
                        if (text) {
                            recordStatus.textContent = isFinal ? `识别完成: ${text}` : `录音中: ${text}`;
                        } else {
                            recordStatus.textContent = '录音中...（请说话）';
                        }
                        
                        // 如果是最终结果，发送到WebSocket
                        if (isFinal && text) {
                            recordStatus.textContent = `正在发送: ${text}`;
                            if (sendChatMessage(text)) {
                                appendChatMessage("user", text);
                                recordStatus.textContent = `已发送: ${text}`;
                            } else {
                                alert(
                                    '/ws/chat 未连接。请确认 python main.py 已运行，或等待几秒后自动重连再试。'
                                );
                                recordStatus.textContent = `发送失败（未连接）: ${text}`;
                            }
                        }
                    });

                    // 检查识别是否启动成功
                    if (!startSuccess) {
                        recordStatus.textContent = '识别启动失败';
                        await AudioRecorder.stop(); // 启动失败则停止录音
                    }
                } catch (audioError) {
                    // 录音启动失败处理
                    console.error("录音启动失败:", audioError);
                    alert(`录音启动失败：${audioError.message}`);
                    recordStatus.textContent = '点击开始录音';
                }
            }
        });
    },
    { passive: true }
);

/**
 * 页面关闭时的处理
 * 
 * beforeunload事件的默认行为是阻止页面关闭，因此需要使用passive: true来避免阻止默认行为
 */
window.addEventListener(
    'beforeunload',
    () => {
        // 页面关闭时停止识别和录音
        SpeechRecognition.stop();
        AudioRecorder.stop().catch(e => console.error("页面关闭时停止录音失败:", e));
        LAppDelegate.releaseInstance();
    },
    { passive: true }
);