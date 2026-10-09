// Unit tests of the Canvas workspace page module (contract section 4 of run canvas-multi-device):
// the pure functions, the markup roles and labels, and the page script run in node:vm against a
// small fake DOM built from the real markup and a fake server that implements the /api contract
// of section 3.6. No browser, Canvas, device or network is involved, and nothing is written to disk.
import assert from "node:assert/strict";
import test, { describe } from "node:test";
import { Script, createContext } from "node:vm";

import {
  EMPTY_WORKSPACE_TEXT,
  LAYOUTS,
  hotkey,
  normalizeModel,
  parseTabPath,
  renderWorkspacePage,
  tabPath,
  workspaceMarkup,
  workspaceScript,
  workspaceStyles,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-workspace-page.mjs";

// --- a minimal DOM: enough of the API the workspace script uses ---------------------------------

const VOID = new Set(["input", "br", "hr", "img", "meta", "link"]);
const ENTITIES = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };
const decode = (value) => value.replace(/&(amp|lt|gt|quot|#39);/g, (entity) => ENTITIES[entity]);

class FakeText {
  constructor(data) { this.nodeType = 3; this.data = data; this.parentNode = null; }
  get textContent() { return this.data; }
  cloneNode() { return new FakeText(this.data); }
}

class FakeElement {
  constructor(document, tag) {
    this.nodeType = 1;
    this.ownerDocument = document;
    this.tagName = tag.toUpperCase();
    this.attributes = new Map();
    this.childNodes = [];
    this.parentNode = null;
    this.listeners = new Map();
    this.state = {};
    if (this.tagName === "IFRAME") {
      const messages = [];
      // Messages are copied out of the page's realm so deepEqual compares plain objects.
      this.contentWindow = { messages, postMessage: (message, origin) => messages.push({ message: JSON.parse(JSON.stringify(message)), origin }) };
    }
  }
  get localName() { return this.tagName.toLowerCase(); }
  getAttribute(name) { return this.attributes.has(name) ? this.attributes.get(name) : null; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  hasAttribute(name) { return this.attributes.has(name); }
  reflectBoolean(name, value) { if (value) this.setAttribute(name, ""); else this.removeAttribute(name); }
  get id() { return this.getAttribute("id") || ""; }
  get className() { return this.getAttribute("class") || ""; }
  set className(value) { this.setAttribute("class", value); }
  get hidden() { return this.hasAttribute("hidden"); }
  set hidden(value) { this.reflectBoolean("hidden", value); }
  get disabled() { return this.hasAttribute("disabled"); }
  set disabled(value) { this.reflectBoolean("disabled", value); }
  get title() { return this.getAttribute("title") || ""; }
  set title(value) { this.setAttribute("title", value); }
  get value() { return "value" in this.state ? this.state.value : this.getAttribute("value") || ""; }
  set value(value) { this.state.value = String(value); }
  get classList() {
    const read = () => this.className.split(/\s+/).filter(Boolean);
    return { contains: (name) => read().includes(name) };
  }
  get children() { return this.childNodes.filter((node) => node.nodeType === 1); }
  get textContent() { return this.childNodes.map((node) => node.textContent).join(""); }
  set textContent(value) {
    for (const node of this.childNodes) node.parentNode = null;
    this.childNodes = value === "" ? [] : [new FakeText(String(value))];
    this.childNodes.forEach((node) => { node.parentNode = this; });
  }
  appendChild(node) { return this.insertBefore(node, null); }
  insertBefore(node, reference) {
    if (node.parentNode) node.parentNode.removeChild(node);
    const index = reference ? this.childNodes.indexOf(reference) : -1;
    if (index < 0) this.childNodes.push(node); else this.childNodes.splice(index, 0, node);
    node.parentNode = this;
    return node;
  }
  removeChild(node) {
    const index = this.childNodes.indexOf(node);
    if (index < 0) throw new Error("not a child");
    this.childNodes.splice(index, 1);
    node.parentNode = null;
    return node;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  cloneNode(deep) {
    const copy = new FakeElement(this.ownerDocument, this.localName);
    for (const [name, value] of this.attributes) copy.setAttribute(name, value);
    if (deep) for (const child of this.childNodes) copy.appendChild(child.cloneNode(true));
    return copy;
  }
  descendants() {
    const out = [];
    const walk = (element) => { for (const child of element.children) { out.push(child); walk(child); } };
    walk(this);
    return out;
  }
  querySelectorAll(selector) { return this.descendants().filter((element) => element.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  matches(selector) { return selector.split(",").some((part) => matchesCompound(this, part.trim())); }
  closest(selector) {
    for (let node = this; node && node.nodeType === 1; node = node.parentNode) if (node.matches(selector)) return node;
    return null;
  }
  contains(node) { for (let item = node; item; item = item.parentNode) if (item === this) return true; return false; }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
  focus() {
    const previous = this.ownerDocument.activeElement;
    this.ownerDocument.activeElement = this;
    if (previous && previous !== this) dispatch(previous, "blur", {}, false);
  }
  // innerHTML is deliberately absent: a script that relied on it would fail loudly here.
  set innerHTML(_) { throw new Error("innerHTML is not allowed on the workspace page"); }
  get innerHTML() { throw new Error("innerHTML is not allowed on the workspace page"); }
}

function matchesCompound(element, selector) {
  const pattern = /^([a-z0-9]+)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:=(?:"([^"]*)"|([^\]]*)))?\]/gy;
  let match;
  let consumed = 0;
  pattern.lastIndex = 0;
  while (consumed < selector.length && (match = pattern.exec(selector))) {
    consumed = pattern.lastIndex;
    const [, tag, id, className, attribute, quoted, bare] = match;
    if (tag && element.localName !== tag) return false;
    if (id && element.id !== id) return false;
    if (className && !element.classList.contains(className)) return false;
    if (attribute) {
      if (!element.hasAttribute(attribute)) return false;
      const expected = quoted ?? bare;
      if (expected !== undefined && element.getAttribute(attribute) !== expected) return false;
    }
  }
  if (consumed !== selector.length) throw new Error(`unsupported selector ${selector}`);
  return true;
}

class FakeDocument {
  constructor() {
    this.activeElement = null;
    this.documentElement = new FakeElement(this, "html");
    this.body = null;
    this.title = "";
    this.visibilityState = "visible";
    this.listeners = new Map();
    this.window = null;
  }
  createElement(tag) { return new FakeElement(this, tag); }
  getElementById(id) { return this.documentElement.descendants().find((element) => element.id === id) || null; }
  querySelectorAll(selector) { return this.documentElement.querySelectorAll(selector); }
  querySelector(selector) { return this.documentElement.querySelector(selector); }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }
}

function parseInto(document, parent, html) {
  const token = /<\/([a-zA-Z0-9]+)\s*>|<([a-zA-Z0-9]+)((?:\s+[^\s=>/]+(?:="[^"]*")?)*)\s*(\/?)>|([^<]+)/g;
  const stack = [parent];
  let match;
  let consumed = 0;
  while ((match = token.exec(html))) {
    assert.equal(match.index, consumed, `unparsed markup near ${html.slice(consumed, consumed + 40)}`);
    consumed = token.lastIndex;
    const [, closing, tag, attributes, selfClosing, text] = match;
    const top = stack[stack.length - 1];
    if (closing) {
      assert.equal(top.localName, closing.toLowerCase(), `mismatched </${closing}>`);
      stack.pop();
    } else if (tag) {
      const element = document.createElement(tag.toLowerCase());
      for (const [, name, value] of attributes.matchAll(/([^\s=]+)(?:="([^"]*)")?/g)) element.setAttribute(name, decode(value ?? ""));
      top.appendChild(element);
      if (!selfClosing && !VOID.has(element.localName)) stack.push(element);
    } else if (text) {
      top.appendChild(new FakeText(decode(text)));
    }
  }
  assert.equal(consumed, html.length, "markup parsed to the end");
  assert.equal(stack.length, 1, "every element closed");
}

function parseMarkup(html) {
  const document = new FakeDocument();
  parseInto(document, document.documentElement, html);
  return document;
}

/** Dispatches an event on target, bubbling through its ancestors, then the document and window. */
function dispatch(target, type, properties = {}, bubbles = true) {
  let stopped = false;
  const event = {
    type, target, defaultPrevented: false, ...properties,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { stopped = true; },
  };
  const chain = [];
  for (let node = target; node; node = bubbles ? node.parentNode : null) chain.push(node);
  const document = target.ownerDocument || target;
  if (bubbles && document && document !== target) chain.push(document);
  if (bubbles && document && document.window) chain.push(document.window);
  for (const node of chain) {
    if (stopped) break;
    for (const listener of node.listeners?.get(type) || []) {
      event.currentTarget = node;
      listener(event);
    }
  }
  return event;
}

function click(target) {
  assert.ok(target, "click target exists");
  if (target.disabled) return null;
  return dispatch(target, "click");
}

const flush = async () => { for (let i = 0; i < 30; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

// --- fake timers ----------------------------------------------------------------------------

function fakeTimers() {
  const timers = new Map();
  let now = 0;
  let next = 1;
  const add = (fn, ms, every) => { const id = next++; timers.set(id, { fn, at: now + Math.max(0, ms || 0), every }); return id; };
  return {
    setInterval: (fn, ms) => add(fn, ms, Math.max(1, ms || 1)),
    setTimeout: (fn, ms) => add(fn, ms, 0),
    clear: (id) => { timers.delete(id); },
    intervals: () => [...timers.values()].filter((timer) => timer.every).map((timer) => timer.every).sort((a, b) => a - b),
    async tick(ms) {
      const end = now + ms;
      for (;;) {
        let due = null;
        for (const [id, timer] of timers) if (timer.at <= end && (!due || timer.at < due[1].at)) due = [id, timer];
        if (!due) break;
        const [id, timer] = due;
        now = timer.at;
        if (timer.every) timer.at += timer.every; else timers.delete(id);
        timer.fn();
        await flush();
      }
      now = end;
    },
  };
}

// --- a fake Canvas server implementing the /api contract (section 3.6) ------------------------

const ANDROID = "android~emulator-5580";
const IOS = "ios~45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5";
const NETWORK = "android~127.0.0.1:5555";
const TAB_1 = "t_00000001";
const TAB_2 = "t_00000002";

function summary(id, extra = {}) {
  const [platform, target] = id.split("~");
  return {
    id, platform, target, name: platform === "ios" ? "Autonom-Fast-Test" : "Pixel 8", state: "live",
    tab: null, slot: 0, primary: false, focused: false, profile: "background", transport: platform === "ios" ? "idb" : "scrcpy",
    fps_cap: 30, max_size: 1024, scale_factor: null, control_owner: "shared", input_paused: false,
    session: { id: "s_1", started_by_canvas: true, reused: false }, session_error: null, booted_by_canvas: false,
    error: null, url: "/d/" + encodeURIComponent(id) + "/", attached_at: "2026-10-07T10:00:00Z", ...extra,
  };
}

class FakeCanvas {
  constructor({ tabs, devices = [], absent = [], running = [], bootable = [], activeTab = null, limits = {}, auth = null } = {}) {
    this.tabs = tabs || [{ id: TAB_1, name: "Canvas 1", layout: 1, slots: [null] }];
    this.tabs.forEach((tab) => { tab.created_at = "2026-10-07T10:00:00Z"; });
    this.devices = new Map(devices.map((device) => [device.id, device]));
    this.absent = absent;
    this.running = running;
    this.bootable = bootable;
    this.activeTab = activeTab || this.tabs[0].id;
    this.limits = { max_tabs: 8, max_tiles: 4, max_devices: 8, ...limits };
    this.auth = auth;
    this.focus = null;
    this.jobs = new Map();
    this.nextTab = 10;
    this.overrides = [];
    this.revision = 1;
    this.gates = new Map();
  }
  tab(id) { return this.tabs.find((tab) => tab.id === id) || null; }
  locate(id) {
    for (const tab of this.tabs) {
      const slot = tab.slots.findIndex((ref) => ref && ref.device === id);
      if (slot >= 0) return { tab, slot };
    }
    return null;
  }
  snapshot() {
    const tabs = this.tabs.map((tab) => ({ ...tab, url: "/c/" + tab.id, slots: tab.slots.map((ref) => (ref ? { ...ref } : null)) }));
    const devices = [...this.devices.values()].map((device) => {
      const place = this.locate(device.id);
      return { ...device, tab: place ? place.tab.id : null, slot: place ? place.slot : null, focused: this.focus === device.id };
    });
    return { ok: true, mode: "workspace", name: "default", revision: this.revision, persisted: true, active_tab: this.activeTab, tabs, devices, absent: this.absent, limits: this.limits };
  }
  error(status, error_code, error, hint = null) { return { status, body: { ok: false, error, error_code, hint } }; }
  firstEmpty(tab) { return tab.slots.findIndex((ref) => ref === null); }
  handle(method, path, body) {
    for (const override of this.overrides) {
      const answer = override(method, path, body);
      if (answer) return answer;
    }
    this.revision += method === "POST" ? 1 : 0;
    let match;
    if (method === "POST" && path === "/auth") {
      if (this.auth === "refuse") return { status: 401, body: { error: "Unauthorized" } };
      return { status: 200, body: { ok: true, csrf: "csrf-1" } };
    }
    if (method === "GET" && path === "/api/workspace") return { status: 200, body: this.snapshot() };
    if (method === "GET" && path === "/api/targets") {
      const running = this.running.map((target) => {
        const id = target.platform + "~" + target.target;
        const place = this.locate(id);
        return { attachable: true, reason: null, state: "device", ...target, attached: this.devices.has(id), tab: this.devices.has(id) && place ? place.tab.id : null };
      });
      return { status: 200, body: { ok: true, running, bootable: this.bootable } };
    }
    if (method === "POST" && path === "/api/tabs") {
      if (this.tabs.length >= this.limits.max_tabs) return this.error(409, "tab_limit", "At most 8 tabs");
      const tab = { id: "t_000000" + String(this.nextTab++).padStart(2, "0"), name: "Canvas " + (this.tabs.length + 1), layout: body.layout || 1, slots: [null], created_at: "x" };
      this.tabs.push(tab);
      return { status: 201, body: { ok: true, tab: { ...tab, url: "/c/" + tab.id } } };
    }
    if ((match = /^\/api\/tabs\/([^/]+)\/activate$/.exec(path)) && method === "POST") {
      if (!this.tab(match[1])) return this.error(404, "tab_not_found", "No such tab");
      this.activeTab = match[1];
      return { status: 200, body: { ok: true, active_tab: match[1] } };
    }
    if ((match = /^\/api\/tabs\/([^/]+)\/close$/.exec(path)) && method === "POST") {
      const tab = this.tab(match[1]);
      if (!tab) return this.error(404, "tab_not_found", "No such tab");
      const detached = tab.slots.filter((ref) => ref && ref.present !== false).map((ref) => ref.device);
      for (const id of detached) this.devices.delete(id);
      this.tabs = this.tabs.filter((item) => item !== tab);
      let created = null;
      if (!this.tabs.length) { created = { id: "t_000000ff", name: "Canvas 1", layout: 1, slots: [null], created_at: "x" }; this.tabs.push(created); }
      if (this.activeTab === tab.id) this.activeTab = this.tabs[0].id;
      return { status: 200, body: { ok: true, closed: tab.id, detached, created } };
    }
    if ((match = /^\/api\/tabs\/([^/]+)\/slots\/(\d+)\/clear$/.exec(path)) && method === "POST") {
      const tab = this.tab(match[1]);
      if (!tab) return this.error(404, "tab_not_found", "No such tab");
      const slot = Number(match[2]);
      if (tab.slots[slot] && tab.slots[slot].present !== false) return this.error(409, "slot_live", "A live device", "Detach it instead");
      const ref = tab.slots[slot];
      tab.slots[slot] = null;
      if (ref) this.absent = this.absent.filter((item) => item.id !== ref.device);
      return { status: 200, body: { ok: true, tab } };
    }
    if ((match = /^\/api\/tabs\/([^/]+)$/.exec(path)) && method === "POST") {
      const tab = this.tab(match[1]);
      if (!tab) return this.error(404, "tab_not_found", "No such tab");
      if (body.name !== undefined) tab.name = body.name;
      if (body.layout !== undefined) {
        const filled = tab.slots.filter(Boolean);
        if (body.layout < filled.length) return this.error(409, "layout_too_small", "Too small");
        tab.slots = [...filled, ...Array(body.layout - filled.length).fill(null)];
        tab.layout = body.layout;
      }
      return { status: 200, body: { ok: true, tab: { ...tab, url: "/c/" + tab.id } } };
    }
    if (method === "POST" && path === "/api/devices") {
      const id = body.platform + "~" + body.target;
      if (this.devices.size >= this.limits.max_devices) return this.error(409, "device_limit", "Device limit reached", "Detach a device first");
      const tab = this.tab(body.tab);
      if (!tab) return this.error(404, "tab_not_found", "No such tab");
      const slot = Number.isInteger(body.slot) ? body.slot : this.firstEmpty(tab);
      if (tab.slots[slot]) return this.error(409, "slot_taken", "Slot taken");
      tab.slots[slot] = { device: id, present: true };
      const device = summary(id, { name: (this.running.find((item) => item.target === body.target) || {}).name || body.target });
      this.devices.set(id, device);
      return { status: 201, body: { ok: true, device, attached: true } };
    }
    // Contract 3.6: move, detach and reconnect live under /api/devices/<id>/; only control is a device route.
    if ((match = /^\/api\/devices\/([^/]+)\/(move|detach|reconnect)$/.exec(path) || /^\/d\/([^/]+)\/(control)$/.exec(path)) && method === "POST") {
      const id = decodeURIComponent(match[1]);
      const device = this.devices.get(id);
      if (!device) return this.error(404, "device_not_found", "No such device");
      if (match[2] === "move") {
        const tab = this.tab(body.tab);
        if (!tab) return this.error(404, "tab_not_found", "No such tab");
        const from = this.locate(id);
        if (tab.slots[body.slot]) return this.error(409, "slot_taken", "Slot taken");
        if (from) from.tab.slots[from.slot] = null;
        tab.slots[body.slot] = { device: id, present: true };
        return { status: 200, body: { ok: true, device } };
      }
      if (match[2] === "detach") {
        const from = this.locate(id);
        if (from) from.tab.slots[from.slot] = null;
        this.devices.delete(id);
        return { status: 200, body: { ok: true, detached: id, session_stopped: true } };
      }
      if (match[2] === "reconnect") { device.state = "attaching"; return { status: 202, body: { ok: true, device } }; }
      if (body.mode === "pause") device.input_paused = true;
      if (body.mode === "resume") device.input_paused = false;
      if (body.mode === "takeover") device.control_owner = "human";
      if (body.mode === "release") device.control_owner = "shared";
      return { status: 200, body: { ok: true, control_owner: device.control_owner, input_paused: device.input_paused } };
    }
    if (method === "POST" && path === "/api/focus") {
      if (body.id !== null && !this.devices.has(body.id)) return this.error(404, "device_not_found", "No such device");
      this.focus = body.id;
      return { status: 200, body: { ok: true, focus: body.id } };
    }
    if (method === "POST" && path === "/api/boot") {
      if (this.devices.size >= this.limits.max_devices) return this.error(409, "device_limit", "Device limit reached");
      const job = { id: "b_0000000" + (this.jobs.size + 1), kind: body.kind, name: body.name ?? null, udid: body.udid ?? null, port: null, state: "booting",
        attach: true, tab: body.tab ?? null, slot: body.slot ?? null, target: null, device: null, already_running: false, error_code: null, error: null,
        started_at: "x", finished_at: null, elapsed_ms: 0 };
      this.jobs.set(job.id, job);
      return { status: 202, body: { ok: true, job } };
    }
    if ((match = /^\/api\/boot\/([^/]+)$/.exec(path)) && method === "GET") {
      const job = this.jobs.get(match[1]);
      if (!job) return this.error(404, "job_not_found", "No such job");
      return { status: 200, body: { ok: true, job: { ...job } } };
    }
    return this.error(404, "not_found", "Not found: " + method + " " + path);
  }
}

// --- the page harness -----------------------------------------------------------------------

function boot({ server = new FakeCanvas(), path = "/", hash = "", model = {}, holdAuth = false } = {}) {
  const normalized = { title: "Autonom Canvas", workspace: "default", initial_tab: null, ...model };
  const document = parseMarkup(`<body>${workspaceMarkup(normalized)}</body>`);
  document.body = document.documentElement.children[0];
  const timers = fakeTimers();
  const calls = [];
  const history = [];
  const location = { origin: "http://127.0.0.1:3277", protocol: "http:", host: "127.0.0.1:3277", pathname: path, search: "", hash };
  const navigate = (kind, url) => {
    history.push({ kind, url });
    const parsed = new URL(url, location.origin);
    location.pathname = parsed.pathname; location.search = parsed.search; location.hash = parsed.hash;
  };
  let releaseAuth = () => {};
  const authGate = holdAuth ? new Promise((resolve) => { releaseAuth = resolve; }) : null;
  const fetch = async (url, init = {}) => {
    const body = init.body ? JSON.parse(init.body) : null;
    const method = init.method || "GET";
    calls.push({ path: url, method, headers: JSON.parse(JSON.stringify(init.headers || {})), body });
    if (url === "/auth" && authGate) await authGate;
    const answer = server.handle(method, url, body || {});
    // A gate holds an answer computed now, so a test can deliver it late (a stale snapshot).
    const gate = server.gates.get(method + " " + url);
    if (gate) { server.gates.delete(method + " " + url); await gate; }
    return { ok: answer.status < 400, status: answer.status, statusText: "", json: async () => JSON.parse(JSON.stringify(answer.body)) };
  };
  const sandbox = {
    document, location, fetch, console, URLSearchParams,
    history: { pushState: (_, __, url) => navigate("push", url), replaceState: (_, __, url) => navigate("replace", url) },
    setInterval: timers.setInterval, clearInterval: timers.clear, setTimeout: timers.setTimeout, clearTimeout: timers.clear,
    listeners: new Map(),
    addEventListener(type, listener) { if (!this.listeners.has(type)) this.listeners.set(type, []); this.listeners.get(type).push(listener); },
  };
  sandbox.window = sandbox;
  document.window = sandbox;
  const context = createContext(sandbox);
  new Script(workspaceScript(normalized), { filename: "workspace-script.js" }).runInContext(context);
  const $ = (id) => document.getElementById(id);
  const page = {
    document, $, calls, history, timers, location, server, sandbox, releaseAuth,
    posts: (path) => calls.filter((call) => call.method === "POST" && (path === undefined || call.path === path)),
    gets: (path) => calls.filter((call) => call.method === "GET" && call.path === path),
    tabs: () => $("ws-tabs").querySelectorAll("[role=tab]"),
    tabByName: (name) => $("ws-tabs").querySelectorAll("[role=tab]").find((tab) => tab.textContent === name),
    tiles: () => $("ws-grid").children,
    iframes: () => $("ws-grid").querySelectorAll("iframe"),
    tile: (index) => $("ws-grid").children[index],
    button: (root, label) => root.querySelectorAll("button").find((item) => item.getAttribute("aria-label") === label || item.textContent === label),
    message(iframe, data, origin = location.origin) {
      for (const listener of sandbox.listeners.get("message") || []) listener({ origin, source: iframe ? iframe.contentWindow : {}, data });
    },
    key(properties) { return dispatch(document.activeElement || document.body, "keydown", properties); },
    popstate(url) { navigate("browser", url); for (const listener of sandbox.listeners.get("popstate") || []) listener({}); },
    sent: (iframe) => iframe.contentWindow.messages.map((entry) => entry.message),
  };
  return page;
}

async function started(options) {
  const page = boot(options);
  await flush();
  return page;
}

function twoTabServer(extra = {}) {
  return new FakeCanvas({
    tabs: [
      { id: TAB_1, name: "Canvas 1", layout: 2, slots: [{ device: ANDROID, present: true }, { device: IOS, present: true }] },
      { id: TAB_2, name: "Canvas 2", layout: 1, slots: [{ device: NETWORK, present: true }] },
    ],
    devices: [summary(ANDROID), summary(IOS), summary(NETWORK, { name: "Network phone" })],
    ...extra,
  });
}

// --- pure functions -------------------------------------------------------------------------

describe("pure functions", () => {
  test("LAYOUTS lists 1-4 tiles as columns and rows and is frozen", () => {
    assert.deepEqual(JSON.parse(JSON.stringify(LAYOUTS)), {
      1: { columns: 1, rows: 1 }, 2: { columns: 2, rows: 1 }, 3: { columns: 3, rows: 1 }, 4: { columns: 2, rows: 2 },
    });
    assert.ok(Object.isFrozen(LAYOUTS));
    assert.ok(Object.isFrozen(LAYOUTS[4]));
  });

  test("tabPath and parseTabPath round-trip tab ids", () => {
    assert.equal(tabPath("t_1a2b3c4d"), "/c/t_1a2b3c4d");
    assert.equal(parseTabPath("/c/t_1a2b3c4d"), "t_1a2b3c4d");
    assert.equal(parseTabPath(tabPath("t_00000000")), "t_00000000");
    assert.equal(parseTabPath("/"), null);
    for (const other of ["/c/t_1A2B3C4D", "/c/t_1a2b3c4", "/c/t_1a2b3c4d/", "/c/", "/c", "/d/android~x/", "", "/index.html", undefined, null, 42]) {
      assert.equal(parseTabPath(other), undefined, `${other} is not a tab path`);
    }
  });

  test("hotkey maps Ctrl+Alt+1..4 to tiles and Ctrl+Alt+Left/Right to tabs", () => {
    const base = { ctrlKey: true, altKey: true, metaKey: false, shiftKey: false };
    for (const n of [1, 2, 3, 4]) assert.deepEqual(hotkey({ ...base, code: `Digit${n}`, key: "¡" }), { tile: n });
    assert.deepEqual(hotkey({ ...base, key: "3" }), { tile: 3 }, "key is the fallback when code is missing");
    assert.deepEqual(hotkey({ ...base, code: "ArrowLeft", key: "ArrowLeft" }), { tab: -1 });
    assert.deepEqual(hotkey({ ...base, code: "ArrowRight", key: "ArrowRight" }), { tab: 1 });
    assert.deepEqual(hotkey({ ...base, key: "ArrowRight" }), { tab: 1 });
    assert.equal(hotkey({ ...base, code: "Digit5", key: "5" }), null);
    assert.equal(hotkey({ ...base, code: "KeyA", key: "1" }), null, "the physical key wins over a produced character");
    assert.equal(hotkey({ ...base, ctrlKey: false, code: "Digit1" }), null);
    assert.equal(hotkey({ ...base, altKey: false, code: "Digit1" }), null);
    assert.equal(hotkey({ ...base, metaKey: true, code: "Digit1" }), null);
    assert.equal(hotkey({ ...base, shiftKey: true, code: "ArrowLeft" }), null);
    assert.equal(hotkey({ ...base, code: "ArrowUp" }), null);
    assert.equal(hotkey(null), null);
    assert.equal(hotkey(undefined), null);
  });

  test("normalizeModel keeps known fields and replaces bad values with defaults", () => {
    assert.deepEqual(normalizeModel(undefined), {
      title: "Autonom Canvas", workspace: "default", initial_tab: null,
      limits: { max_tabs: 8, max_tiles: 4, max_devices: 8 }, platforms: ["android", "ios"],
    });
    const model = normalizeModel({ title: "  Lab  ", workspace: "qa.team-1", initial_tab: "t_0123abcd",
      limits: { max_tabs: 3, max_tiles: 9, max_devices: 0 }, platforms: ["ios", "web", "ios"], extra: "dropped" });
    assert.deepEqual(model, { title: "Lab", workspace: "qa.team-1", initial_tab: "t_0123abcd",
      limits: { max_tabs: 3, max_tiles: 4, max_devices: 8 }, platforms: ["ios"] });
    assert.equal(normalizeModel({ workspace: "</script>" }).workspace, "default");
    assert.equal(normalizeModel({ initial_tab: "t_ZZZ" }).initial_tab, null);
  });
});

// --- document and markup --------------------------------------------------------------------

describe("renderWorkspacePage and markup", () => {
  test("is a complete document with one style, one script and the phone and dark rules", () => {
    const html = renderWorkspacePage({ title: "Autonom Canvas", workspace: "default", initial_tab: "t_1a2b3c4d" });
    assert.match(html, /^<!doctype html>\n<html lang="en">/);
    assert.match(html, /<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">/);
    assert.match(html, /<title>Autonom Canvas<\/title>/);
    assert.equal(html.match(/<style>/g).length, 1);
    assert.equal(html.match(/<script>/g).length, 1);
    assert.ok(html.trimEnd().endsWith("</html>"));
    const styles = workspaceStyles();
    assert.match(styles, /@media \(prefers-color-scheme:dark\)\{:root\{/);
    assert.match(styles, /@media \(max-width:760px\)\{/);
    assert.match(styles, /\.ws-tabs\{[^}]*overflow-x:auto/);
    assert.match(styles, /@media \(prefers-reduced-motion:reduce\)/);
    assert.match(styles, /body\{[^}]*background:var\(--bg\)/);
    // Phone tap targets are at least 44 px high: tabs, their close button, icons, buttons and the control chip.
    const phone = styles.slice(styles.indexOf("@media (max-width:760px){"));
    for (const rule of [/\.ws-tab-button\{height:44px/, /\.ws-tab-close\{[^}]*height:44px/, /\.ws-icon\{width:44px;height:44px\}/,
      /\.btn,\.btn\.small,\.tile-pop-item,\.ws-menu-item\{height:44px\}/, /\.tile-chip\{height:44px\}/]) {
      assert.match(phone, rule);
    }
    // The layout buttons keep 44 px too: their more specific rule must not shrink them.
    assert.match(phone, /\.ws-layouts \.ws-icon\{width:44px;height:44px\}/);
    const phoneOnly = phone.slice(0, phone.indexOf("@media (prefers-reduced-motion"));
    for (const [, size] of phoneOnly.matchAll(/[{;]height:(\d+)px/g)) assert.ok(Number(size) >= 44, "phone height " + size + "px is below 44 px");
    assert.match(styles, /\.btn\.danger:focus-visible\{outline-color:var\(--text\)\}/);
  });

  test("the script is plain JavaScript: it compiles, has no backticks and never uses innerHTML", () => {
    const script = workspaceScript({ initial_tab: "t_1a2b3c4d" });
    assert.doesNotThrow(() => new Script(script));
    assert.equal(script.includes("`"), false);
    assert.equal(/innerHTML|outerHTML|insertAdjacentHTML|document\.write/.test(script), false);
  });

  test("model text is escaped in the title and cannot close the script", () => {
    const html = renderWorkspacePage({ title: "</title><script>alert(1)</script>", workspace: "</script><b>" });
    assert.match(html, /<title>&lt;\/title&gt;&lt;script&gt;alert\(1\)&lt;\/script&gt;<\/title>/);
    const script = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));
    assert.equal(script.includes("</"), false, "no closing tag inside the inline script");
    assert.match(script, /"title":"\\u003c\/title>\\u003cscript>alert\(1\)\\u003c\/script>"/);
    assert.match(script, /"workspace":"default"/);
  });

  test("tab bar, layout group, menu, stage and confirmation carry their roles and labels", () => {
    const document = parseMarkup(workspaceMarkup({ workspace: "lab" }));
    const tablist = document.getElementById("ws-tabs");
    assert.equal(tablist.getAttribute("role"), "tablist");
    assert.equal(tablist.getAttribute("aria-label"), "Canvas tabs");
    assert.equal(document.getElementById("ws-name").textContent, "lab");
    assert.equal(document.getElementById("ws-new-tab").getAttribute("aria-label"), "New tab");
    const layouts = document.getElementById("ws-layouts");
    assert.equal(layouts.getAttribute("role"), "group");
    assert.equal(layouts.getAttribute("aria-label"), "Layout");
    assert.deepEqual(layouts.querySelectorAll("button").map((item) => [item.getAttribute("data-layout-choice"), item.getAttribute("aria-pressed")]),
      [["1", "false"], ["2", "false"], ["3", "false"], ["4", "false"]]);
    const menu = document.getElementById("ws-menu");
    assert.equal(menu.getAttribute("role"), "menu");
    assert.ok(menu.hidden);
    assert.equal(document.getElementById("ws-menu-button").getAttribute("aria-controls"), "ws-menu");
    assert.deepEqual(menu.querySelectorAll("[role=menuitem]").map((item) => item.textContent), ["Rename…", "Open in new window", "Close tab"]);
    const open = document.getElementById("ws-menu-open");
    assert.equal(open.localName, "a");
    assert.equal(open.getAttribute("target"), "_blank");
    assert.equal(open.getAttribute("rel"), "noopener");
    assert.equal(document.getElementById("ws-stage").getAttribute("role"), "tabpanel");
    const confirm = document.getElementById("ws-confirm");
    assert.equal(confirm.getAttribute("role"), "alertdialog");
    assert.ok(document.getElementById(confirm.getAttribute("aria-labelledby")));
    assert.ok(document.getElementById(confirm.getAttribute("aria-describedby")));
    assert.equal(document.getElementById("ws-empty").textContent, EMPTY_WORKSPACE_TEXT);
    assert.equal(EMPTY_WORKSPACE_TEXT, "Attach a device or boot one. The Canvas keeps running when devices leave; stop it with Ctrl+C or `autonom canvas stop`.");
    assert.equal(document.getElementById("ws-notice").getAttribute("role"), "status");
    for (const item of document.querySelectorAll("button")) {
      assert.equal(item.getAttribute("type"), "button", `${item.id} has a type`);
      assert.ok(item.textContent.trim() || item.getAttribute("aria-label"), `${item.id} has a name`);
    }
    assert.ok(document.getElementById("ws-icons").hidden);
  });

  test("layout buttons follow max_tiles and the markup holds no device data", () => {
    const markup = workspaceMarkup({ limits: { max_tiles: 2 } });
    const document = parseMarkup(markup);
    assert.equal(document.querySelectorAll("[data-layout-choice]").length, 2);
    assert.equal(/emulator-|android~|ios~|iframe/.test(markup), false);
  });
});

// --- the script against the fake server -----------------------------------------------------

describe("boot, sign-in and polling", () => {
  test("the token leaves the URL at once and tiles are created only after sign-in", async () => {
    const server = twoTabServer();
    const page = boot({ server, path: "/c/" + TAB_1, hash: "#token=secret-token", model: { initial_tab: TAB_1 }, holdAuth: true });
    assert.deepEqual(page.history[0], { kind: "replace", url: "/c/" + TAB_1 });
    assert.equal(page.location.hash, "");
    await flush();
    assert.deepEqual(page.calls.map((call) => call.path), ["/auth"]);
    assert.deepEqual(page.calls[0].body, { token: "secret-token" });
    assert.equal(page.iframes().length, 0);
    page.releaseAuth();
    await flush();
    assert.equal(page.gets("/api/workspace").length, 1);
    assert.deepEqual(page.iframes().map((frame) => frame.getAttribute("src")),
      ["/d/android~emulator-5580/?embed=1", "/d/ios~45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5/?embed=1"]);
    assert.equal(page.iframes()[0].getAttribute("title"), "Pixel 8 screen");
    assert.deepEqual(page.tabs().map((tab) => [tab.textContent, tab.getAttribute("aria-selected"), tab.getAttribute("tabindex")]),
      [["Canvas 1", "true", "0"], ["Canvas 2", "false", "-1"]]);
    assert.equal(page.$("ws-stage").getAttribute("aria-labelledby"), "ws-tab-" + TAB_1);
    assert.equal(page.document.title, "Canvas 1 · Autonom Canvas");
    assert.equal(page.$("ws-menu-open").getAttribute("href"), "/c/" + TAB_1);
  });

  test("without a token the page signs in with an empty body and opens the active tab", async () => {
    const page = await started({ server: twoTabServer({ activeTab: TAB_2 }) });
    assert.deepEqual(page.calls[0], { path: "/auth", method: "POST", headers: { "Content-Type": "application/json" }, body: {} });
    assert.equal(page.location.pathname, "/c/" + TAB_2);
    assert.equal(page.tabByName("Canvas 2").getAttribute("aria-selected"), "true");
    assert.deepEqual(page.iframes().map((frame) => frame.getAttribute("src")), ["/d/android~127.0.0.1%3A5555/?embed=1"]);
    assert.equal(page.$("ws-notice").hidden, true, "no closed-tab notice when no tab was asked for");
  });

  test("a refused sign-in without a token says how to sign in and does not poll", async () => {
    const page = await started({ server: new FakeCanvas({ auth: "refuse" }) });
    assert.equal(page.$("ws-notice").textContent, "Open the Canvas URL with its #token to sign in.");
    assert.equal(page.$("ws-notice").getAttribute("data-kind"), "error");
    await page.timers.tick(5000);
    assert.equal(page.gets("/api/workspace").length, 0);
  });

  test("the workspace is polled every second while visible and not while hidden", async () => {
    const page = await started({ server: twoTabServer() });
    assert.equal(page.gets("/api/workspace").length, 1);
    await page.timers.tick(3000);
    assert.equal(page.gets("/api/workspace").length, 4);
    page.document.visibilityState = "hidden";
    dispatch(page.document, "visibilitychange");
    await page.timers.tick(5000);
    assert.equal(page.gets("/api/workspace").length, 4);
    page.document.visibilityState = "visible";
    dispatch(page.document, "visibilitychange");
    await flush();
    assert.equal(page.gets("/api/workspace").length, 5, "polls at once when visible again");
    await page.timers.tick(1000);
    assert.equal(page.gets("/api/workspace").length, 6);
  });

  test("the empty-workspace text shows only while no device is attached or placed", async () => {
    const server = new FakeCanvas();
    const page = await started({ server });
    assert.equal(page.$("ws-empty").hidden, false);
    server.tabs[0].slots[0] = { device: ANDROID, present: true };
    server.devices.set(ANDROID, summary(ANDROID));
    await page.timers.tick(1000);
    assert.equal(page.$("ws-empty").hidden, true);
  });
});

describe("tabs", () => {
  test("selecting a tab pushes its URL, activates it and keeps only its tiles as iframes", async () => {
    const page = await started({ server: twoTabServer() });
    const before = page.iframes();
    assert.equal(before.length, 2);
    click(page.tabByName("Canvas 2"));
    await flush();
    assert.deepEqual(page.history.at(-1), { kind: "push", url: "/c/" + TAB_2 });
    const activate = page.posts("/api/tabs/" + TAB_2 + "/activate");
    assert.equal(activate.length, 1);
    assert.deepEqual(activate[0].headers, { "Content-Type": "application/json", "X-Autonom-Origin": "human", "X-Autonom-CSRF": "csrf-1" });
    assert.deepEqual(page.iframes().map((frame) => frame.getAttribute("src")), ["/d/android~127.0.0.1%3A5555/?embed=1"]);
    assert.ok(before.every((frame) => !page.$("ws-grid").contains(frame)), "the other tab's iframes are gone");
    assert.equal(page.server.activeTab, TAB_2);
    page.popstate("/c/" + TAB_1);
    await flush();
    assert.equal(page.tabByName("Canvas 1").getAttribute("aria-selected"), "true");
    assert.equal(page.iframes().length, 2);
  });

  test("arrow keys on the tab bar move between tabs", async () => {
    const page = await started({ server: twoTabServer() });
    page.tabByName("Canvas 1").focus();
    page.key({ key: "ArrowRight" });
    await flush();
    assert.equal(page.tabByName("Canvas 2").getAttribute("aria-selected"), "true");
    assert.equal(page.document.activeElement, page.tabByName("Canvas 2"));
  });

  test("a closed tab id shows the notice and opens the active tab", async () => {
    const page = await started({ server: twoTabServer(), path: "/c/t_deadbeef", model: { initial_tab: "t_deadbeef" } });
    assert.equal(page.$("ws-notice").textContent, "This tab was closed");
    assert.equal(page.location.pathname, "/c/" + TAB_1);
    assert.equal(page.tabByName("Canvas 1").getAttribute("aria-selected"), "true");
  });

  test("a tab closed in another window switches this one to the active tab", async () => {
    const server = twoTabServer();
    const page = await started({ server, path: "/c/" + TAB_2 });
    assert.equal(page.tabByName("Canvas 2").getAttribute("aria-selected"), "true");
    server.tabs = server.tabs.filter((tab) => tab.id !== TAB_2);
    server.devices.delete(NETWORK);
    await page.timers.tick(1000);
    assert.equal(page.$("ws-notice").textContent, "This tab was closed");
    assert.equal(page.location.pathname, "/c/" + TAB_1);
    assert.equal(page.tabs().length, 1);
  });

  test("Back to a tab closed meanwhile shows the notice and replaces the address with the active tab", async () => {
    const server = twoTabServer();
    const page = await started({ server, path: "/c/" + TAB_2 });
    server.tabs = server.tabs.filter((tab) => tab.id !== TAB_2);
    server.devices.delete(NETWORK);
    server.activeTab = TAB_1;
    await page.timers.tick(1000);
    page.$("ws-notice").textContent = "";
    page.popstate("/c/" + TAB_2);
    await flush();
    assert.equal(page.$("ws-notice").textContent, "This tab was closed");
    assert.equal(page.location.pathname, "/c/" + TAB_1);
    assert.deepEqual(page.history.at(-1), { kind: "replace", url: "/c/" + TAB_1 });
    assert.equal(page.tabByName("Canvas 1").getAttribute("aria-selected"), "true");
    assert.equal(page.posts("/api/tabs/" + TAB_2 + "/activate").length, 0);
  });

  test("a workspace answer read before a POST finished does not undo it", async () => {
    const page = await started({ server: twoTabServer() });
    let release;
    page.server.gates.set("GET /api/workspace", new Promise((resolve) => { release = resolve; }));
    await page.timers.tick(1000);
    click(page.$("ws-new-tab"));
    await flush();
    assert.equal(page.location.pathname, "/c/t_00000010");
    release();
    await flush();
    assert.equal(page.location.pathname, "/c/t_00000010");
    assert.notEqual(page.$("ws-notice").textContent, "This tab was closed");
    assert.equal(page.tabByName("Canvas 3").getAttribute("aria-selected"), "true");
    assert.equal(page.tabs().length, 3);
    await page.timers.tick(1000);
    assert.equal(page.location.pathname, "/c/t_00000010");
    assert.equal(page.server.activeTab, "t_00000010");
  });

  test("double-clicking + or a tab's close button sends one request", async () => {
    const server = twoTabServer();
    server.tabs.push({ id: "t_00000003", name: "Empty", layout: 1, slots: [null] });
    const page = await started({ server });
    click(page.$("ws-new-tab"));
    click(page.$("ws-new-tab"));
    await flush();
    assert.equal(page.posts("/api/tabs").length, 1);
    assert.equal(page.tabs().length, 4);
    const close = page.tabByName("Empty").parentNode.querySelector("button.ws-tab-close");
    click(close);
    click(close);
    await flush();
    assert.equal(page.posts("/api/tabs/t_00000003/close").length, 1);
    assert.equal(page.$("ws-notice").hidden, true, "no error after a close that worked");
    assert.equal(page.tabByName("Empty"), undefined);
  });

  test("a poll that sees this window's own close before its answer goes to the neighbour quietly", async () => {
    const page = await started({ server: twoTabServer() });
    let release;
    page.server.gates.set("POST /api/tabs/" + TAB_1 + "/close", new Promise((resolve) => { release = resolve; }));
    click(page.$("ws-menu-button"));
    click(page.$("ws-menu-close"));
    click(page.$("ws-confirm-go"));
    await flush();
    await page.timers.tick(1000);
    assert.equal(page.location.pathname, "/c/" + TAB_2);
    assert.notEqual(page.$("ws-notice").textContent, "This tab was closed");
    release();
    await flush();
    assert.deepEqual(page.tabs().map((tab) => tab.textContent), ["Canvas 2"]);
    assert.equal(page.location.pathname, "/c/" + TAB_2);
    assert.equal(page.server.activeTab, TAB_2);
    assert.notEqual(page.$("ws-notice").textContent, "This tab was closed");
  });

  test("a snapshot with an older revision is skipped, but a lasting lower revision (restart) is taken", async () => {
    const server = twoTabServer();
    const page = await started({ server });
    server.revision = 50;
    await page.timers.tick(1000);
    server.revision = 10;
    server.tabs[0].name = "Renamed";
    await page.timers.tick(2000);
    assert.equal(page.tabByName("Renamed"), undefined, "two older snapshots are ignored");
    await page.timers.tick(1000);
    assert.equal(page.tabByName("Renamed").getAttribute("aria-selected"), "true", "the third is taken");
  });

  test("+ creates a tab and selects it; it is disabled at max_tabs", async () => {
    const page = await started({ server: twoTabServer({ limits: { max_tabs: 3 } }) });
    click(page.$("ws-new-tab"));
    await flush();
    assert.equal(page.posts("/api/tabs").length, 1);
    assert.equal(page.tabs().length, 3);
    assert.equal(page.tabByName("Canvas 3").getAttribute("aria-selected"), "true");
    assert.equal(page.location.pathname, "/c/t_00000010");
    assert.equal(page.$("ws-new-tab").disabled, true);
    assert.equal(page.$("ws-new-tab").title, "At most 3 tabs");
  });

  test("rename: double-click opens an input, Enter saves and Escape cancels", async () => {
    const page = await started({ server: twoTabServer() });
    dispatch(page.tabByName("Canvas 1"), "dblclick");
    let input = page.$("ws-tabs").querySelector("input");
    assert.ok(input);
    assert.equal(input.value, "Canvas 1");
    assert.equal(input.getAttribute("aria-label"), "Tab name");
    assert.equal(page.document.activeElement, input);
    input.value = "  Checkout  ";
    dispatch(input, "keydown", { key: "Enter" });
    await flush();
    assert.deepEqual(page.posts("/api/tabs/" + TAB_1).map((call) => call.body), [{ name: "Checkout" }]);
    assert.equal(page.$("ws-tabs").querySelector("input"), null);
    assert.ok(page.tabByName("Checkout"));
    click(page.$("ws-menu-button"));
    click(page.$("ws-menu-rename"));
    input = page.$("ws-tabs").querySelector("input");
    input.value = "Ignored";
    dispatch(input, "keydown", { key: "Escape" });
    await flush();
    assert.equal(page.posts("/api/tabs/" + TAB_1).length, 1, "Escape saves nothing");
    assert.ok(page.tabByName("Checkout"));
  });

  test("closing a tab with devices asks first; Cancel keeps it, Close detaches and switches", async () => {
    const page = await started({ server: twoTabServer() });
    click(page.$("ws-menu-button"));
    assert.equal(page.$("ws-menu").hidden, false);
    assert.equal(page.$("ws-menu-button").getAttribute("aria-expanded"), "true");
    click(page.$("ws-menu-close"));
    assert.equal(page.$("ws-confirm").hidden, false);
    assert.equal(page.$("ws-confirm-title").textContent, "Close tab and detach 2 devices?");
    click(page.$("ws-confirm-cancel"));
    assert.equal(page.$("ws-confirm").hidden, true);
    assert.equal(page.posts("/api/tabs/" + TAB_1 + "/close").length, 0);
    click(page.tabByName("Canvas 1").parentNode.querySelector("button.ws-tab-close"));
    assert.equal(page.$("ws-confirm").hidden, false);
    click(page.$("ws-confirm-go"));
    await flush();
    assert.equal(page.posts("/api/tabs/" + TAB_1 + "/close").length, 1);
    assert.deepEqual(page.tabs().map((tab) => tab.textContent), ["Canvas 2"]);
    assert.equal(page.location.pathname, "/c/" + TAB_2);
    assert.equal(page.$("ws-notice").textContent === "This tab was closed", false, "closing here is not announced as closed elsewhere");
  });

  test("a tab with one device asks in the singular; an empty last tab closes at once into a fresh one", async () => {
    const server = new FakeCanvas({ tabs: [{ id: TAB_1, name: "Canvas 1", layout: 1, slots: [{ device: ANDROID, present: true }] }], devices: [summary(ANDROID)] });
    let page = await started({ server });
    click(page.$("ws-menu-button"));
    click(page.$("ws-menu-close"));
    assert.equal(page.$("ws-confirm-title").textContent, "Close tab and detach 1 device?");
    page = await started({ server: new FakeCanvas() });
    click(page.$("ws-menu-button"));
    click(page.$("ws-menu-close"));
    await flush();
    assert.equal(page.$("ws-confirm").hidden, true);
    assert.equal(page.posts("/api/tabs/" + TAB_1 + "/close").length, 1);
    assert.equal(page.location.pathname, "/c/t_000000ff");
    assert.deepEqual(page.tabs().map((tab) => tab.textContent), ["Canvas 1"]);
  });

  test("the tab menu closes with Escape and links the current tab in a new window", async () => {
    const page = await started({ server: twoTabServer() });
    click(page.$("ws-menu-button"));
    assert.equal(page.document.activeElement, page.$("ws-menu-rename"));
    page.key({ key: "Escape" });
    assert.equal(page.$("ws-menu").hidden, true);
    assert.equal(page.$("ws-menu-button").getAttribute("aria-expanded"), "false");
    click(page.tabByName("Canvas 2"));
    await flush();
    assert.equal(page.$("ws-menu-open").getAttribute("href"), "/c/" + TAB_2);
  });

  test("layout buttons show the current layout, disable values below the device count and post a change", async () => {
    const page = await started({ server: twoTabServer() });
    const choices = page.document.querySelectorAll("[data-layout-choice]");
    assert.deepEqual(choices.map((item) => [item.getAttribute("aria-pressed"), item.disabled]),
      [["false", true], ["true", false], ["false", false], ["false", false]]);
    assert.equal(page.$("ws-grid").getAttribute("data-layout"), "2");
    click(choices[0]);
    assert.equal(page.posts("/api/tabs/" + TAB_1).length, 0, "a disabled layout posts nothing");
    click(choices[3]);
    await flush();
    assert.deepEqual(page.posts("/api/tabs/" + TAB_1).map((call) => call.body), [{ layout: 4 }]);
    assert.equal(page.$("ws-grid").getAttribute("data-layout"), "4");
    assert.equal(page.tiles().length, 4);
    assert.equal(page.iframes().length, 2, "the two device iframes are kept");
  });
});

describe("tiles", () => {
  test("the tile header shows name, platform, state and the stream badge, all as text", async () => {
    const server = twoTabServer();
    server.devices.get(ANDROID).name = "<img src=x onerror=alert(1)>";
    const page = await started({ server });
    const head = page.tile(0).querySelector(".tile-head");
    assert.equal(head.querySelector(".tile-name").textContent, "<img src=x onerror=alert(1)>");
    assert.equal(head.querySelector("img"), null);
    assert.equal(head.querySelector(".badge").textContent, "Android");
    assert.equal(page.tile(1).querySelector(".badge").textContent, "iOS");
    assert.equal(head.querySelector(".dot").getAttribute("data-state"), "live");
    assert.equal(head.querySelector(".sr").textContent, "Live");
    assert.equal(head.querySelector(".tile-stats").textContent, "30 fps max");
    assert.equal(page.tile(0).getAttribute("aria-label"), "Tile 1");
    for (const label of ["Tools", "Actions", "Detach"]) assert.ok(page.button(head, label), `${label} button`);
  });

  test("dot colours and alerts follow attaching, offline, failed and session errors", async () => {
    const server = twoTabServer();
    server.devices.get(ANDROID).state = "attaching";
    server.devices.get(IOS).state = "failed";
    server.devices.get(IOS).error = { error_code: "device_failed", error: "scrcpy-server exited" };
    const page = await started({ server });
    assert.equal(page.tile(0).querySelector(".dot").getAttribute("data-state"), "warn");
    assert.equal(page.tile(0).querySelector("iframe"), null, "no iframe while attaching");
    assert.equal(page.tile(0).querySelector(".tile-wait").textContent, "Attaching Pixel 8…");
    assert.equal(page.tile(1).querySelector(".dot").getAttribute("data-state"), "error");
    const alert = page.tile(1).querySelector(".tile-alert");
    assert.equal(alert.hidden, false);
    assert.equal(alert.querySelector("span").textContent, "Device failed: scrcpy-server exited");
    click(page.button(alert, "Reconnect"));
    await flush();
    assert.equal(page.posts("/api/devices/" + encodeURIComponent(IOS) + "/reconnect").length, 1);
    assert.equal(page.posts("/d/" + encodeURIComponent(IOS) + "/reconnect").length, 0);
    assert.match(page.$("ws-notice").textContent, /^Reconnecting Autonom-Fast-Test…$/);
    server.devices.get(ANDROID).state = "offline";
    server.devices.get(IOS).state = "live";
    server.devices.get(IOS).session_error = { error_code: "backend_failed", error: "mitmdump did not start" };
    await page.timers.tick(1000);
    assert.equal(page.tile(0).querySelector(".dot").getAttribute("data-state"), "warn");
    assert.ok(page.tile(0).querySelector("iframe"), "an offline device keeps its iframe");
    assert.equal(page.tile(0).querySelector(".tile-alert").querySelector("span").textContent, "Device is offline. It reconnects when the target is back.");
    assert.equal(page.tile(1).querySelector(".tile-alert").querySelector("span").textContent, "No Autonom session: mitmdump did not start");
    assert.ok(page.button(page.tile(1).querySelector(".tile-alert"), "Reconnect"));
  });

  test("the control chip pauses, resumes, takes over and releases through /d/<id>/control", async () => {
    const page = await started({ server: twoTabServer() });
    const tile = page.tile(0);
    const chip = tile.querySelector(".tile-chip");
    assert.equal(chip.textContent, "Shared");
    click(chip);
    assert.equal(tile.querySelector(".tile-pop").hidden, false);
    assert.equal(chip.getAttribute("aria-expanded"), "true");
    click(page.button(tile, "Pause input"));
    await flush();
    assert.deepEqual(page.posts("/d/android~emulator-5580/control").map((call) => call.body), [{ mode: "pause" }]);
    assert.equal(tile.querySelector(".tile-pop").hidden, true);
    assert.equal(chip.textContent, "Shared · Paused");
    click(chip);
    click(page.button(tile, "Take over"));
    await flush();
    assert.equal(chip.textContent, "People · Paused");
    click(chip);
    click(page.button(tile, "Resume input"));
    click(chip);
    click(page.button(tile, "Release"));
    await flush();
    assert.deepEqual(page.posts("/d/android~emulator-5580/control").map((call) => call.body.mode), ["pause", "takeover", "resume", "release"]);
    assert.equal(chip.textContent, "Shared");
  });

  test("Tools and Actions toggle their drawers in the tile by message; Detach posts", async () => {
    const page = await started({ server: twoTabServer() });
    const tile = page.tile(0);
    const frame = tile.querySelector("iframe");
    const tools = page.button(tile, "Tools");
    const actions = page.button(tile, "Actions");
    click(tools);
    assert.equal(tools.getAttribute("aria-pressed"), "true");
    click(actions);
    assert.equal(tools.getAttribute("aria-pressed"), "false");
    assert.equal(actions.getAttribute("aria-pressed"), "true");
    click(actions);
    const drawer = page.sent(frame).filter((message) => message.type !== "autonom:focus");
    assert.deepEqual(drawer, [
      { type: "autonom:tools", open: true },
      { type: "autonom:panel", name: "actions", open: true },
      { type: "autonom:panel", name: "actions", open: false },
    ]);
    assert.ok(frame.contentWindow.messages.every((entry) => entry.origin === "http://127.0.0.1:3277"), "messages go to the page origin only");
    click(page.button(tile, "Detach"));
    await flush();
    assert.equal(page.posts("/api/devices/android~emulator-5580/detach").length, 1);
    assert.equal(page.posts("/d/android~emulator-5580/detach").length, 0);
    assert.equal(page.tile(0).querySelector(".picker-title").textContent, "Add a device", "the slot is empty after detach");
  });

  test("autonom:state from a tile updates its badge and chip; other origins, sources and ids are ignored", async () => {
    const page = await started({ server: twoTabServer() });
    const [first, second] = page.iframes();
    const stats = () => page.tile(0).querySelector(".tile-stats").textContent;
    page.message(first, { type: "autonom:state", id: "evil", fps: 1, width: 1, height: 1 });
    page.message(first, { type: "autonom:state", id: ANDROID, fps: 9, width: 9, height: 9 }, "http://evil.test");
    page.message(null, { type: "autonom:state", id: ANDROID, fps: 9, width: 9, height: 9 });
    assert.equal(stats(), "30 fps max");
    page.message(first, { type: "autonom:state", id: ANDROID, live: true, session: "s_1", transport: "scrcpy", owner: "agent", paused: true, fps: 59.6, width: 1080, height: 2400 });
    assert.equal(stats(), "1080×2400 · 60 fps");
    assert.equal(page.tile(0).querySelector(".tile-chip").textContent, "Agent · Paused");
    page.message(second, { type: "autonom:state", id: IOS, fps: "lots", width: -3, height: 10 });
    assert.equal(page.tile(1).querySelector(".tile-stats").textContent, "30 fps max", "bad numbers are dropped");
  });
});

describe("focus and shortcuts", () => {
  test("the first device is focused on load; pointerdown and focus-request move focus and tell every tile", async () => {
    const page = await started({ server: twoTabServer() });
    const [first, second] = page.iframes();
    assert.deepEqual(page.posts("/api/focus").map((call) => call.body), [{ id: ANDROID }]);
    assert.equal(page.tile(0).getAttribute("data-focused"), "true");
    assert.equal(page.tile(1).getAttribute("data-focused"), "false");
    dispatch(page.tile(1).querySelector(".tile-head"), "pointerdown");
    await flush();
    assert.deepEqual(page.posts("/api/focus").map((call) => call.body), [{ id: ANDROID }, { id: IOS }]);
    assert.equal(page.tile(1).getAttribute("data-focused"), "true");
    assert.deepEqual(page.sent(first).at(-1), { type: "autonom:focus", focused: false });
    assert.deepEqual(page.sent(second).at(-1), { type: "autonom:focus", focused: true });
    page.message(first, { type: "autonom:focus-request", id: ANDROID });
    await flush();
    assert.equal(page.tile(0).getAttribute("data-focused"), "true");
    assert.equal(page.posts("/api/focus").length, 3);
    page.message(first, { type: "autonom:focus-request", id: ANDROID });
    assert.equal(page.posts("/api/focus").length, 3, "focusing the focused tile posts nothing");
    page.message(second, { type: "autonom:ready", id: IOS });
    assert.deepEqual(page.sent(second).at(-1), { type: "autonom:focus", focused: false });
  });

  test("Ctrl+Alt+N focuses tile N and its iframe; Ctrl+Alt+Left/Right switches tabs", async () => {
    const page = await started({ server: twoTabServer() });
    const [, second] = page.iframes();
    const event = page.key({ ctrlKey: true, altKey: true, code: "Digit2", key: "™" });
    await flush();
    assert.equal(event.defaultPrevented, true);
    assert.equal(page.tile(1).getAttribute("data-focused"), "true");
    assert.equal(page.document.activeElement, second);
    page.key({ ctrlKey: true, altKey: true, code: "Digit4", key: "4" });
    assert.equal(page.tile(1).getAttribute("data-focused"), "true", "a tile that does not exist changes nothing");
    page.key({ ctrlKey: true, altKey: true, code: "ArrowRight", key: "ArrowRight" });
    await flush();
    assert.equal(page.location.pathname, "/c/" + TAB_2);
    assert.equal(page.tabByName("Canvas 2").getAttribute("aria-selected"), "true");
    assert.deepEqual(page.posts("/api/focus").at(-1).body, { id: NETWORK }, "the new tab's first device takes focus");
    page.key({ ctrlKey: true, altKey: true, code: "ArrowRight", key: "ArrowRight" });
    await flush();
    assert.equal(page.location.pathname, "/c/" + TAB_1, "switching wraps around");
  });

  test("window focus re-claims the focused device; a shortcut to an empty tile focuses its first button", async () => {
    const server = new FakeCanvas({
      tabs: [{ id: TAB_1, name: "Canvas 1", layout: 2, slots: [{ device: ANDROID, present: true }, null] }],
      devices: [summary(ANDROID)],
      running: [{ platform: "android", target: "emulator-5584", name: "Autonom_Split_API36" }],
    });
    const page = await started({ server });
    assert.equal(page.posts("/api/focus").length, 1);
    for (const listener of page.sandbox.listeners.get("focus") || []) listener({});
    await flush();
    assert.deepEqual(page.posts("/api/focus").map((call) => call.body), [{ id: ANDROID }, { id: ANDROID }], "the last focusing window wins");
    page.key({ ctrlKey: true, altKey: true, code: "Digit2", key: "2" });
    assert.equal(page.document.activeElement, page.tile(1).querySelector("button"));
    assert.equal(page.tile(0).getAttribute("data-focused"), "true", "an empty tile does not take device focus");
  });

  test("autonom:hotkey from a focused tile runs the same shortcuts", async () => {
    const page = await started({ server: twoTabServer() });
    const [first] = page.iframes();
    page.message(first, { type: "autonom:hotkey", id: ANDROID, key: "2" });
    await flush();
    assert.equal(page.tile(1).getAttribute("data-focused"), "true");
    page.message(first, { type: "autonom:hotkey", id: ANDROID, key: "ArrowLeft" });
    await flush();
    assert.equal(page.location.pathname, "/c/" + TAB_2);
    page.message(page.iframes()[0], { type: "autonom:hotkey", id: NETWORK, key: "Delete" });
    assert.equal(page.location.pathname, "/c/" + TAB_2);
  });
});

describe("picker", () => {
  function pickerServer(extra = {}) {
    return new FakeCanvas({
      tabs: [
        { id: TAB_1, name: "Canvas 1", layout: 2, slots: [{ device: ANDROID, present: true }, null] },
        { id: TAB_2, name: "Checkout", layout: 1, slots: [{ device: IOS, present: true }] },
      ],
      devices: [summary(ANDROID), summary(IOS)],
      running: [
        { platform: "android", target: "emulator-5580", name: "Pixel 8" },
        { platform: "ios", target: "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", name: "Autonom-Fast-Test", state: "Booted" },
        { platform: "android", target: "emulator-5584", name: "Autonom_Split_API36" },
        { platform: "android", target: "R58M123", name: "R58M123", state: "unauthorized", attachable: false, reason: "Allow USB debugging on the phone" },
      ],
      bootable: [
        { kind: "avd", name: "Autonom_Split2_API36", port: 5586, udid: null, label: "Autonom_Split2_API36", running: false, target: null, job: null },
        { kind: "avd", name: "Autonom_Split_API36", port: 5584, udid: null, label: "Running one", running: true, target: "emulator-5584", job: null },
      ],
      ...extra,
    });
  }
  const rows = (page) => page.tile(1).querySelectorAll("li").map((li) => [
    li.querySelector(".picker-name").textContent,
    li.querySelector(".picker-status").hidden ? "" : li.querySelector(".picker-status").textContent,
    li.querySelector("button").hidden ? "" : li.querySelector("button").textContent,
  ]);

  test("lists running targets and bootable entries with the right action for each", async () => {
    const page = await started({ server: pickerServer() });
    assert.equal(page.gets("/api/targets").length, 1);
    assert.equal(page.tile(1).querySelector(".picker-title").textContent, "Add a device");
    assert.deepEqual(rows(page), [
      ["Pixel 8", "In this tab", ""],
      ["Autonom-Fast-Test", "In tab Checkout", "Move here"],
      ["Autonom_Split_API36", "", "Attach"],
      ["R58M123", "Allow USB debugging on the phone", ""],
      ["Autonom_Split2_API36", "", "Boot"],
    ]);
    assert.equal(page.tile(1).querySelectorAll("li")[2].querySelector(".picker-meta").textContent, "Android · emulator-5584");
    assert.equal(page.tile(1).querySelectorAll("li")[4].querySelector(".picker-meta").textContent, "Android emulator · port 5586");
    await page.timers.tick(3000);
    assert.equal(page.gets("/api/targets").length, 2, "targets are polled every 3 s while a picker shows");
  });

  test("Attach and Move here post the tab and slot of the empty tile", async () => {
    const page = await started({ server: pickerServer() });
    click(page.tile(1).querySelectorAll("li")[2].querySelector("button"));
    await flush();
    assert.deepEqual(page.posts("/api/devices").map((call) => call.body), [{ platform: "android", target: "emulator-5584", tab: TAB_1, slot: 1 }]);
    assert.equal(page.tile(1).querySelector(".tile-name").textContent, "Autonom_Split_API36");
    assert.equal(page.tile(1).querySelector("iframe").getAttribute("src"), "/d/android~emulator-5584/?embed=1");
    assert.equal(page.gets("/api/targets").length >= 2, true);
    click(page.tile(1).querySelector("button[aria-label=Detach]"));
    await flush();
    const move = page.tile(1).querySelectorAll("li").find((li) => li.querySelector("button").textContent === "Move here");
    click(move.querySelector("button"));
    await flush();
    assert.deepEqual(page.posts("/api/devices/" + encodeURIComponent(IOS) + "/move").map((call) => call.body), [{ tab: TAB_1, slot: 1 }]);
    assert.equal(page.posts("/d/" + encodeURIComponent(IOS) + "/move").length, 0);
    assert.equal(page.tile(1).querySelector(".tile-name").textContent, "Autonom-Fast-Test");
  });

  test("a slow attach does not stop polling: other changes still reach this window", async () => {
    const page = await started({ server: pickerServer() });
    let release;
    page.server.gates.set("POST /api/devices", new Promise((resolve) => { release = resolve; }));
    click(page.tile(1).querySelectorAll("li")[2].querySelector("button"));
    await flush();
    assert.equal(page.tile(1).querySelectorAll("li")[2].querySelector("button").textContent, "Attaching…");
    page.server.devices.get(ANDROID).state = "failed";
    page.server.devices.get(ANDROID).error = { error_code: "device_failed", error: "scrcpy-server exited" };
    page.server.tabs[1].name = "Renamed elsewhere";
    await page.timers.tick(1000);
    const alert = page.tile(0).querySelector(".tile-alert");
    assert.equal(alert.hidden, false, "the failed device shows its alert while the attach is pending");
    assert.ok(page.button(alert, "Reconnect"));
    assert.ok(page.tabByName("Renamed elsewhere"));
    release();
    await flush();
    await page.timers.tick(1000);
    assert.equal(page.tile(1).querySelector(".tile-name").textContent, "Autonom_Split_API36");
  });

  test("Boot posts the entry with the tile's place and shows progress until the job is done", async () => {
    const server = pickerServer();
    const page = await started({ server });
    click(page.tile(1).querySelectorAll("li")[4].querySelector("button"));
    await flush();
    assert.deepEqual(page.posts("/api/boot").map((call) => call.body), [{ kind: "avd", name: "Autonom_Split2_API36", attach: true, tab: TAB_1, slot: 1 }]);
    assert.deepEqual(rows(page).at(-1), ["Autonom_Split2_API36", "Booting… 0 s", ""]);
    server.jobs.get("b_00000001").elapsed_ms = 2400;
    await page.timers.tick(1000);
    assert.equal(page.gets("/api/boot/b_00000001").length, 1);
    assert.deepEqual(rows(page).at(-1), ["Autonom_Split2_API36", "Booting… 2 s", ""]);
    server.jobs.get("b_00000001").state = "done";
    server.tabs[0].slots[1] = { device: "android~emulator-5586", present: true };
    server.devices.set("android~emulator-5586", summary("android~emulator-5586", { name: "Autonom_Split2_API36" }));
    await page.timers.tick(1000);
    assert.equal(page.tile(1).querySelector(".tile-name").textContent, "Autonom_Split2_API36");
    const polls = page.gets("/api/boot/b_00000001").length;
    await page.timers.tick(5000);
    assert.equal(page.gets("/api/boot/b_00000001").length, polls, "a finished job is not polled");
  });

  test("a finished job named by Bootable.job is polled once and never re-added", async () => {
    const server = pickerServer();
    server.jobs.set("b_0000000a", { id: "b_0000000a", kind: "avd", name: "Autonom_Split2_API36", udid: null, state: "done", elapsed_ms: 9000 });
    server.bootable[0].job = "b_0000000a";
    const page = await started({ server });
    await page.timers.tick(10000);
    assert.equal(page.gets("/api/boot/b_0000000a").length, 1);
    assert.deepEqual(rows(page).at(-1), ["Autonom_Split2_API36", "", "Boot"]);
  });

  test("a Bootable.job object keeps its real state: done is not polled, booting shows its progress", async () => {
    const done = pickerServer();
    done.bootable[0].job = { id: "b_0000000b", kind: "avd", name: "Autonom_Split2_API36", udid: null, state: "done", elapsed_ms: 9000 };
    const page = await started({ server: done });
    await page.timers.tick(5000);
    assert.equal(page.gets("/api/boot/b_0000000b").length, 0);
    assert.deepEqual(rows(page).at(-1), ["Autonom_Split2_API36", "", "Boot"]);
    const booting = pickerServer();
    const job = { id: "b_0000000c", kind: "avd", name: "Autonom_Split2_API36", udid: null, state: "booting", elapsed_ms: 3100 };
    booting.jobs.set(job.id, { ...job });
    booting.bootable[0].job = job;
    const other = await started({ server: booting });
    assert.deepEqual(rows(other).at(-1), ["Autonom_Split2_API36", "Booting… 3 s", ""]);
    await other.timers.tick(1000);
    assert.equal(other.gets("/api/boot/b_0000000c").length, 1);
  });

  test("a network error while polling a job keeps polling it; a 404 ends it", async () => {
    const server = pickerServer();
    const page = await started({ server });
    click(page.tile(1).querySelectorAll("li")[4].querySelector("button"));
    await flush();
    let down = true;
    server.overrides.push((method, path) => (down && path === "/api/boot/b_00000001" ? { status: 502, body: { ok: false, error: "Bad gateway", error_code: "backend_failed", hint: null } } : null));
    await page.timers.tick(3000);
    assert.equal(page.gets("/api/boot/b_00000001").length, 3);
    assert.deepEqual(rows(page).at(-1), ["Autonom_Split2_API36", "Booting… 0 s", ""]);
    down = false;
    server.jobs.delete("b_00000001");
    await page.timers.tick(1000);
    assert.deepEqual(rows(page).at(-1), ["Autonom_Split2_API36", "Boot failed", "Boot"]);
    await page.timers.tick(3000);
    assert.equal(page.gets("/api/boot/b_00000001").length, 4);
  });

  test("a failed boot says so and offers Boot again", async () => {
    const server = pickerServer();
    const page = await started({ server });
    click(page.tile(1).querySelectorAll("li")[4].querySelector("button"));
    await flush();
    Object.assign(server.jobs.get("b_00000001"), { state: "failed", error_code: "boot_timeout", error: "The AVD did not boot in 180 s" });
    await page.timers.tick(1000);
    assert.equal(page.$("ws-notice").textContent, "Boot failed: The AVD did not boot in 180 s");
    assert.deepEqual(rows(page).at(-1), ["Autonom_Split2_API36", "Boot failed", "Boot"]);
  });

  test("at max_devices Attach and Boot are disabled with the reason, Move here stays", async () => {
    const page = await started({ server: pickerServer({ limits: { max_devices: 2 } }) });
    const items = page.tile(1).querySelectorAll("li");
    assert.equal(items[1].querySelector("button").disabled, false, "Move here keeps the device count");
    assert.equal(items[2].querySelector("button").disabled, true);
    assert.equal(items[4].querySelector("button").disabled, true);
    const note = page.tile(1).querySelector(".picker-note");
    assert.equal(note.hidden, false);
    assert.equal(note.textContent, "2 devices are attached, the most one Canvas holds. Detach one to add another.");
    assert.equal(items[2].querySelector("button").title, note.textContent);
    click(items[2].querySelector("button"));
    assert.equal(page.posts("/api/devices").length, 0);
  });

  test("no running devices: the picker says how to start one; without a picker targets are not polled", async () => {
    const page = await started({ server: new FakeCanvas() });
    assert.equal(page.tile(0).querySelector(".picker-note").textContent, "No devices are running. Start an emulator or a Simulator and it shows up here.");
    const full = await started({ server: twoTabServer() });
    await full.timers.tick(6000);
    assert.equal(full.gets("/api/targets").length, 0);
  });

  test("API errors show the error and its hint", async () => {
    const server = pickerServer();
    server.overrides.push((method, path) => (method === "POST" && path === "/api/devices"
      ? { status: 409, body: { ok: false, error: "Slot 2 of Canvas 1 is taken", error_code: "slot_taken", hint: "Pick an empty tile" } } : null));
    const page = await started({ server });
    click(page.tile(1).querySelectorAll("li")[2].querySelector("button"));
    await flush();
    assert.equal(page.$("ws-notice").textContent, "Slot 2 of Canvas 1 is taken — Pick an empty tile");
    assert.equal(page.$("ws-notice").getAttribute("data-kind"), "error");
    assert.equal(page.tile(1).querySelectorAll("li")[2].querySelector("button").textContent, "Attach", "the button recovers");
  });
});

describe("absent devices", () => {
  test("an absent tile names the device, boots it when bootable and removes it from the slot", async () => {
    const absentId = "android~emulator-5586";
    const server = new FakeCanvas({
      tabs: [{ id: TAB_1, name: "Canvas 1", layout: 2, slots: [{ device: absentId, present: false }, { device: IOS, present: false }] }],
      absent: [
        { id: absentId, platform: "android", target: "emulator-5586", name: "Autonom_Split2_API36", tab: TAB_1, slot: 0,
          bootable: { kind: "avd", name: "Autonom_Split2_API36", port: 5586, udid: null, label: "Autonom_Split2_API36", running: false, target: null, job: null } },
        { id: IOS, platform: "ios", target: "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", name: "Autonom-Split-Test", tab: TAB_1, slot: 1, bootable: null },
      ],
    });
    const page = await started({ server });
    assert.equal(page.$("ws-empty").hidden, true, "placed devices count as content");
    assert.equal(page.tile(0).querySelector(".absent-title").textContent, "Autonom_Split2_API36 is not running");
    assert.equal(page.tile(0).querySelector("iframe"), null);
    assert.equal(page.button(page.tile(1), "Boot").hidden, true, "no Boot without a bootable entry");
    click(page.button(page.tile(0), "Boot"));
    await flush();
    // The job names the absent tile's tab (not a slot), so it attaches where the device is placed
    // even when another window made a different tab the active one.
    assert.deepEqual(page.posts("/api/boot").map((call) => call.body), [{ kind: "avd", name: "Autonom_Split2_API36", attach: true, tab: TAB_1 }]);
    assert.equal(page.tile(0).querySelector(".absent-status").textContent, "Booting… 0 s");
    assert.equal(page.button(page.tile(0), "Boot").hidden, true);
    click(page.button(page.tile(1), "Remove"));
    await flush();
    assert.equal(page.posts("/api/tabs/" + TAB_1 + "/slots/1/clear").length, 1);
    assert.equal(page.tile(1).querySelector(".picker-title").textContent, "Add a device");
  });
});
