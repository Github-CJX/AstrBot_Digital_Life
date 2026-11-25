"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const rootPath = path.resolve(__dirname, "..");
const pageRoot = path.join(rootPath, "pages", "陪伴小屋");
const schema = JSON.parse(fs.readFileSync(path.join(rootPath, "digital_life_host/native_extensions/reality/settings-schema.json"), "utf8"));
const settings = JSON.parse(fs.readFileSync(path.join(rootPath, "digital_life_host/native_extensions/reality/settings-defaults.json"), "utf8"));
const sources = {
  panel: fs.readFileSync(path.join(pageRoot, "native-extension-panel.js"), "utf8"),
  management: fs.readFileSync(path.join(pageRoot, "native-extension-management.js"), "utf8"),
};

function fakeRoot() {
  const root = {
    isConnected: true,
    _html: "",
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(root, "innerHTML", {
    get() { return root._html; },
    set(value) { root._html = value; },
  });
  return root;
}

function context() {
  const window = {};
  const sandbox = {
    window, document: {}, console, Intl, Date, Object, Set, Map, JSON, structuredClone,
    setTimeout, clearTimeout, navigator: {clipboard: {writeText: async () => {}}},
    crypto: {randomUUID: () => "synthetic-operation"},
  };
  vm.createContext(sandbox);
  vm.runInContext(sources.management, sandbox);
  vm.runInContext(sources.panel, sandbox);
  return {window, sandbox};
}

function extensionRecord(management = {}) {
  return {
    settings: structuredClone(settings), schema, revision: "synthetic-revision",
    runtime: {state: "running"},
    management: {
      configuration: {mobile: {enabled: true, running: true, bound_port: 6322}},
      camera: {devices: []}, audio_output: {devices: []}, users: [],
      ...management,
    },
  };
}

test("mobile connection partitions settings into collapsible cards without dropping fields", async () => {
  const {window} = context(), root = fakeRoot(), record = extensionRecord();
  /* 这几项是 schema 里其它字段的 condition 开关：用例要核对「分组齐全、字段
     一个都不丢」，得先让所有条件成立。关闭时隐藏字段的行为由
     tests/test_native_extension_conditions.cjs 覆盖。 */
  Object.assign(record.settings.mobile,
    {enabled: true, amap_reverse_geocode_enabled: true, activity_enabled: true});
  window.CompanionProduct = {
    request: async () => ({extensions: {reality: record}}),
    extension: () => ({owner: "digital_life_core", assembled: true, desired_state: "active", installed_revision: 1}),
    errorLabel: () => "", target: () => ({scope_kind: "private", participant_id: "42"}),
    refreshIntegrations: async () => {},
  };
  const panel = window.CompanionNativeExtensionPanel.mount(root, {extensionId: "reality", onlyGroup: "mobile"});
  await new Promise(resolve => setTimeout(resolve, 0));
  const html = root.innerHTML;
  assert.equal((html.match(/data-mobile-config-section=/g) || []).length, 5);
  assert.match(html, /data-mobile-config-section="gateway" open/);
  for (const [key, field] of Object.entries(schema.mobile.items)) {
    if (["telemetry_enabled", "telemetry_ttl_seconds"].includes(key)) continue;
    assert.ok(html.includes(field.description), "missing mobile field: " + field.description);
  }
  panel.stop();
});

test("mobile connection page displays a paired device from the management snapshot", async () => {
  const {window} = context(), root = fakeRoot(), record = extensionRecord({
    configuration: {mobile: {enabled: true, running: true, bound_port: 6322,
      allowed_user_id: "42", core_user_id: "42", pairing_token_configured: true}},
    mobile_observations: [{user_id: "42", connections: [{device_name: "测试手机",
      last_seen_at: 1_798_650_000, online: true}], device: {}, location: {}, activity: {}, telemetry: {}}],
  });
  window.CompanionProduct = {
    request: async () => ({extensions: {reality: record}}),
    extension: () => ({owner: "digital_life_core", assembled: true, desired_state: "active", installed_revision: 1}),
    errorLabel: () => "", target: () => ({scope_kind: "private", participant_id: "42"}),
    refreshIntegrations: async () => {},
  };
  const panel = window.CompanionNativeExtensionPanel.mount(root, {extensionId: "reality", onlyGroup: "mobile"});
  await new Promise(resolve => setTimeout(resolve, 0));
  assert.match(root.innerHTML, /已配对终端/);
  assert.match(root.innerHTML, /测试手机 · 在线/);
  assert.match(root.innerHTML, /data-mobile-refresh/);
  panel.stop();
});

test("mobile health reachability is not presented as an active pairing", async () => {
  for (const activeSessions of [0, 1]) {
    const {window} = context(), root = fakeRoot(), record = extensionRecord({
      configuration: {mobile: {enabled: true, running: true, bound_port: 6322,
        active_sessions: activeSessions, allowed_user_id: "42", core_user_id: "42",
        pairing_token_configured: true}},
      mobile_observations: [{user_id: "42", connections: [], device: {}, location: {}, activity: {}, telemetry: {}}],
    });
    window.CompanionProduct = {
      request: async () => ({extensions: {reality: record}}),
      extension: () => ({owner: "digital_life_core", assembled: true, desired_state: "active", installed_revision: 1}),
      errorLabel: () => "", target: () => ({scope_kind: "private", participant_id: "42"}),
      refreshIntegrations: async () => {},
    };
    const panel = window.CompanionNativeExtensionPanel.mount(root, {extensionId: "reality", onlyGroup: "mobile"});
    await new Promise(resolve => setTimeout(resolve, 0));
    assert.match(root.innerHTML, activeSessions ? /会话未匹配当前对象/ : /等待手机配对/);
    assert.match(root.innerHTML, activeSessions ? /核对手机与插件使用的私聊账号/ : /健康检查成功只代表网关在线/);
    panel.stop();
  }
});
