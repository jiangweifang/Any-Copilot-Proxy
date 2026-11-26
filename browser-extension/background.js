"use strict";

const HOST = "io.github.copilot_proxy.m365";
const CHAT_HOSTS = new Set(["substrate.office.com", "substrate.svc.cloud.microsoft"]);
const SCENARIOS = new Set(["OfficeWebIncludedCopilot", "OfficeWebPaidCopilot", "OfficeWebPremiumConsumerCopilot"]);
const PAGE_ORIGINS = new Set(["https://m365.cloud.microsoft", "https://copilot.cloud.microsoft", "https://copilot.com", "https://www.copilot.com",
    "https://copilot.microsoft.com", "https://www.office.com", "https://www.microsoft365.com"]);
const RENEW_MARGIN_MS = 12 * 60000;
const RENEW_WAIT_MS = 2 * 60000;
const RENEW_RETRY_MS = [2 * 60000, 4 * 60000, 2 * 60000];
let queue = Promise.resolve();
let latestRequest = null;
let captureScheduled = false;

function extractCapture(details) {
    const url = new URL(details.url);
    if (!CHAT_HOSTS.has(url.hostname) || (url.port && url.port !== "443") || url.username || url.password) return null;
    let token;
    let endpoint = null;
    if (url.protocol === "wss:" && /^\/m365Copilot\/Chathub\w*\/[A-Za-z0-9_-]+@[A-Za-z0-9_-]+\/?$/i.test(decodeURIComponent(url.pathname))) {
        token = url.searchParams.get("access_token");
        const scenario = url.searchParams.get("scenario");
        if (scenario && !SCENARIOS.has(scenario)) return null;
        const clean = new URL(url.origin + url.pathname);
        if (scenario) clean.searchParams.set("scenario", scenario);
        endpoint = clean.href;
    } else if (url.protocol === "https:" && /^\/m365Copilot\//i.test(url.pathname)) {
        const header = (details.requestHeaders || []).find(item => item.name.toLowerCase() === "authorization");
        token = header && /^Bearer\s+([A-Za-z0-9_.-]+)$/i.exec(header.value || "")?.[1];
        if (token && token.split(".").length !== 3) return null;
    }
    if (typeof token !== "string" || token.length > 16384 || !/^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*){2,4}$/.test(token)) return null;
    const encrypted = token.split(".").length === 5;
    if (!encrypted) {
        let claims;
        try {
            const part = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
            claims = JSON.parse(atob(part));
        } catch {
            return null;
        }
        const audience = claims.aud;
        if (audience === "https://graph.microsoft.com" || audience === "00000003-0000-0000-c000-000000000000") return null;
        if (!["https://substrate.office.com", "https://substrate.office.com/", "https://substrate.svc.cloud.microsoft",
            "https://substrate.svc.cloud.microsoft/"].includes(audience) && !/sydney|m365chat/i.test(claims.scp || claims.scope || "")) return null;
        if (typeof claims.exp !== "number" || claims.exp * 1000 <= Date.now() + 30000) return null;
    }
    const origin = PAGE_ORIGINS.has(details.initiator) ? details.initiator : encrypted ? "https://copilot.com" : "https://m365.cloud.microsoft";
    return {accessToken: token, endpoint, origin, userAgent: navigator.userAgent, capturedAt: Date.now()};
}

async function native(message) {
    let reply;
    const {port = 0} = await chrome.storage.local.get("port");
    if (!Number.isInteger(port) || port < 0 || port > 65535) throw new Error("管理端口无效，请在连接设置中填写 0 到 65535。");
    try {
        reply = await chrome.runtime.sendNativeMessage(HOST, {...message, port});
    } catch {
        await chrome.storage.session.set({connected: false, hostAvailable: false});
        throw new Error("无法连接本地授权组件，请确认已完成扩展注册。");
    }
    if (!reply || reply.ok !== true) {
        const reason = typeof reply?.error === "string" ? reply.error : "";
        const unavailable = /Cannot (reach|connect)|Native Host|Control port/.test(reason);
        await chrome.storage.session.set({connected: !unavailable, hostAvailable: true});
        if (/synchronization is paused/.test(reason)) await chrome.storage.session.set({paused: true});
        throw new Error(nativeError(reason));
    }
    await chrome.storage.session.set({connected: reply.result.serviceRunning !== false, hostAvailable: true});
    return reply.result;
}

function nativeError(reason) {
    if (/synchronization is paused/.test(reason)) return "主界面已暂停 M365 授权同步，请在主界面重新启用。";
    if (/Control port/.test(reason)) return "管理端口不一致或无效，请与主程序保持一致，或设为 0 自动发现。";
    if (/Cannot (reach|connect)|Native Host/.test(reason)) return "本地网关未运行或尚未注册，请启动本项目后重试。";
    if (/different M365 account/.test(reason)) return "检测到其他 M365 账号，请先断开本地授权，再连接新账号。";
    if (/disconnected|outdated/.test(reason)) return "本地授权已断开，请重新点击“连接此安装”。";
    if (/expired|about to expire/.test(reason)) return "授权已过期或即将到期，请打开 M365 网页重新登录。";
    if (/older M365 capture/.test(reason)) return "收到较旧的授权，已保留当前较新的授权。";
    if (/Substrate|Sydney/.test(reason)) return "当前不是 M365 聊天授权，请打开 M365 Copilot 网页重新获取。";
    if (/encrypted token requires/.test(reason)) return "个人账号授权缺少聊天会话，请在 M365 网页开始一次对话后重试。";
    if (/capture|token|identity|origin|scenario|endpoint/i.test(reason)) return "授权信息未通过校验，请刷新 M365 网页重新获取。";
    return "本地网关未能完成操作，请检查本项目状态后重试。";
}

async function reportError(error) {
    const text = error instanceof Error && /[\u3400-\u9fff]/.test(error.message) ?
        error.message : "M365 授权同步失败，请重新检查连接。";
    await chrome.storage.session.set({error: text});
}

async function acceptCapture(details) {
    const {paused} = await chrome.storage.session.get("paused");
    if (paused) return;
    const {linkId} = await chrome.storage.local.get("linkId");
    if (!linkId) return;
    const {capture} = await chrome.storage.session.get("capture");
    const candidate = extractCapture(details);
    if (!candidate || candidate.accessToken === capture?.accessToken) return;
    const status = await native({action: "capture", profile: candidate, linkId});
    if (!status.linked) throw new Error("同步已断开，请重新点击“连接此安装”。");
    const {renewal} = await chrome.storage.session.get("renewal");
    const {autoRenewal} = await chrome.storage.local.get("autoRenewal");
    const renewed = status.capturedAt > Math.max(renewal?.baselineAt || 0, autoRenewal?.baselineAt || 0) &&
        Boolean(renewal?.baselineAt || autoRenewal?.baselineAt || renewal?.state === "waiting");
    await chrome.storage.session.set({capture: {...candidate, capturedAt: status.capturedAt}, status, linked: true, error: "", recoveryMessage: "",
        ...(Number.isInteger(details.tabId) && details.tabId >= 0 ? {sourceTabId: details.tabId} : {}),
        ...(renewed ?
            {renewal: {state: "updated", updatedAt: Date.now()}} : {})});
    if (renewed) {
        await chrome.storage.local.remove("autoRenewal");
        await closeRenewalTab();
    }
}

function schedule(operation) {
    queue = queue.then(operation).catch(reportError);
    return queue;
}

const urls = ["https://substrate.office.com/*", "https://substrate.svc.cloud.microsoft/*",
    "wss://substrate.office.com/*", "wss://substrate.svc.cloud.microsoft/*"];
function onRequest(details) {
    // Keep at most one waiting request while the native process is responding.
    if (!extractCapture(details)) return;
    latestRequest = details;
    if (captureScheduled) return;
    captureScheduled = true;
    void schedule(async () => {
        try {
            while (latestRequest) {
                const next = latestRequest;
                latestRequest = null;
                await acceptCapture(next);
            }
        } finally {
            captureScheduled = false;
            latestRequest = null;
        }
    });
}
chrome.webRequest.onBeforeRequest.addListener(onRequest, {urls});
chrome.webRequest.onBeforeSendHeaders.addListener(onRequest, {urls}, ["requestHeaders"]);

async function refreshStatus() {
    const status = await native({action: "status"});
    const {linkId} = await chrome.storage.local.get("linkId");
    const linked = Boolean(linkId && status.linked && status.linkId === linkId);
    const paused = status.syncEnabled === false;
    await chrome.storage.session.set({status, linked, paused, error: ""});
    if (paused) {
        await closeRenewalTab();
        await chrome.storage.local.remove("autoRenewal");
        await chrome.storage.session.set({renewal: {state: "paused"}});
    }
    if (!linked) {
        await chrome.storage.session.remove(["capture", "renewal", "sourceTabId"]);
        await chrome.storage.local.remove(["linkId", "autoRenewal"]);
        await closeRenewalTab();
    }
    const {capture} = await chrome.storage.session.get("capture");
    if (linked && !paused && capture && (!status.capturedAt || capture.capturedAt > status.capturedAt)) {
        const updated = await native({action: "capture", profile: capture, linkId});
        await chrome.storage.session.set({status: updated});
    }
    return status;
}

async function publicSnapshot() {
    const stored = await chrome.storage.session.get(["status", "linked", "connected", "hostAvailable", "error", "capture", "renewal", "recoveryMessage", "paused"]);
    const {port = 0} = await chrome.storage.local.get("port");
    const status = stored.status;
    const canCopy = stored.hostAvailable === true && stored.linked === true && status?.state === "captured" &&
        (!status.expiresAt || status.expiresAt * 1000 > Date.now() + 30000) &&
        Boolean(stored.capture?.accessToken && stored.capture.capturedAt === status.capturedAt);
    const error = typeof stored.error === "string" && stored.error ?
        /[\u3400-\u9fff]/.test(stored.error) ? stored.error : nativeError(stored.error) : "";
    return {connected: stored.connected === true, hostAvailable: stored.hostAvailable === true,
        linked: stored.linked === true, error, recoveryMessage: stored.recoveryMessage || "",
        canCopy, paused: stored.paused === true, controlPort: port,
        autoRenewalEnabled: stored.linked === true && !stored.paused, renewal: stored.renewal || {state: "idle"},
        status: status ? {state: status.state, accountKind: status.accountKind, username: status.username,
            expiresAt: status.expiresAt, capturedAt: status.capturedAt, verification: status.verification} : null};
}

async function findCopilotTabs() {
    const tabs = await chrome.tabs.query({url: [...PAGE_ORIGINS].filter(origin => /copilot\.|m365\.cloud/.test(origin))
        .map(origin => origin + "/*")});
    return tabs.filter(tab => {
        if (!Number.isInteger(tab.id) || !tab.url) return false;
        const url = new URL(tab.url);
        return PAGE_ORIGINS.has(url.origin) && (url.pathname === "/" || /^\/(chat|home)(\/|$)/.test(url.pathname));
    });
}

async function openCopilot(renew = false) {
    const {sourceTabId} = await chrome.storage.session.get("sourceTabId");
    const candidates = await findCopilotTabs();
    const tab = candidates.find(item => item.id === sourceTabId) || candidates.find(item => item.active) || candidates[0];
    if (tab) {
        await chrome.tabs.update(tab.id, {active: true});
        if (renew) await chrome.tabs.reload(tab.id);
    } else {
        await chrome.tabs.create({url: "https://m365.cloud.microsoft/chat/", active: true});
    }
}

async function closeRenewalTab() {
    const {renewalTabId} = await chrome.storage.session.get("renewalTabId");
    if (!Number.isInteger(renewalTabId)) return;
    await chrome.storage.session.remove("renewalTabId");
    const tab = (await findCopilotTabs()).find(item => item.id === renewalTabId);
    if (tab && !tab.active) {
        try {
            await chrome.tabs.remove(tab.id);
        } catch {
            throw new Error("后台续期页未能关闭，请手动关闭该页面。");
        }
    }
}

function automaticRenewalDecision(status, progress, now = Date.now()) {
    if (!status?.capturedAt) return "none";
    if (status.state !== "needs-login" &&
        (!Number.isFinite(status.expiresAt) || status.expiresAt * 1000 - now > RENEW_MARGIN_MS)) return "none";
    const attempts = progress?.attempts || 0;
    if (attempts && now - progress.lastAttemptAt < RENEW_WAIT_MS) return "waiting";
    if (attempts >= RENEW_RETRY_MS.length) return "needs-user";
    if (attempts && now < progress.nextAttemptAt) return "retrying";
    return "renew";
}

async function renewAutomatically() {
    const {linked, status, renewal} = await chrome.storage.session.get(["linked", "status", "renewal"]);
    const {linkId, autoRenewal} = await chrome.storage.local.get(["linkId", "autoRenewal"]);
    if (!linked || !linkId || status?.syncEnabled === false) return;
    const stamp = `${status?.capturedAt || 0}:${status?.expiresAt || 0}`;
    const progress = autoRenewal?.stamp === stamp && autoRenewal.linkId === linkId ? autoRenewal : null;
    const now = Date.now();
    if (renewal?.state === "waiting" && !renewal.automatic && now - renewal.startedAt < RENEW_WAIT_MS) return;
    const decision = automaticRenewalDecision(status, progress, now);
    if (decision === "none") {
        if (autoRenewal && !progress) {
            await chrome.storage.local.remove("autoRenewal");
            await chrome.storage.session.set({renewal: {state: status?.capturedAt > autoRenewal.baselineAt ? "updated" : "idle", updatedAt: now}});
            await closeRenewalTab();
        }
        return;
    }
    if (decision !== "renew") {
        await chrome.storage.session.set({renewal: {state: decision, automatic: true, startedAt: progress.lastAttemptAt,
            nextAttemptAt: progress.nextAttemptAt, baselineAt: status.capturedAt, attempts: progress.attempts}});
        return;
    }
    const attempts = (progress?.attempts || 0) + 1;
    const nextAttemptAt = now + RENEW_RETRY_MS[attempts - 1];
    // Persist the attempt before opening a page so a sleeping worker cannot restart a retry loop.
    await chrome.storage.local.set({autoRenewal: {linkId, stamp, attempts, lastAttemptAt: now,
        nextAttemptAt, baselineAt: status.capturedAt}});
    await chrome.storage.session.set({renewal: {state: "waiting", automatic: true, startedAt: now,
        nextAttemptAt, baselineAt: status.capturedAt, attempts}});
    try {
        const {renewalTabId} = await chrome.storage.session.get("renewalTabId");
        const owned = (await findCopilotTabs()).find(tab => tab.id === renewalTabId && !tab.active);
        if (owned) {
            await chrome.tabs.reload(owned.id);
        } else {
            await chrome.storage.session.remove("renewalTabId");
            const tab = await chrome.tabs.create({url: "https://m365.cloud.microsoft/chat/", active: false});
            if (!Number.isInteger(tab.id)) throw new Error("Missing renewal tab ID");
            await chrome.storage.session.set({renewalTabId: tab.id});
        }
    } catch {
        await chrome.storage.session.set({renewal: {state: "retrying", automatic: true,
            startedAt: now, nextAttemptAt, baselineAt: status.capturedAt, attempts}});
        throw new Error("后台续期未能启动，稍后会自动重试；仍可手动打开 M365 登录。");
    }
}

async function checkAuthorization() {
    const {linkId} = await chrome.storage.local.get("linkId");
    if (!linkId) return;
    await refreshStatus();
    await recoverFromPages();
    await renewAutomatically();
}

function readPageAccessToken(expectedAccount, minimumExpiry = 0) {
    const accounts = new Map();
    let inaccessible = false;
    for (const storageName of ["localStorage", "sessionStorage"]) {
        let storage;
        const keys = new Set();
        try {
            storage = window[storageName];
            for (let i = 0; i < Math.min(storage.length, 1024); i++) {
                const key = storage.key(i);
                if (!key) continue;
                if (/^msal\.token\.keys\./i.test(key)) {
                    const raw = storage.getItem(key);
                    if (!raw || raw.length > 32768) continue;
                    let index;
                    try { index = JSON.parse(raw); } catch { continue; }
                    if (Array.isArray(index?.accessToken)) {
                        for (const entry of index.accessToken.slice(0, 64)) {
                            if (typeof entry === "string" && entry.length <= 2048 && /-accesstoken-/i.test(entry)) keys.add(entry);
                        }
                    }
                } else if (/-accesstoken-/i.test(key) && /substrate|sydney|m365chat/i.test(key)) {
                    keys.add(key);
                }
            }
        } catch {
            inaccessible = true;
            continue;
        }
        for (const key of [...keys].slice(0, 64)) {
            let record;
            try {
                const raw = storage.getItem(key);
                if (!raw || raw.length > 32768) continue;
                record = JSON.parse(raw);
            } catch {
                continue;
            }
            // Only indexed access tokens, never refresh tokens or another extension's cache.
            if (!record || typeof record.credentialType !== "string" || record.credentialType.toLowerCase() !== "accesstoken" ||
                typeof record.secret !== "string" || record.secret.length > 16384) continue;
            const parts = record.secret.split(".");
            if (parts.length !== 3) continue;
            let claims;
            try { claims = JSON.parse(atob(parts[1].replace(/-/g, "+").replace(/_/g, "/"))); } catch { continue; }
            if (!claims || typeof claims !== "object") continue;
            if (!["https://substrate.office.com", "https://substrate.office.com/",
                "https://substrate.svc.cloud.microsoft", "https://substrate.svc.cloud.microsoft/"].includes(claims.aud) &&
                !/sydney|m365chat/i.test(typeof claims.scp === "string" ? claims.scp : "")) continue;
            if (["https://graph.microsoft.com", "00000003-0000-0000-c000-000000000000"].includes(claims.aud)) continue;
            if (typeof claims.oid !== "string" || typeof claims.tid !== "string" ||
                !/^[A-Za-z0-9_-]{1,128}$/.test(claims.oid) || !/^[A-Za-z0-9_-]{1,128}$/.test(claims.tid)) continue;
            if (!Number.isInteger(claims.exp) || claims.exp < minimumExpiry || claims.exp * 1000 <= Date.now() + 30000) continue;
            const identity = `${claims.oid}@${claims.tid}`;
            if (expectedAccount && identity !== expectedAccount) continue;
            if (!accounts.has(identity) || accounts.get(identity).expires < claims.exp) {
                accounts.set(identity, {token: record.secret, expires: claims.exp});
            }
        }
    }
    if (accounts.size !== 1) return {reason: accounts.size > 1 ? "multiple-accounts" : inaccessible ? "inaccessible" : "missing"};
    return {token: [...accounts.values()][0].token, origin: location.origin};
}

async function recoverFromPages(force = false) {
    const {linked, capture, status} = await chrome.storage.session.get(["linked", "capture", "status"]);
    if (!linked || status?.syncEnabled === false) return false;
    if (!force && capture && status?.state === "captured" &&
        (!status.expiresAt || status.expiresAt * 1000 > Date.now() + RENEW_MARGIN_MS)) return false;
    let hint = "";
    let found = false;
    const tabs = await findCopilotTabs();
    for (const tab of tabs) {
        let frames;
        try {
            frames = await chrome.scripting.executeScript({target: {tabId: tab.id, allFrames: true},
                func: readPageAccessToken, args: [status?.accountId || null, status?.expiresAt || 0]});
        } catch {
            hint = "无法读取网页已有授权，请打开 M365 页面后重试。";
            continue;
        }
        for (const frame of frames) {
            if (frame.error) {
                hint = "网页授权读取失败，请刷新 M365 页面后重试。";
                continue;
            }
            const result = frame.result;
            if (!result?.token || !PAGE_ORIGINS.has(result.origin)) {
                if (result?.reason === "multiple-accounts") hint = "页面存在多个账号，请在目标账号的 M365 页面刷新。";
                if (result?.reason === "inaccessible") hint = "页面未允许读取授权，请刷新 M365 页面后重试。";
                continue;
            }
            await acceptCapture({url: "https://substrate.office.com/m365Copilot/getgptlist", initiator: result.origin,
                requestHeaders: [{name: "Authorization", value: `Bearer ${result.token}`}], tabId: tab.id});
            const current = await chrome.storage.session.get("status");
            found = current.status?.state === "captured";
            if (found) {
                await chrome.storage.session.set({recoveryMessage: ""});
                return (current.status.capturedAt || 0) > (status?.capturedAt || 0);
            }
        }
    }
    await chrome.storage.session.set({recoveryMessage: hint || (tabs.length ?
        "网页暂未提供可用授权，可点击“获取授权”或“立即续期”；个人账号可能需要在网页发起一次聊天。" :
        "请在当前浏览器中打开 M365 Copilot。")});
    return false;
}

async function popupAction(message) {
    if (!message || typeof message !== "object") throw new Error("无效的扩展操作。");
    const action = message.action;
    if (action === "snapshot") return;
    await chrome.storage.session.set({error: ""});
    if (action === "set-port") {
        if (!Number.isInteger(message.port) || message.port < 0 || message.port > 65535) {
            throw new Error("管理端口须为 0（自动）或 1 到 65535 之间的整数。");
        }
        await chrome.storage.local.set({port: message.port});
    } else if (action === "connect") {
        const {linkId} = await chrome.storage.local.get("linkId");
        const status = await native({action: "connect"});
        if (linkId !== status.linkId) {
            await chrome.storage.session.remove(["capture", "renewal", "sourceTabId", "recoveryMessage"]);
            await chrome.storage.local.remove("autoRenewal");
            await closeRenewalTab();
        }
        await chrome.storage.local.set({linkId: status.linkId});
        await chrome.storage.session.set({linked: status.linked, status});
    } else if (action === "disconnect") {
        const {linkId} = await chrome.storage.local.get("linkId");
        await native({action: "disconnect", linkId});
        await chrome.storage.local.remove(["linkId", "autoRenewal"]);
        try {
            await closeRenewalTab();
        } finally {
            await chrome.storage.session.clear();
        }
    } else if (action === "open") {
        try {
            await openCopilot();
        } catch {
            throw new Error("无法打开 M365 网页，请在当前浏览器中手动打开。");
        }
        return;
    } else if (!["status", "sync", "renew", "copy"].includes(action)) {
        throw new Error("不支持此扩展操作。");
    }
    await refreshStatus();
    const current = await publicSnapshot();
    if (current.paused && ["connect", "sync", "renew"].includes(action)) {
        throw new Error("主界面已暂停 M365 授权同步，请在主界面重新启用。");
    }
    const recovered = action !== "disconnect" && await recoverFromPages(action === "renew");
    if (action === "renew") {
        const snapshot = await publicSnapshot();
        if (!snapshot.hostAvailable || !snapshot.linked) throw new Error("请先点击“连接此安装”，再获取新的授权。");
        if (recovered) {
            await chrome.storage.session.set({renewal: {state: "updated", updatedAt: Date.now()}});
            return;
        }
        if (snapshot.renewal.state === "waiting" && Date.now() - snapshot.renewal.startedAt < RENEW_WAIT_MS) return;
        await chrome.storage.local.remove("autoRenewal");
        await closeRenewalTab();
        await chrome.storage.session.set({renewal: {state: "waiting", startedAt: Date.now(), baselineAt: snapshot.status?.capturedAt || 0}});
        try {
            await openCopilot(true);
        } catch {
            await chrome.storage.session.set({renewal: {state: "failed"}});
            throw new Error("无法刷新 M365 网页，请手动打开并重新登录。");
        }
    }
}

chrome.tabs.onActivated.addListener(({tabId}) => {
    void schedule(async () => {
        const {renewalTabId} = await chrome.storage.session.get("renewalTabId");
        if (renewalTabId === tabId) await chrome.storage.session.remove("renewalTabId");
    });
});
chrome.tabs.onRemoved.addListener(tabId => {
    void schedule(async () => {
        const {renewalTabId} = await chrome.storage.session.get("renewalTabId");
        if (renewalTabId === tabId) await chrome.storage.session.remove("renewalTabId");
    });
});

chrome.runtime.onMessage.addListener((message, sender, respond) => {
    if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL("popup.html")) return false;
    void schedule(async () => {
        let token;
        try {
            await popupAction(message);
            if (message.action === "copy") {
                if (!(await publicSnapshot()).canCopy) throw new Error("当前没有可复制的有效令牌，请先获取或续期。");
                const {capture} = await chrome.storage.session.get("capture");
                token = capture.accessToken;
            }
        } catch (error) {
            await reportError(error);
        }
        respond({...await publicSnapshot(), ...(token ? {token} : {})});
    });
    return true;
});

chrome.alarms.onAlarm.addListener(alarm => {
    if (alarm.name === "m365-status") {
        void schedule(checkAuthorization);
    }
});
void chrome.alarms.create("m365-status", {periodInMinutes: 1});
void schedule(checkAuthorization);
