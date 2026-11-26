const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {join} = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");

function harness(initial = {}) {
    const data = {...initial};
    const localData = {linkId: initial.linkId, autoRenewal: initial.autoRenewal, port: initial.port};
    const sent = [];
    const tabActions = [];
    let browserTabs = [...(initial.tabs || [])];
    const listeners = {};
    class ClockDate extends Date {
        static now() { return Number.isFinite(data.now) ? data.now : Date.now(); }
    }
    const storage = values => ({
        async get(keys) {
            if (typeof keys === "string") keys = [keys];
            return Object.fromEntries(keys.map(key => [key, values[key]]));
        },
        async set(value) { Object.assign(values, value); },
        async remove(keys) { for (const key of typeof keys === "string" ? [keys] : keys) delete values[key]; },
        async clear() { for (const key of Object.keys(values)) delete values[key]; }
    });
    const session = storage(data);
    let serverToken = initial.serverToken;
    let serverStatus = initial.status || {linked: true, linkId: initial.serverLinkId || "test-connection", state: "waiting-for-capture"};
    const chrome = {
        storage: {session, local: storage(localData)},
        runtime: {
            id: "a".repeat(32),
            getURL: path => `chrome-extension://${"a".repeat(32)}/${path}`,
            onMessage: {addListener: listener => { listeners.message = listener; }},
            async sendNativeMessage(host, message) {
                sent.push({host, message});
                if (data.nativeOffline) throw new Error("host unavailable");
                if (data.nativeError) return {ok: false, error: data.nativeError};
                if (message.port && data.activePort && message.port !== data.activePort) {
                    return {ok: false, error: "Control port does not match the running application. Use the same port or automatic discovery."};
                }
                if (message.action === "capture" && message.profile.accessToken !== serverToken) {
                    serverToken = message.profile.accessToken;
                    const parts = serverToken.split(".");
                    const claims = parts.length === 3 ? JSON.parse(Buffer.from(parts[1], "base64url").toString()) : {};
                    serverStatus = {...serverStatus, state: "captured", capturedAt: message.profile.capturedAt,
                        expiresAt: claims.exp || null};
                } else if (message.action === "disconnect") {
                    serverStatus = {linked: false, state: "disconnected", linkId: null};
                }
                return {ok: true, result: {...serverStatus, syncEnabled: data.syncEnabled !== false, serviceRunning: !data.serviceOffline}};
            }
        },
        webRequest: {
            onBeforeRequest: {addListener: listener => { listeners.request = listener; }},
            onBeforeSendHeaders: {addListener: listener => { listeners.headers = listener; }}
        },
        alarms: {onAlarm: {addListener(listener) { listeners.alarm = listener; }}, async create() {}},
        tabs: {
            onActivated: {addListener(listener) { listeners.activated = listener; }},
            onRemoved: {addListener(listener) { listeners.removed = listener; }},
            async query() { return browserTabs; },
            async update(id, options) { tabActions.push({action: "update", id, options}); },
            async reload(id) {
                if (data.reloadFailure) throw new Error("tab no longer exists");
                tabActions.push({action: "reload", id});
            },
            async create(options) {
                if (data.createFailure) throw new Error("Cannot create page");
                tabActions.push({action: "create", options});
                const tab = {id: 100 + tabActions.length, ...options};
                browserTabs.push(tab);
                return tab;
            },
            async remove(id) {
                tabActions.push({action: "remove", id});
                browserTabs = browserTabs.filter(tab => tab.id !== id);
            }
        },
        scripting: {
            async executeScript(options) {
                if (data.scanError) throw new Error("Cannot access page");
                return data.pageFrames || [];
            }
        }
    };
    const context = vm.createContext({chrome, URL, navigator: {userAgent: "TestBrowser/1"}, atob, Date: ClockDate, console});
    vm.runInContext(readFileSync(join(__dirname, "..", "browser-extension", "background.js"), "utf8"), context);
    const request = (action, extra = {}) => new Promise(resolve => {
        listeners.message({action, ...extra}, {id: chrome.runtime.id, url: chrome.runtime.getURL("popup.html")}, resolve);
    });
    const tick = async () => {
        listeners.alarm({name: "m365-status"});
        return request("snapshot");
    };
    return {context, data, localData, sent, listeners, tabActions, request, tick, get tabs() { return browserTabs; }};
}

function token(overrides = {}) {
    const enc = object => Buffer.from(JSON.stringify(object)).toString("base64url");
    return `${enc({alg: "RS256"})}.${enc({aud: "https://substrate.office.com", exp: Math.floor(Date.now() / 1000) + 3600,
        oid: "user-1", tid: "tenant-1", ...overrides})}.signature`;
}

function socket(accessToken = token()) {
    return {url: `wss://substrate.office.com/m365Copilot/Chathub/user-1@tenant-1?access_token=${accessToken}&ConversationId=private&scenario=OfficeWebIncludedCopilot`,
        initiator: "https://m365.cloud.microsoft"};
}

test("captures only chat authorization and removes volatile URL data", () => {
    const {context} = harness();
    const result = context.extractCapture(socket());
    assert.ok(result.accessToken);
    assert.equal(result.endpoint, "wss://substrate.office.com/m365Copilot/Chathub/user-1@tenant-1?scenario=OfficeWebIncludedCopilot");
    assert.equal(result.endpoint.includes("access_token"), false);
    assert.equal(context.extractCapture(socket(token({aud: "https://graph.microsoft.com"}))), null);
    assert.equal(context.extractCapture({...socket(), url: socket().url.replace("substrate.office.com", "example.invalid")}), null);
});

test("encrypted personal tokens require a chat socket, not an arbitrary header", () => {
    const {context} = harness();
    const encrypted = Buffer.from(JSON.stringify({alg: "dir", enc: "A256GCM"})).toString("base64url") + "..a.b.c";
    assert.equal(context.extractCapture(socket(encrypted)).accessToken, encrypted);
    assert.equal(context.extractCapture({url: "https://substrate.office.com/m365Copilot/getgptlist",
        requestHeaders: [{name: "Authorization", value: `Bearer ${encrypted}`}]}), null);
});

test("does not capture before linking and does not resend a duplicate", async () => {
    const {context, data, localData, sent} = harness();
    const details = socket();
    await context.acceptCapture(details);
    assert.equal(sent.length, 0);
    data.linked = true;
    localData.linkId = "test-connection";
    await context.acceptCapture(details);
    await context.acceptCapture(details);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].host, "io.github.copilot_proxy.m365");
    assert.equal(sent[0].message.action, "capture");
});

test("page senders cannot operate the native bridge", () => {
    const {listeners, sent} = harness();
    assert.equal(listeners.message({action: "connect"}, {id: "a".repeat(32), url: "https://m365.cloud.microsoft/"}, () => {}), false);
    assert.equal(sent.length, 0);
});

test("status from another connection does not silently connect this browser profile", async () => {
    const {context, data} = harness({linkId: "previous-connection", linked: true, capture: {accessToken: "secret"}});
    await context.refreshStatus();
    assert.equal(data.linked, false);
    assert.equal(data.capture, undefined);
    assert.equal(data.error, "");
    assert.equal((await context.publicSnapshot()).linked, false);
});

test("connect works directly without asking for a pairing code", async () => {
    const {listeners, localData, sent} = harness();
    const result = await new Promise(resolve => {
        listeners.message({action: "connect"}, {
            id: "a".repeat(32), url: `chrome-extension://${"a".repeat(32)}/popup.html`
        }, resolve);
    });
    assert.equal(result.error, "");
    assert.equal(localData.linkId, "test-connection");
    assert.equal(sent[0].message.action, "connect");
    assert.equal("code" in sent[0].message, false);
});

test("offline native errors are Chinese and do not leave the gateway marked connected", async () => {
    const {data, request} = harness();
    data.nativeOffline = true;
    const result = await request("status");
    assert.equal(result.connected, false);
    assert.match(result.error, /无法连接本地授权组件/);
    assert.equal(result.error.includes("host unavailable"), false);
});

test("renew reloads only an existing Copilot chat page and waits for a new token", async () => {
    const {request, tabActions, data, context} = harness({linkId: "test-connection", tabs: [
        {id: 1, url: "https://www.office.com/document/unsaved"},
        {id: 2, url: "https://copilot.cloud.microsoft/home", active: true}
    ]});
    const result = await request("renew");
    assert.equal(result.renewal.state, "waiting");
    assert.deepEqual(tabActions.map(item => [item.action, item.id]), [["update", 2], ["reload", 2]]);
    await request("renew");
    assert.equal(tabActions.length, 2, "do not repeatedly refresh during an outstanding renewal");
    await context.acceptCapture({...socket(), tabId: 2});
    assert.equal(data.renewal.state, "updated");
    assert.equal(data.sourceTabId, 2);
    assert.equal((await context.publicSnapshot()).canCopy, true);
});

test("renew opens M365 when no chat tab exists and reports tab failures", async () => {
    const fresh = harness({linkId: "test-connection"});
    await fresh.request("renew");
    assert.equal(fresh.tabActions[0].action, "create");
    assert.equal(fresh.tabActions[0].options.url, "https://m365.cloud.microsoft/chat/");
    const failure = harness({linkId: "test-connection", reloadFailure: true,
        tabs: [{id: 4, url: "https://m365.cloud.microsoft/chat/"}]});
    const result = await failure.request("renew");
    assert.equal(result.renewal.state, "failed");
    assert.match(result.error, /无法刷新/);
});

test("recapturing the same token after extension reload can be copied but is not a successful renewal", async () => {
    const sameToken = token();
    const originalCapture = Date.now() - 600000;
    const {context, request, data} = harness({linkId: "test-connection", serverToken: sameToken,
        status: {linked: true, linkId: "test-connection", state: "captured", capturedAt: originalCapture,
            expiresAt: Math.floor(Date.now() / 1000) + 3600}});
    await request("renew");
    await context.acceptCapture(socket(sameToken));
    const snapshot = await context.publicSnapshot();
    assert.equal(snapshot.canCopy, true);
    assert.equal(data.capture.capturedAt, originalCapture);
    assert.equal(snapshot.renewal.state, "waiting");
});

test("ordinary status never returns a token; copying is explicit and disallowed on expired authorization", async () => {
    const instance = harness({linkId: "test-connection"});
    await instance.request("status");
    await instance.context.acceptCapture(socket());
    const status = await instance.request("snapshot");
    assert.equal(status.canCopy, true);
    assert.equal("token" in status, false);
    assert.equal("linkId" in status.status, false);
    assert.equal(JSON.stringify(status).includes("signature"), false);
    const copied = await instance.request("copy");
    assert.equal(copied.token, instance.data.capture.accessToken);
    const expired = harness({linkId: "test-connection", linked: true, capture: {accessToken: "test-secret", capturedAt: 1},
        status: {linked: true, linkId: "test-connection", state: "needs-login", capturedAt: 1, expiresAt: 1}});
    const rejected = await expired.request("copy");
    assert.equal("token" in rejected, false);
    assert.match(rejected.error, /没有可复制的有效令牌/);
});

test("opening M365 works while offline and disconnect clears session credentials", async () => {
    const {request, data, localData, tabActions} = harness({linkId: "test-connection"});
    data.nativeOffline = true;
    await request("open");
    assert.equal(tabActions[0].action, "create");
    data.nativeOffline = false;
    await request("disconnect");
    assert.equal(localData.linkId, undefined);
    assert.equal(data.capture, undefined);
    assert.equal(data.linked, false);
});

function popupHarness(response = {connected: false, linked: false}) {
    const elements = {};
    const actions = [];
    const writes = [];
    const document = {
        body: {dataset: {}},
        getElementById(id) {
            return elements[id] ||= {textContent: "", dataset: {}, disabled: false, hidden: false,
                addEventListener(event, callback) { this[event] = callback; }};
        }
    };
    const chrome = {
        runtime: {id: "a".repeat(32), async sendMessage(message) { actions.push(message.action); return {...response}; }},
        storage: {onChanged: {addListener() {}}}
    };
    const context = vm.createContext({document, chrome, Date, confirm: () => true, setInterval() {},
        navigator: {clipboard: {async writeText(value) { writes.push(value); }}}});
    vm.runInContext(readFileSync(join(__dirname, "..", "browser-extension", "popup.js"), "utf8"), context);
    return {context, elements, actions, writes};
}

test("popup maps offline, fresh, expired and opaque tokens into accurate Chinese states", () => {
    const {context} = popupHarness();
    const now = 1000000;
    const fresh = {connected: true, linked: true, canCopy: true,
        status: {state: "captured", accountKind: "enterprise", username: "test@example.invalid", expiresAt: now / 1000 + 3600, capturedAt: now - 120000}};
    const ready = context.popupView(fresh, now);
    assert.equal(ready.heading, "授权已同步");
    assert.equal(ready.account.text, "test@example.invalid");
    assert.equal(ready.token.text, "有效");
    assert.equal(ready.captured, "2 分钟前获取");
    const expired = context.popupView({...fresh, status: {...fresh.status, expiresAt: 1}}, now);
    assert.equal(expired.token.text, "已过期");
    assert.equal(expired.primary.action, "renew");
    assert.equal(expired.canCopy, false);
    const opaque = context.popupView({...fresh, status: {...fresh.status, username: null, accountKind: "personal", expiresAt: null}}, now);
    assert.equal(opaque.account.text, "未提供用户名");
    assert.equal(opaque.token.text, "有效期未知");
    const offline = context.popupView({...fresh, connected: false}, now);
    assert.equal(offline.heading, "本地授权组件未连接");
    assert.equal(offline.gateway.text, "未连接");
    assert.equal(offline.canCopy, false);
});

test("gateway shutdown does not discard captured authorization or disable copying and renewal", async () => {
    const sameToken = token();
    const capturedAt = Date.now() - 600000;
    const {request} = harness({linkId: "test-connection", serviceOffline: true, serverToken: sameToken,
        status: {linked: true, linkId: "test-connection", state: "captured", accountKind: "enterprise",
            capturedAt, expiresAt: Math.floor(Date.now() / 1000) + 3600}});
    const restored = await request("status");
    assert.equal(restored.connected, false);
    assert.equal(restored.hostAvailable, true);
    assert.equal(restored.status.state, "captured");
    const {context} = popupHarness();
    const view = context.popupView(restored);
    assert.equal(view.heading, "授权已保存");
    assert.equal(view.gateway.text, "未运行");
    assert.equal(view.token.text, "有效");
    const renew = await request("renew");
    assert.equal(renew.renewal.state, "waiting");
    assert.equal(renew.error, "");
});

test("reload recovers a page token even when the chat gateway is stopped", async () => {
    const {request, sent, context} = harness({linkId: "test-connection", serviceOffline: true,
        tabs: [{id: 5, url: "https://copilot.cloud.microsoft/home"}],
        pageFrames: [{result: {token: token(), origin: "https://copilot.cloud.microsoft"}}]});
    const result = await request("status");
    assert.equal(result.status.state, "captured");
    assert.equal(result.connected, false);
    assert.equal(result.canCopy, true);
    assert.equal(sent.filter(item => item.message.action === "capture").length, 1);
    assert.equal((await context.publicSnapshot()).status.capturedAt, result.status.capturedAt);
});

test("existing expired credentials show expired, not missing, while offline", () => {
    const {context} = popupHarness();
    const expired = context.popupView({connected: false, hostAvailable: true, linked: true,
        status: {state: "needs-login", expiresAt: 1, capturedAt: 1}});
    assert.equal(expired.token.text, "已过期");
    assert.equal(expired.primary.action, "renew");
    assert.equal(expired.gateway.text, "未运行");
    const unknown = context.popupView({connected: false, hostAvailable: false});
    assert.equal(unknown.token.text, "状态未知");
});

function scanPage(values, expectedAccount = null) {
    const {context} = harness();
    const readKeys = [];
    const storage = {
        length: Object.keys(values).length,
        key(index) { return Object.keys(values)[index]; },
        getItem(key) { readKeys.push(key); return values[key] || null; }
    };
    const scope = vm.createContext({window: {localStorage: storage, sessionStorage: storage},
        location: {origin: "https://copilot.cloud.microsoft"}, atob, Date});
    const result = vm.runInContext(`(${context.readPageAccessToken.toString()})(${JSON.stringify(expectedAccount)})`, scope);
    return {result, readKeys};
}

test("page recovery reads only indexed chat access tokens and respects the selected account", () => {
    const chatKey = "account-login.microsoftonline.com-accesstoken-client-tenant-sydney";
    const graphKey = "account-login.microsoftonline.com-accesstoken-client-tenant-graph";
    const otherKey = "other-login.microsoftonline.com-accesstoken-client-tenant-sydney";
    const selected = token();
    const values = {
        "msal.token.keys.client": JSON.stringify({accessToken: [chatKey, graphKey, otherKey], refreshToken: ["refresh-key"]}),
        [chatKey]: JSON.stringify({credentialType: "AccessToken", secret: selected}),
        [graphKey]: JSON.stringify({credentialType: "AccessToken", secret: token({aud: "https://graph.microsoft.com"})}),
        [otherKey]: JSON.stringify({credentialType: "AccessToken", secret: token({oid: "other-user", exp: Math.floor(Date.now() / 1000) + 7200})}),
        "refresh-key": "do-not-read",
        "m365copilot.capture.v1": "another-extension-do-not-read"
    };
    const {result, readKeys} = scanPage(values, "user-1@tenant-1");
    assert.equal(result.token, selected);
    assert.equal(readKeys.includes("refresh-key"), false);
    assert.equal(readKeys.includes("m365copilot.capture.v1"), false);
    assert.equal(scanPage(values).result.reason, "multiple-accounts");
});

test("popup reports actual renewal waiting and timeout rather than claiming automatic success", () => {
    const {context} = popupHarness();
    const snapshot = {connected: true, linked: true, status: {state: "needs-login", capturedAt: 1},
        renewal: {state: "waiting", startedAt: 1000000}};
    const waiting = context.popupView(snapshot, 1000001);
    assert.equal(waiting.heading, "正在获取新授权");
    assert.equal(waiting.primary.disabled, true);
    const timeout = context.popupView(snapshot, 1120000);
    assert.equal(timeout.renewing, false);
    assert.equal(timeout.renewal.text, "需要手动登录");
    assert.equal(timeout.primary.disabled, false);
});

test("popup renders labels, hides technical IDs and copies a token only after an explicit action", async () => {
    const popup = popupHarness({connected: true, linked: true, canCopy: true, token: "clipboard-test-secret",
        status: {state: "captured", accountKind: "enterprise", username: "long.username@example.invalid",
            capturedAt: Date.now(), expiresAt: Date.now() / 1000 + 3600}});
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(popup.elements["account-text"].textContent, "long.username@example.invalid");
    assert.equal(popup.elements["account-status"].title, "long.username@example.invalid");
    assert.equal(popup.elements["gateway-text"].textContent, "已连接");
    assert.equal(popup.elements.copy.disabled, false);
    assert.equal(popup.writes.length, 0);
    await popup.context.send("copy");
    assert.deepEqual(popup.writes, ["clipboard-test-secret"]);
    assert.equal(popup.elements.notice.textContent, "令牌已复制，请勿分享给他人。");
    assert.equal(Object.values(popup.elements).some(element => element.textContent.includes("clipboard-test-secret")), false);
});

function renewalHarness(overrides = {}) {
    const now = 2000000000000;
    return harness({now, linkId: "test-connection", serviceOffline: true,
        status: {linked: true, linkId: "test-connection", state: "captured", capturedAt: now - 3600000,
            expiresAt: now / 1000 + 12 * 60},
        tabs: [{id: 10, active: true, url: "https://copilot.cloud.microsoft/home"}], ...overrides});
}

test("automatic renewal begins exactly twelve minutes before expiry, not earlier", async () => {
    const instance = renewalHarness();
    const {context} = instance;
    const expiresAt = 2000000000000 / 1000 + 12 * 60;
    assert.equal(context.automaticRenewalDecision({capturedAt: 1, state: "captured", expiresAt}, null, 1999999999999), "none");
    assert.equal(context.automaticRenewalDecision({capturedAt: 1, state: "captured", expiresAt}, null, 2000000000000), "renew");
    await instance.request("snapshot");
    assert.equal(instance.localData.autoRenewal.attempts, 1);
    assert.equal(instance.tabActions.length, 1);
    assert.equal(instance.tabActions[0].action, "create");
    assert.equal(instance.tabActions[0].options.url, "https://m365.cloud.microsoft/chat/");
    assert.equal(instance.tabActions[0].options.active, false);
});

test("automatic renewal persists backoff, caps attempts and never reloads the user's active page", async () => {
    const instance = renewalHarness();
    const {data, localData, tabActions, tick} = instance;
    await instance.request("snapshot");
    const start = data.now;
    data.now = start + 119999;
    await tick();
    assert.equal(tabActions.length, 1);
    data.now = start + 120000;
    await tick();
    assert.equal(localData.autoRenewal.attempts, 2);
    assert.equal(tabActions[1].action, "reload");
    assert.notEqual(tabActions[1].id, 10);
    data.now = start + 240000;
    assert.equal((await tick()).renewal.state, "retrying");
    data.now = start + 359999;
    await tick();
    assert.equal(localData.autoRenewal.attempts, 2);
    data.now = start + 360000;
    await tick();
    assert.equal(localData.autoRenewal.attempts, 3);
    data.now = start + 480000;
    assert.equal((await tick()).renewal.state, "needs-user");
    data.now += 3600000;
    await tick();
    assert.equal(tabActions.length, 3);
    assert.equal(tabActions.some(item => item.action === "update"), false);
    const restarted = renewalHarness({now: data.now, autoRenewal: {...localData.autoRenewal}});
    assert.equal((await restarted.request("snapshot")).renewal.state, "needs-user");
    assert.equal(restarted.tabActions.length, 0);
});

test("new token completes automatic renewal and closes only its owned background tab", async () => {
    const instance = renewalHarness();
    await instance.request("snapshot");
    const ownTab = instance.data.renewalTabId;
    await instance.context.acceptCapture({...socket(token({exp: instance.data.now / 1000 + 3600})), tabId: ownTab});
    assert.equal(instance.localData.autoRenewal, undefined);
    assert.equal(instance.data.renewal.state, "updated");
    assert.equal(instance.tabActions.at(-1).action, "remove");
    assert.equal(instance.tabActions.at(-1).id, ownTab);
    assert.equal(instance.tabs[0].id, 10);
    instance.data.now += 60000;
    await instance.tick();
    assert.equal(instance.tabActions.length, 2);
});

test("the same token is not renewal success and a user-adopted page is not closed", async () => {
    const oldToken = token({exp: 2000000000000 / 1000 + 12 * 60});
    const instance = renewalHarness({serverToken: oldToken});
    await instance.request("snapshot");
    const ownTab = instance.data.renewalTabId;
    await instance.context.acceptCapture({...socket(oldToken), tabId: ownTab});
    assert.equal(instance.localData.autoRenewal.attempts, 1);
    assert.equal(instance.data.renewal.state, "waiting");
    instance.listeners.activated({tabId: ownTab});
    await instance.request("snapshot");
    await instance.context.acceptCapture(socket(token({exp: instance.data.now / 1000 + 3600})));
    assert.equal(instance.data.renewal.state, "updated");
    assert.equal(instance.tabActions.some(item => item.action === "remove"), false);
});

test("disconnect cancels automatic renewal and missing or unknown expiry never guesses a timer", async () => {
    const instance = renewalHarness();
    await instance.request("snapshot");
    await instance.request("disconnect");
    assert.equal(instance.localData.autoRenewal, undefined);
    assert.equal(instance.tabs.length, 1);
    const actions = instance.tabActions.length;
    instance.data.now += 3600000;
    await instance.tick();
    assert.equal(instance.tabActions.length, actions);
    const {context} = harness();
    assert.equal(context.automaticRenewalDecision({state: "waiting-for-capture"}, null), "none");
    assert.equal(context.automaticRenewalDecision({capturedAt: 1, state: "captured", expiresAt: null}, null), "none");
});

test("failed background tab creation consumes a bounded attempt and reports the failure", async () => {
    const instance = renewalHarness({createFailure: true});
    const result = await instance.request("snapshot");
    assert.match(result.error, /后台续期未能启动/);
    assert.equal(instance.localData.autoRenewal.attempts, 1);
    instance.data.now += 60000;
    await instance.tick();
    assert.equal(instance.localData.autoRenewal.attempts, 1);
});

test("popup exposes proactive renewal and retry without account type labels", () => {
    const {context} = popupHarness();
    const snapshot = {connected: false, hostAvailable: true, linked: true, autoRenewalEnabled: true,
        status: {username: "name@example.invalid", accountKind: "enterprise", state: "captured", expiresAt: 2000, capturedAt: 1}};
    const fresh = context.popupView(snapshot, 1000000);
    assert.equal(fresh.account.text, "name@example.invalid");
    assert.equal(fresh.renewal.text, "自动续期已启用");
    const waiting = context.popupView({...snapshot, renewal: {automatic: true, state: "waiting", startedAt: 1000000}}, 1000001);
    assert.equal(waiting.heading, "正在自动续期");
    assert.match(waiting.subtitle, /不打断/);
    const retry = context.popupView({...snapshot, renewal: {automatic: true, state: "retrying", nextAttemptAt: 1120000}}, 1060000);
    assert.equal(retry.renewal.text, "1 分钟后重试");
});

test("the desktop sync switch pauses capture and renewal without clearing the login", async () => {
    const instance = renewalHarness();
    await instance.request("snapshot");
    instance.data.syncEnabled = false;
    const paused = await instance.tick();
    assert.equal(paused.paused, true);
    assert.equal(paused.linked, true);
    assert.equal(paused.autoRenewalEnabled, false);
    assert.equal(instance.localData.linkId, "test-connection");
    assert.equal(instance.localData.autoRenewal, undefined);
    assert.equal(instance.tabs.length, 1);
    const calls = instance.sent.length;
    await instance.context.acceptCapture(socket(token({exp: instance.data.now / 1000 + 3600})));
    assert.equal(instance.sent.length, calls);
    const {context} = popupHarness();
    const view = context.popupView(paused, instance.data.now);
    assert.equal(view.heading, "授权同步已暂停");
    assert.equal(view.primary.disabled, true);
    instance.data.syncEnabled = true;
    const resumed = await instance.tick();
    assert.equal(resumed.autoRenewalEnabled, true);
    assert.equal(instance.localData.autoRenewal.attempts, 1);
});

test("extension port settings are validated and never silently ignore a mismatched fixed port", async () => {
    const instance = harness({activePort: 43210});
    const failed = await instance.request("set-port", {port: 43211});
    assert.equal(instance.localData.port, 43211);
    assert.match(failed.error, /管理端口不一致/);
    const fixed = await instance.request("set-port", {port: 43210});
    assert.equal(fixed.error, "");
    assert.equal(instance.sent.at(-1).message.port, 43210);
    const auto = await instance.request("set-port", {port: 0});
    assert.equal(auto.error, "");
    assert.equal(instance.sent.at(-1).message.port, 0);
    const invalid = await instance.request("set-port", {port: 65536});
    assert.match(invalid.error, /管理端口须为/);
    assert.equal(instance.localData.port, 0);
});
