"use strict";
const assert = require("node:assert/strict"), fs = require("node:fs"), vm = require("node:vm");
const source = fs.readFileSync("vghks_bot/static/bot.js", "utf8");
const deferred = () => {let resolve;const promise = new Promise(done => {resolve = done;});return {promise, resolve};};
const tick = async () => {for (let index = 0; index < 20; index++) await Promise.resolve();};

function surface(native = false) {
  const nodes = new Map(), events = new Map();let context;
  class Element {
    constructor(tag = "div") {this.tagName = tag.toUpperCase();this.children = [];this.dataset = {};this.style = {};this.attributes = {};this.events = new Map();this.hidden = false;this.disabled = false;this.value = "";this.textContent = "";this.className = "";
      this.classList = {add: name => {this.className += " " + name;}};}
    append(...children) {this.children.push(...children);}
    replaceChildren(...children) {this.children = children;}
    all() {return this.children.flatMap(child => [child, ...child.all()]);}
    contains(element) {return element === this || this.all().includes(element);}
    setAttribute(name, value) {this.attributes[name] = String(value);}
    getAttribute(name) {return this.attributes[name] ?? null;}
    removeAttribute(name) {delete this.attributes[name];}
    addEventListener(name, callback) {this.events.set(name, [...(this.events.get(name) || []), callback]);}
    emit(name, event = {}) {return Promise.all((this.events.get(name) || []).map(callback => callback({target: this, preventDefault() {}, ...event})));}
    querySelector(selector) {return this.all().find(element => selector.startsWith(".") ? element.className.split(" ").includes(selector.slice(1)) : element.getAttribute("aria-pressed") === "true");}
    focus() {context.document.activeElement = this;}
    close() {this.open = false;return this.emit("close");}
    getBoundingClientRect() {return this === nodes.get("sidebarAccount") ? {left: 12, bottom: 140} : {width: 320, height: 420};}
  }
  const get = id => {if (!nodes.has(id)) nodes.set(id, new Element());return nodes.get(id);};
  const document = {activeElement: null, getElementById: get, createElement: tag => new Element(tag), querySelectorAll: () => [],
    addEventListener(name, callback) {events.set(name, callback);}};
  context = {document, window: {innerWidth: 390, innerHeight: 600, addEventListener() {}}, console};
  get("accountPanel").append(get("accountOptions"));
  get("loginFields").hidden = true;
  get("connectionStatus").textContent = "已登入";
  if (native) {
    get("accountPanel").showPopover = () => {get("accountPanel").hidden = false;};
    get("accountPanel").hidePopover = () => {get("accountPanel").hidden = true;get("accountPanel").emit("toggle", {newState: "closed"});};
  }
  vm.createContext(context);vm.runInContext(fs.readFileSync("vghks_bot/static/account-switcher.js", "utf8"), context);
  return {context, get, events, ui: context.window.AccountSwitcher};
}
const profiles = [
  {id: "A", label: "高榮上午門診", username: "DOCTOR_A", campus: "高榮", online: true, remembered: true},
  {id: "B", label: "聯醫手術與門診工作區", username: "DOCTOR_B", campus: "聯醫", online: true, remembered: true},
  {id: "C", label: "高榮同院區的另一個長名稱合成帳號", username: "LONG_ACCOUNT_03", campus: "高榮", remembered: true},
  {id: "D", label: "<img src=x onerror=alert(1)>", username: "PASSWORD_REQUIRED", campus: "聯醫", remembered: false},
];
async function presentationTests(native) {
  const {get, events, context, ui} = surface(native), clicks = [];
  ui.init({onOpen() {}, onSwitch: id => clicks.push(["switch", id]), onLogin: id => clicks.push(["login", id]), onOffline() {}, onOptions() {}, onNew() {}});
  ui.render({accounts: profiles, read_only: false}, "A");
  assert.equal(get("accountOptions").children.length, 4);
  const firstOption = get("accountOptions").children[0];
  ui.render({accounts: profiles.map(profile => ({...profile})), read_only: false}, "A");
  assert.equal(get("accountOptions").children[0], firstOption, "refreshing account status must preserve the button under the pointer");
  assert.equal(get("currentAccountCampus").textContent, "高榮");
  assert.equal(get("currentAccountUsername").textContent, "DOCTOR_A");
  assert.equal(get("loginFields").disabled, true, "collapsed required fields must not block saved-card actions");
  const unsafe = get("accountOptions").children[3].querySelector(".account-nickname");
  assert.equal(unsafe.textContent, profiles[3].label, "account names must remain literal text");
  assert.equal(get("connectionStatus").hidden, true, "a normal connection must not add a status line");
  assert.equal(firstOption.querySelector(".account-option-status").hidden, true);
  assert.equal(firstOption.querySelector(".account-option-name").textContent, "DOCTOR_A");
  await get("sidebarAccount").emit("click");
  assert.equal(get("sidebarAccount").getAttribute("aria-expanded"), "true");
  assert.equal(context.document.activeElement, get("accountOptions").children[0]);
  const left = parseFloat(get("accountPanel").style.left), top = parseFloat(get("accountPanel").style.top);
  assert.ok(left >= 8 && left + 320 <= 382 && top >= 8 && top + 420 <= 592, "the panel must stay inside the narrow viewport");
  await events.get("keydown")({key: "Escape", preventDefault() {}});
  assert.equal(get("sidebarAccount").getAttribute("aria-expanded"), "false");
  assert.equal(context.document.activeElement, get("sidebarAccount"));
  await get("sidebarAccount").emit("click");
  await get("accountOptions").children[0].emit("click");
  assert.deepEqual(clicks, [], "selecting the current account must not log in again");
  await get("sidebarAccount").emit("click");
  await events.get("click")({target: get("content")});
  assert.equal(context.document.activeElement, get("sidebarAccount"), "outside dismissal must not leave focus in the hidden panel");
  ui.busy("B", "正在切換帳號…");
  assert.ok(get("accountOptions").children.every(button => button.disabled));
  assert.equal(get("content").inert, true);
  await get("accountOptions").children[1].emit("click");assert.deepEqual(clicks, []);
  ui.busy();
  await get("accountOptions").children[1].emit("click");assert.deepEqual(clicks, [["switch", "B"]]);
  ui.render({accounts: profiles, read_only: true}, "A");
  assert.equal(get("connectionStatus").textContent, "唯讀");
  assert.equal(get("connectionStatus").hidden, false);
  assert.equal(get("addWorkspaceAccount").disabled, true);
  assert.equal(get("reconnect").disabled, true);
  assert.equal(get("logout").disabled, true);
  assert.equal(get("accountOptions").children[1].disabled, false, "read-only accounts still need offline navigation");
}
async function compactIdentityTests() {
  const {get, ui} = surface();ui.init({});
  const accounts = [
    {...profiles[0], label: ""},
    {...profiles[1], label: "  DOCTOR_B  "},
    {...profiles[2], label: "long_account_03"},
  ];
  ui.render({accounts, read_only: false}, "A");
  for (const button of get("accountOptions").children) {
    assert.equal(button.querySelector(".account-nickname").hidden, true, "empty and duplicate nicknames must not occupy a second line");
    const username = accounts.find(item => item.id === button.dataset.accountId).username;
    assert.equal(button.getAttribute("aria-label").split(username).length - 1, 1, "assistive labels must not repeat the account ID");
  }
  for (const card of get("savedLoginAccounts").children) assert.equal(card.children[0].querySelector(".account-nickname").hidden, true);
  const current = get("accountOptions").children[0];
  get("connectionStatus").textContent = "已登入 · 密碼剩 3 日";ui.syncConnection();
  assert.equal(get("connectionStatus").textContent, "密碼剩 3 日");assert.equal(get("connectionStatus").hidden, false);
  assert.equal(current.querySelector(".account-option-status").textContent, "密碼剩 3 日");
  get("connectionStatus").textContent = "離線檢閱";ui.syncConnection();
  assert.equal(current.querySelector(".account-option-status").hidden, false);
  get("connectionStatus").textContent = "已登入";ui.syncConnection();
  assert.equal(get("connectionStatus").hidden, true);assert.equal(current.querySelector(".account-option-status").hidden, true);
}

function workflow() {
  const {context: c, get, ui} = surface(), calls = [], notices = [];
  Object.assign(c, {root: {context: "database-one", accounts: profiles.map(profile => ({...profile})), read_only: false}, account: "A", page: "library", epoch: 1,
    accountTransition: false, restoringAccountView: false, accountViews: new Map(), reconnectAttempts: new Map(), draftTimer: null,
    DOMException, clearTimeout, draftValue: () => ({selected: ["A-patient"]}), applyReadonly() {}, renderAccounts() {ui.render(c.root, c.account);},
    $: selector => get(selector.slice(1)), $$: () => ["patients", "library", "surgery", "sheetManagement"].map(page => ({dataset: {page}})),
    say: (...values) => notices.push(values),
    api: async path => {calls.push(path + ":" + c.account);},
    ra: async path => {calls.push(path);return path === "/bootstrap" ? c.root : {online: true, status: "ready"};},
    loadAccount: async key => {calls.push("load:" + key);c.account = key;c.epoch++;c.page = "patients";},
    navigate: async page => {calls.push("page:" + page);c.page = page;},
    showPage: page => {calls.push("fallback:" + page);c.page = page;},
    openReconnect: (...args) => calls.push(["password", ...args]),
    showEntry: () => {calls.push("entry");c.account = "";},
    loginFields: (...args) => calls.push(["fields", ...args]), login: (...args) => calls.push(["login", ...args]),
  });
  c.window.ReviewNotesUI = {flushAll: async () => {calls.push("notes");return true;}};
  c.window.ToolWorkspace = {currentKind: () => "retina", navigate: async tool => {calls.push("tool:" + tool);c.page = "tool";}};
  ui.init({});ui.render(c.root, c.account);
  const start = source.indexOf("function rememberAccountView(){"), end = source.indexOf("async function loadAccount(key){", start);
  vm.runInContext(source.slice(start, end), c);
  return {c, get, calls, notices};
}
async function lifecycleTests() {
  const {c, calls, notices} = workflow();
  c.accountViews.set(JSON.stringify([c.root.context, "B"]), {page: "sheetManagement"});
  await c.activateAccount("B");
  assert.equal(c.account, "B");assert.equal(c.page, "sheetManagement");
  assert.deepEqual(calls, ["notes", "/draft/save:A", "/accounts/activate", "/bootstrap", "load:B", "page:sheetManagement"]);
  assert.equal(c.accountViews.get(JSON.stringify([c.root.context, "A"])).page, "library");
  await c.activateAccount("A");assert.equal(c.page, "library");
  c.page = "module";c.rememberAccountView();await c.activateAccount("B");await c.activateAccount("A");
  assert.ok(calls.includes("tool:retina"), "embedded tools must restore their own type, not a generic module page");
  c.root.context = "another-database";await c.enter("B");assert.equal(c.page, "patients", "navigation must not leak across databases");
  c.accountViews.set(JSON.stringify([c.root.context, "A"]), {page: "surgery"});
  c.navigate = async () => {throw new Error("synthetic route failure");};
  await c.enter("A");assert.equal(c.page, "patients");assert.ok(notices.some(item => item[1] === true));
  const fresh = workflow();await fresh.c.enter("B");assert.equal(fresh.c.page, "patients", "a fresh page must forget navigation history");
}
async function failureTests() {
  for (const failure of ["notes", "draft", "activation", "password"]) {
    const {c, calls} = workflow();
    if (failure === "notes") c.window.ReviewNotesUI.flushAll = async () => false;
    if (failure === "draft") c.api = async () => {throw new Error("synthetic disk failure");};
    if (failure === "activation") c.ra = async () => ({status: "unavailable", message: "synthetic network failure"});
    if (failure === "password") c.ra = async () => ({status: "needs_password"});
    if (failure === "password") await c.activateAccount("D");
    else await assert.rejects(c.activateAccount("B"));
    assert.equal(c.account, "A");assert.equal(c.page, "library");assert.equal(c.accountTransition, false);
    if (["notes", "draft"].includes(failure)) assert.ok(!calls.includes("/accounts/activate"));
    if (failure === "password") assert.ok(calls.some(item => Array.isArray(item) && item[0] === "password"));
  }
  const {c, calls} = workflow(), gate = deferred();
  c.window.ReviewNotesUI.flushAll = () => gate.promise;
  const first = c.activateAccount("B"), duplicate = c.activateAccount("C");
  await duplicate;assert.equal(calls.length, 0);gate.resolve(true);await first;
  assert.equal(calls.filter(value => value === "/accounts/activate").length, 1);
  assert.equal(c.account, "B");
  const stale = workflow(), note = deferred();
  stale.c.window.ReviewNotesUI.flushAll = () => note.promise;
  const pending = stale.c.activateAccount("B");stale.c.epoch++;stale.c.account = "C";note.resolve(true);
  await assert.rejects(pending, {name: "AbortError"});assert.equal(stale.calls.length, 0, "an old note save must never save the new account's draft");
}
async function savedLoginTests() {
  const {c, get, calls} = workflow();c.account = "";
  c.selectSavedLogin("B");assert.ok(calls.some(value => Array.isArray(value) && value[0] === "login" && value[1] === "B" && value[2] === true));
  calls.length = 0;c.selectSavedLogin("D");assert.ok(!calls.some(value => Array.isArray(value) && value[0] === "login"));
  assert.equal(c.document.activeElement, get("loginPassword"));
  c.root.read_only = true;await c.selectSavedLogin("B");assert.ok(calls.includes("/accounts/offline"));
  assert.ok(!calls.includes("/accounts/activate"));
}
async function cancelledReconnectTests() {
  const {c, get, calls} = workflow(), gate = deferred();
  Object.assign(c, {reconnectRevision: 1, reconnectTask: "", reconnectTarget: "D", reconnectAttempts: new Map(), work: {tasks: []},
    renderAccounts() {}, refresh: async () => {},});
  get("reconnectDialog").open = true;get("reconnectPassword").value = "synthetic";
  c.ra = async path => {calls.push(path);return gate.promise;};
  const start = source.indexOf('$("#reconnectDialog").addEventListener("close"'), end = source.indexOf('$("#exit").addEventListener', start);
  vm.runInContext(source.slice(start, end), c);
  const button = get("reconnectSubmit");await get("reconnectForm").emit("submit", {submitter: button});await tick();
  assert.ok(calls.includes("/accounts/login"));
  await get("reconnectDialog").close();gate.resolve({online: true});await tick();
  assert.equal(c.account, "A", "closing a password prompt must ignore a late successful login");
  assert.equal(c.accountTransition, false);assert.equal(get("reconnectPassword").value, "");
  assert.ok(!calls.includes("/bootstrap"));
}
async function loginAndLogoutTests() {
  const {c, get} = workflow();c.account = "";
  const loginSource = source.match(/async function login\(key, automatic=false\)\{[\s\S]*?\n\}/)[0];
  vm.runInContext(loginSource, c);
  c.ra = async () => {throw new Error("synthetic rejected password");};
  await c.login("B", true);
  assert.equal(c.root.accounts.find(profile => profile.id === "B").online, false, "a failed login must clear the old connected label");
  assert.equal(get("loginError").hidden, false);assert.equal(c.accountTransition, false);
  let payload;
  c.ra = async (path, body) => {
    if(path === "/accounts/login") {payload = body;return {account: {id: "B"}};}
    return c.root;
  };
  await c.login("B", true);assert.equal(payload.id, "B");assert.deepEqual(Object.keys(payload), ["id"]);
  assert.equal(c.account, "B");assert.equal(c.accountTransition, false);
  c.root.accounts.find(profile => profile.id === "B").online = true;
  c.act = callback => callback();
  const start = source.indexOf('$("#logout").addEventListener'), end = source.indexOf('$("#reconnect").addEventListener', start);
  vm.runInContext(source.slice(start, end), c);await get("logout").emit("click");
  assert.equal(c.account, "");assert.equal(c.root.accounts.find(profile => profile.id === "B").online, false);
  const editing = workflow();editing.c.act = callback => callback();
  const editStart = source.indexOf('$("#editLogin").addEventListener'), editEnd = source.indexOf('$("#saveConcurrency").addEventListener', editStart);
  vm.runInContext(source.slice(editStart, editEnd), editing.c);
  editing.c.api = async () => {throw new Error("synthetic draft failure");};
  await assert.rejects(editing.get("editLogin").emit("click"));
  assert.equal(editing.c.account, "A", "editing credentials must preserve the workspace if its draft cannot be saved");
  assert.equal(editing.c.accountTransition, false);
}
(async () => {
  await presentationTests(false);await presentationTests(true);await compactIdentityTests();await lifecycleTests();await failureTests();await savedLoginTests();await cancelledReconnectTests();await loginAndLogoutTests();
  console.log("Account cards, switching isolation, navigation memory and cancellation checks passed");
})().catch(error => {console.error(error);process.exitCode = 1;});
