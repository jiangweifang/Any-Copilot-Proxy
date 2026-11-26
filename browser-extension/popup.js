"use strict";

function captureAge(timestamp, now) {
    if (!Number.isFinite(timestamp) || timestamp <= 0) return "尚未获取授权";
    const minutes = Math.max(0, Math.floor((now - timestamp) / 60000));
    if (minutes < 1) return "刚刚获取";
    if (minutes < 60) return `${minutes} 分钟前获取`;
    if (minutes < 1440) return `${Math.floor(minutes / 60)} 小时前获取`;
    return `${Math.floor(minutes / 1440)} 天前获取`;
}

function popupView(snapshot, now = Date.now()) {
    const status = snapshot.status || {};
    const linked = snapshot.linked === true;
    const connected = snapshot.connected === true;
    const hostAvailable = snapshot.hostAvailable === true || connected;
    const loading = snapshot.connected === undefined;
    const expired = Number.isFinite(status.expiresAt) && status.expiresAt * 1000 <= now;
    const expiring = Number.isFinite(status.expiresAt) && status.expiresAt * 1000 <= now + 30000;
    const needsLogin = status.state === "needs-login" || expiring;
    const hasToken = Boolean(status.capturedAt);
    const username = typeof status.username === "string" ? status.username.trim() : "";
    const renewal = snapshot.renewal || {};
    const waiting = linked && hostAvailable && renewal.state === "waiting" && now - renewal.startedAt < 120000;
    const timedOut = renewal.state === "timeout" || renewal.state === "needs-user" ||
        (!renewal.automatic && renewal.state === "waiting" && !waiting && now - renewal.startedAt >= 120000);
    const view = {
        heading: "授权已同步", subtitle: "网页更新授权时，将自动同步到本地网关。",
        tone: "good", connected, renewing: waiting,
        account: {text: username || (hasToken ? "未提供用户名" : "未获取"), tone: username ? "good" : "muted"},
        token: {text: expired ? "已过期" : needsLogin ? expiring ? "即将到期" : "需要重新登录" :
            hasToken ? status.expiresAt ? "有效" : "有效期未知" : "未获取",
            tone: needsLogin ? "warning" : hasToken ? status.expiresAt ? "good" : "muted" : "muted"},
        gateway: {text: loading ? "检查中" : connected ? "已连接" : "未连接", tone: connected ? "good" : loading ? "muted" : "warning"},
        renewal: {text: snapshot.autoRenewalEnabled ? (Number.isFinite(status.expiresAt) ? "自动续期已启用" : "自动同步已启用") :
            linked ? "等待网页更新" : "未启用", tone: snapshot.autoRenewalEnabled ? "good" : "muted"},
        primary: {action: "sync", text: "同步授权", disabled: false},
        canCopy: hostAvailable && linked && !needsLogin && snapshot.canCopy === true,
        canDisconnect: hostAvailable && linked, captured: captureAge(status.capturedAt, now),
        notice: snapshot.error || "", noticeTone: "warning"
    };
    if (loading) {
        Object.assign(view, {heading: "正在检查连接", subtitle: "正在读取本地网关和授权状态…", tone: "muted"});
        view.primary = {action: "status", text: "正在检查…", disabled: true};
    } else if (!hostAvailable) {
        Object.assign(view, {heading: "本地授权组件未连接", subtitle: "请确认已完成扩展注册，再重新连接。", tone: "muted"});
        if (view.notice.includes("本地授权组件")) view.notice = "";
        view.primary = {action: "status", text: "重新连接", disabled: false};
        view.renewal = {text: "已暂停", tone: "muted"};
    } else if (!linked) {
        Object.assign(view, {heading: "连接你的 M365 Copilot", subtitle: "保留当前浏览器登录，一键连接本地网关。", tone: "muted"});
        view.primary = {action: "connect", text: "连接此安装", disabled: false};
    } else if (waiting) {
        Object.assign(view, {heading: renewal.automatic ? "正在自动续期" : "正在获取新授权",
            subtitle: renewal.automatic ? "正在后台获取新授权，不打断当前页面。" : "已刷新 M365 网页，等待网页完成续期。", tone: "warning"});
        view.renewal = {text: renewal.automatic ? "自动续期中" : "等待新授权", tone: "warning"};
        view.primary = {action: "renew", text: "正在续期…", disabled: true};
    } else if (timedOut || needsLogin) {
        Object.assign(view, {heading: expired ? "授权已过期" : "需要更新授权",
            subtitle: timedOut ? "尚未收到新授权，请在 M365 网页完成登录。" : "刷新 M365 网页，获取新的登录授权。", tone: "warning"});
        view.renewal = {text: timedOut ? "需要手动登录" : "等待续期", tone: "warning"};
        view.primary = {action: "renew", text: "立即续期", disabled: false};
    } else if (!hasToken) {
        Object.assign(view, {heading: "等待获取授权", subtitle: "在当前浏览器中打开 M365 Copilot 并登录。", tone: "muted"});
        view.primary = {action: "renew", text: "获取授权", disabled: false};
    } else if (renewal.state === "updated") {
        view.renewal = {text: "已更新", tone: "good"};
    }
    if (hostAvailable && !connected) {
        view.gateway = {text: "未运行", tone: "warning"};
        if (linked && hasToken && !needsLogin && !waiting && !timedOut) {
            view.heading = "授权已保存";
            view.subtitle = "网关尚未启动，授权已保存到本机。";
        }
    }
    if (!hasToken && !loading && !hostAvailable) {
        view.token = {text: "状态未知", tone: "muted"};
        view.captured = "暂时无法读取本地授权";
    }
    if (!view.notice && (!hasToken || needsLogin) && !waiting) view.notice = snapshot.recoveryMessage || "";
    if (!view.notice && status.verification === "failed") {
        view.notice = "授权验证未通过，请检查网络、订阅和组织权限，或重新登录。";
    }
    if (renewal.state === "failed" && hostAvailable && linked) view.renewal = {text: "续期失败", tone: "warning"};
    if (renewal.automatic && ["waiting", "retrying"].includes(renewal.state) && !waiting && hostAvailable && linked) {
        const minutes = Math.max(1, Math.ceil((renewal.nextAttemptAt - now) / 60000));
        view.renewal = {text: `${minutes} 分钟后重试`, tone: "warning"};
    }
    if (snapshot.paused) {
        Object.assign(view, {heading: "授权同步已暂停", subtitle: "在主界面启用 M365 授权同步后恢复。", tone: "muted", renewing: false});
        view.renewal = {text: "已暂停", tone: "muted"};
        view.primary = {action: "status", text: "主界面已暂停", disabled: true};
    }
    return view;
}

const elements = Object.fromEntries(["heading", "subtitle", "primary", "copy", "open", "disconnect", "refresh",
    "captured", "notice", "token-badge", "extension-id", "account-status", "account-text", "token-status", "token-text",
    "gateway-status", "gateway-text", "renewal-status", "renewal-text", "control-port", "save-port"].map(id => [id, document.getElementById(id)]));
elements["extension-id"].textContent = chrome.runtime.id;
let snapshot = {};
let busy = false;
let feedback = "";
let feedbackTone = "good";
let primaryAction = "status";
let portLoaded = false;

function render() {
    const view = popupView(snapshot);
    document.body.dataset.tone = view.tone;
    document.body.dataset.connected = String(view.connected);
    document.body.dataset.renewing = String(view.renewing);
    elements.heading.textContent = view.heading;
    elements.subtitle.textContent = view.subtitle;
    for (const name of ["account", "token", "gateway", "renewal"]) {
        elements[`${name}-text`].textContent = view[name].text;
        elements[`${name}-status`].dataset.tone = view[name].tone;
    }
    elements["account-status"].title = view.account.text;
    elements["renewal-status"].title = "每分钟检查，到期前 12 分钟开始自动续期；连续三次未成功后需重新登录。";
    elements["token-status"].title = snapshot.status?.expiresAt ?
        `到期时间：${new Date(snapshot.status.expiresAt * 1000).toLocaleString("zh-CN")}` : "到期时间由 Microsoft 网页与服务决定";
    elements["token-badge"].hidden = view.token.tone !== "warning";
    primaryAction = view.primary.action;
    elements.primary.textContent = busy ? "正在处理…" : view.primary.text;
    elements.primary.disabled = busy || view.primary.disabled;
    elements.copy.disabled = busy || !view.canCopy;
    elements.copy.title = view.canCopy ? "复制当前浏览器会话的令牌，请勿分享给他人" : "当前没有可复制的有效令牌，请先获取或续期";
    elements.open.disabled = busy;
    elements.disconnect.disabled = busy || !view.canDisconnect;
    elements.refresh.disabled = busy;
    elements["save-port"].disabled = busy;
    if (!portLoaded && Number.isInteger(snapshot.controlPort)) {
        elements["control-port"].value = String(snapshot.controlPort);
        portLoaded = true;
    }
    elements.captured.textContent = view.captured;
    elements.notice.textContent = feedback || view.notice;
    elements.notice.dataset.tone = feedback ? feedbackTone : view.noticeTone;
    elements.notice.hidden = !elements.notice.textContent;
}

async function send(action) {
    if (busy) return;
    busy = true;
    feedback = "";
    render();
    try {
        const result = await chrome.runtime.sendMessage({action,
            ...(action === "set-port" ? {port: Number(elements["control-port"].value)} : {})});
        if (!result || typeof result !== "object" || typeof result.connected !== "boolean") {
            throw new Error("扩展后台返回了无效状态，请重新加载扩展。");
        }
        snapshot = result;
        if (action === "set-port") portLoaded = false;
        if (action === "copy" && !result.error) {
            try {
                if (typeof result.token !== "string" || !result.token) throw new Error("missing token");
                await navigator.clipboard.writeText(result.token);
                feedback = "令牌已复制，请勿分享给他人。";
                feedbackTone = "good";
            } catch {
                feedback = "复制失败，请保持弹窗打开并允许剪贴板写入后重试。";
                feedbackTone = "warning";
            } finally {
                delete snapshot.token;
            }
        } else if (action === "sync" && !result.error) {
            feedback = "授权状态已同步。";
            feedbackTone = "good";
        } else if (action === "set-port" && !result.error) {
            feedback = "端口已保存，连接检查完成。";
            feedbackTone = "good";
        }
    } catch {
        snapshot = {...snapshot, connected: false, error: "无法读取扩展后台，请重新加载扩展后重试。"};
    } finally {
        busy = false;
        render();
    }
}

elements.primary.addEventListener("click", () => { void send(primaryAction); });
elements.copy.addEventListener("click", () => { void send("copy"); });
elements.open.addEventListener("click", () => { void send("open"); });
elements.refresh.addEventListener("click", () => { void send("status"); });
elements["save-port"].addEventListener("click", () => { void send("set-port"); });
elements.disconnect.addEventListener("click", () => {
    if (confirm("断开并清除此工具的 M365 授权？\n不会退出浏览器中的 Microsoft 账号。")) void send("disconnect");
});
chrome.storage.onChanged.addListener((changes, area) => {
    if (area !== "session" || busy || !["status", "linked", "connected", "hostAvailable", "error", "renewal", "capture", "recoveryMessage", "paused"].some(key => key in changes)) return;
    void send("snapshot");
});
setInterval(render, 15000);
render();
void send("status");
