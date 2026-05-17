import { getApiBase } from "./apiBase.js";

export async function uploadLive2dModelZip(userId, packageKey, zipFile, options = {}) {
    if (!userId || !zipFile) {
        throw new Error("缺少必要参数");
    }

    const formData = new FormData();
    formData.append("user_id", userId.toString());
    if (packageKey && packageKey.trim()) {
        formData.append("package_key", packageKey.trim());
    }
    formData.append("model_zip", zipFile);
    
    const response = await fetch(`${getApiBase()}/live2d-model-assets/upload-zip`, {
        method: "POST",
        body: formData,
    });
    
    const result = await response.json();
    
    if (result.code !== 0) {
        throw new Error(result.message || "上传失败");
    }
    
    return result.data;
}

/** 获取 HitAreas 侧栏填写模板（上传前参考） */
export async function getHitAreasTemplate(packageKey) {
    const pk = String(packageKey || "").trim();
    if (!pk) {
        throw new Error("缺少 package_key");
    }
    const response = await fetch(
        `${getApiBase()}/live2d-model-assets/hit-areas-template?package_key=${encodeURIComponent(pk)}`
    );
    const result = await response.json();
    if (result.code !== 0) {
        throw new Error(result.message || "获取 HitAreas 模板失败");
    }
    return result.data;
}

export async function getModelAssets(userId, packageKey = null, options = {}) {
    const limit = Number(options.limit) > 0 ? Number(options.limit) : 2000;
    let url = `${getApiBase()}/live2d-model-assets?user_id=${userId}&limit=${limit}`;
    if (packageKey) {
        url += `&package_key=${encodeURIComponent(packageKey)}`;
    }
    
    const response = await fetch(url);
    const result = await response.json();
    
    if (result.code !== 0) {
        throw new Error(result.message || "获取资源失败");
    }
    
    return result.data;
}

export async function getModelPackages(userId) {
    const url = `${getApiBase()}/live2d-model-assets/packages?user_id=${userId}`;
    
    const response = await fetch(url);
    const result = await response.json();
    
    if (result.code !== 0) {
        throw new Error(result.message || "获取模型包列表失败");
    }
    
    return result.data;
}

export async function deleteModelPackage(userId, packageKey) {
    const response = await fetch(
        `${getApiBase()}/live2d-model-assets/by-package/${encodeURIComponent(packageKey)}?user_id=${userId}`,
        {
            method: "DELETE",
        }
    );
    
    const result = await response.json();
    
    if (result.code !== 0) {
        throw new Error(result.message || "删除失败");
    }
    
    return result.data;
}

export async function getAssetDownloadUrl(assetId, expiresIn = 3600) {
    const response = await fetch(
        `${getApiBase()}/live2d-model-assets/download-url?asset_id=${assetId}&expires_in=${expiresIn}`
    );
    
    const result = await response.json();
    
    if (result.code !== 0) {
        throw new Error(result.message || "获取下载链接失败");
    }
    
    return result.data;
}

export async function getTtsRefers(userId, packageKey = null) {
    let url = `${getApiBase()}/live2d-tts-refers?user_id=${userId}`;
    if (packageKey) {
        url += `&package_key=${encodeURIComponent(packageKey)}`;
    }
    const response = await fetch(url);
    const result = await response.json();
    if (result.code !== 0) {
        throw new Error(result.message || "获取参考音频配置失败");
    }
    return result.data;
}

export async function uploadTtsReferAudio(userId, packageKey, audioFile, promptText, promptLanguage = "zh") {
    if (!userId || !packageKey || !audioFile || !promptText) {
        throw new Error("缺少必要参数");
    }
    const formData = new FormData();
    formData.append("user_id", userId.toString());
    formData.append("package_key", packageKey);
    formData.append("prompt_text", promptText);
    formData.append("prompt_language", promptLanguage || "zh");
    formData.append("refer_audio", audioFile);

    const response = await fetch(`${getApiBase()}/live2d-tts-refers/upload`, {
        method: "POST",
        body: formData,
    });
    const result = await response.json();
    if (result.code !== 0) {
        throw new Error(result.message || "上传参考音频失败");
    }
    return result.data;
}