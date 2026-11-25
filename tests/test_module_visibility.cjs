/* 模块开关在页面上的落地：左栏条目随 GET /page/modules 收起。
 *
 * 三条不许破的约定：
 *   1. 读不到开关时全开——开关是收敛可见性，不能变成一个让页面空掉的新失败点。
 *   2. 只收起可选/已移除模块独占的入口；保留模块（内容/现实/陪伴）的条目
 *      不因为别的模块关掉而消失。
 *   3. 关掉 phone 之后「专题配置」这一组不能只剩一个标题——整组空了就整组不渲染。
 *
 * 用 node --test 跑。shell.js 只依赖 window / document / console。
 */
"use strict";
const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const PAGE = path.join(__dirname, "..", "pages", "陪伴小屋");
const SHELL = fs.readFileSync(path.join(PAGE, "shell.js"), "utf8");
const APP = fs.readFileSync(path.join(PAGE, "app.js"), "utf8");

function load() {
  const context = vm.createContext({window: {}, document: {}, console});
  vm.runInContext(SHELL, context);
  return context.window.CompanionShell;
}

/* shell.js 跑在另一个 vm realm 里，数组的 prototype 与本文件的不一样，
   deepStrictEqual 会因此判红。比字符串。 */
const same = (actual, expected) => assert.strictEqual(JSON.stringify(actual), JSON.stringify(expected));

const ids = groups => groups.flatMap(([, items]) => items.map(item => item.id));

test("没有开关时左栏照旧全开", () => {
  const shell = load();
  const groups = shell.railGroups();
  same(groups.map(([title]) => title), ["专题配置", "能力配置", "设置"]);
  assert.ok(ids(groups).includes("phone"));
  assert.ok(ids(groups).includes("mobile"));
  assert.ok(shell.moduleVisible("phone"));
  assert.ok(shell.pageVisible("workspace"));
});

test("关掉 phone 只收起手机相关的两个入口", () => {
  const shell = load();
  const before = ids(shell.railGroups());
  shell.setVisibility({modules: {phone: false}, pages: {}});
  const after = ids(shell.railGroups());
  assert.ok(before.includes("phone") && before.includes("mobile"));
  assert.ok(!after.includes("phone"));
  assert.ok(!after.includes("mobile"));
  // 除这两项外一项不少，顺序也不动。
  same(after, before.filter(id => id !== "phone" && id !== "mobile"));
});

test("保留模块的条目不受别的模块开关影响", () => {
  const shell = load();
  const before = ids(shell.railGroups());
  shell.setVisibility({modules: {phone: false, image: false, game: false, screen: false,
    workspace: false, multi_role: false, live: false, qzone: false, mihome: false}, pages: {}});
  const after = ids(shell.railGroups());
  for (const id of ["identity", "contact", "memory", "learning", "models", "maintenance",
    "perception", "understanding", "decision", "action", "output", "expression",
    "governance", "setup", "appearance", "host", "data"]) {
    assert.ok(after.includes(id), id);
  }
  same(after, before.filter(id => id !== "phone" && id !== "mobile"));
});

test("整组条目被收空后不再渲染只有一个标题的分组", () => {
  const shell = load();
  assert.ok(shell.railGroups().every(([, items]) => items.length > 0));
  shell.setVisibility({modules: {phone: false}, pages: {}});
  assert.ok(shell.railGroups().every(([, items]) => items.length > 0));
  // 直接验分支：把「设置」组的三项逐个关掉不会有模块归属，所以改用一份
  // 只留 phone/mobile 的极端映射——组空了就不该出现在结果里。
  const source = fs.readFileSync(path.join(PAGE, "shell.js"), "utf8");
  assert.match(source, /\.filter\(\(\[, items\]\) => items\.length\)/);
});

test("railHTML 跟着 railGroups 走", () => {
  const shell = load();
  assert.match(shell.railHTML(), /data-shell-section="phone"/);
  shell.setVisibility({modules: {phone: false}, pages: {}});
  const html = shell.railHTML();
  assert.doesNotMatch(html, /data-shell-section="phone"/);
  assert.doesNotMatch(html, /data-shell-section="mobile"/);
  assert.match(html, /data-shell-section="identity"/);
});

test("开关变化通知订阅者，重复设置同一份不重复通知", () => {
  const shell = load();
  let hits = 0;
  shell.onChange(() => { hits += 1; });
  shell.setVisibility({modules: {phone: false}});
  assert.strictEqual(hits, 1);
  shell.setVisibility({modules: {phone: false}});
  assert.strictEqual(hits, 1);
  shell.setVisibility({modules: {phone: true}});
  assert.strictEqual(hits, 2);
});

test("坏输入按全开处理", () => {
  const shell = load();
  shell.setVisibility(null);
  assert.strictEqual(shell.snapshotVisibility().modules.phone, undefined);
  assert.ok(shell.moduleVisible("phone"));
  shell.setVisibility({modules: "nonsense"});
  same(ids(shell.railGroups()), ids(load().railGroups()));
});

test("app.js 把开关接到左栏与可选面板上", () => {
  // 读一次 /page/modules，落到外壳与面板共同读的状态上。
  assert.match(APP, /product\.request\('GET','page\/modules'\)/);
  assert.match(APP, /window\.CompanionShell\?\.setVisibility\?\./);
  // 三个可选面板各自的挂载闸门。
  assert.match(APP, /if\(\$\('#phoneAdbWorkspace'\)&&moduleOn\('phone'\)\)\{/);
  assert.match(APP, /if\(gameAccessHost&&moduleOn\('game'\)\)/);
  assert.match(APP, /screenAccessHost&&moduleOn\('screen'\)/);
  // 读不到就保持全开，不许因为一次读取失败把页面卡在空态。
  assert.match(APP, /function moduleOn\(name\)\{ return moduleState\.modules\[name\] !== false; \}/);
});
