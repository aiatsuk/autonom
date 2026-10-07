// Unit tests of the Canvas Tools drawer page module (contract section 3): markup, styles and
// the script run in node:vm against a small fake DOM built from the real markup, with stubs
// for the page globals and a fake fetch. No browser, Canvas, device or network is involved.
import assert from "node:assert/strict";
import test, { describe } from "node:test";
import { Script, createContext } from "node:vm";

import {
  TOOLS_TABS,
  toolsButton,
  toolsMarkup,
  toolsScript,
  toolsStyles,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-tools-page.mjs";
import { LogFeed } from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-tools-server.mjs";

// --- a minimal DOM: enough of the API the drawer script uses ----------------------------------

const VOID = new Set(["input", "br", "hr", "img", "meta", "link"]);
const ENTITIES = { "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" };
const decode = (value) => value.replace(/&(amp|lt|gt|quot|#39);/g, (entity) => ENTITIES[entity]);

class FakeText {
  constructor(data) { this.nodeType = 3; this.data = data; this.parentNode = null; }
  get textContent() { return this.data; }
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
    this.scrollTop = 0;
    this.clientHeight = 160;
    this.state = {};
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
  get placeholder() { return this.getAttribute("placeholder") || ""; }
  set placeholder(value) { this.setAttribute("placeholder", value); }
  get checked() { return "checked" in this.state ? this.state.checked : this.hasAttribute("checked"); }
  set checked(value) { this.state.checked = Boolean(value); }
  get value() {
    if ("value" in this.state) return this.state.value;
    if (this.localName === "textarea") return this.textContent;
    if (this.localName === "select") {
      const options = this.querySelectorAll("option");
      const chosen = options.find((option) => option.hasAttribute("selected")) || options[0];
      return chosen ? chosen.getAttribute("value") ?? chosen.textContent : "";
    }
    return this.getAttribute("value") || "";
  }
  set value(value) { this.state.value = String(value); }
  get classList() {
    const element = this;
    const read = () => element.className.split(/\s+/).filter(Boolean);
    const write = (names) => element.setAttribute("class", names.join(" "));
    return {
      contains: (name) => read().includes(name),
      add: (name) => { if (!read().includes(name)) write([...read(), name]); },
      remove: (name) => write(read().filter((item) => item !== name)),
      toggle: (name, force) => {
        const on = force === undefined ? !read().includes(name) : Boolean(force);
        if (on) { if (!read().includes(name)) write([...read(), name]); } else write(read().filter((item) => item !== name));
        return on;
      },
    };
  }
  get children() { return this.childNodes.filter((node) => node.nodeType === 1); }
  get firstChild() { return this.childNodes[0] || null; }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; }
  get scrollHeight() { return this.children.length * 16; }
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
  replaceChildren(...nodes) {
    for (const node of this.childNodes) node.parentNode = null;
    this.childNodes = [];
    for (const node of nodes) this.appendChild(node);
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
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
  focus() { this.ownerDocument.activeElement = this; }
  // innerHTML is deliberately absent: a script that relied on it would fail loudly here.
  set innerHTML(_) { throw new Error("innerHTML is not allowed in the Tools drawer"); }
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
  }
  createElement(tag) { return new FakeElement(this, tag); }
  getElementById(id) { return this.documentElement.descendants().find((element) => element.id === id) || null; }
  querySelectorAll(selector) { return this.documentElement.querySelectorAll(selector); }
  querySelector(selector) { return this.documentElement.querySelector(selector); }
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

function fire(target, type, properties = {}) {
  let stopped = false;
  const event = {
    type, target, defaultPrevented: false, detail: 1, ...properties,
    preventDefault() { this.defaultPrevented = true; },
    stopPropagation() { stopped = true; },
  };
  for (let node = target; node && !stopped; node = node.parentNode) {
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
  return fire(target, "click");
}

const flush = async () => { for (let i = 0; i < 20; i += 1) await new Promise((resolve) => setImmediate(resolve)); };

// --- fake timers and fetch ------------------------------------------------------------------

function fakeTimers() {
  const timers = new Map();
  let now = 0;
  let next = 1;
  const add = (fn, ms, every) => { const id = next++; timers.set(id, { fn, at: now + Math.max(0, ms || 0), every }); return id; };
  return {
    setInterval: (fn, ms) => add(fn, ms, Math.max(1, ms || 1)),
    setTimeout: (fn, ms) => add(fn, ms, 0),
    clear: (id) => { timers.delete(id); },
    count: () => timers.size,
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

const ANDROID_CONTEXT = {
  platform: "android", target_id: "emulator-5580", emulator: true,
  session: { id: "s1", app_id: "com.example.app" }, app_id: "com.example.app",
  privacy_services: null, android_permission_aliases: { camera: ["android.permission.CAMERA"] },
  simulate: { biometric: ["match"], battery: ["set", "reset"], network: ["online", "offline"], appearance: ["light", "dark"] },
  location_readable: true, location_clearable: false, network_available: true, permissions_readable: true,
};

const IOS_CONTEXT = {
  platform: "ios", target_id: "3760A5A8-E59D-4AE6-B1AF-A626908D7B61", emulator: true,
  session: null, app_id: "com.example.App", privacy_services: ["camera", "photos"], android_permission_aliases: null,
  simulate: { push: ["send"], biometric: ["enroll", "unenroll", "match", "nonmatch"], battery: ["set", "reset"], appearance: ["light", "dark"] },
  location_readable: false, location_clearable: true, network_available: false, permissions_readable: false,
};

function ok(result) { return { status: 200, body: { ok: true, result } }; }
function refused(status, error_code, error, hint = null) {
  return { status, body: { ok: false, error, error_code, hint, capability: null } };
}

function defaultReply(platform, op, payload) {
  switch (op) {
    case "context": return ok(platform === "ios" ? IOS_CONTEXT : ANDROID_CONTEXT);
    case "permissions.list":
      return platform === "ios"
        ? ok({ app_id: payload.app_id, readable: false, permissions: [{ name: "camera", alias: null, granted: null }, { name: "photos", alias: null, granted: null }] })
        : ok({ app_id: payload.app_id, readable: true, permissions: [
          { name: "android.permission.CAMERA", alias: "camera", granted: true },
          { name: "android.permission.RECORD_AUDIO", alias: "microphone", granted: false },
        ] });
    case "network.status": return ok({ proxy: { running: false, port: null }, attached: false, recent_flow_count: 0 });
    case "network.requests": return ok({ requests: [], count: 0, total_matched: 0, truncated: false });
    case "mocks.list": return ok({ mocks: [], registry: "/tmp/registry.json" });
    default: return ok({});
  }
}

function boot({ platform = "android", reply = () => null, canSend = () => true, view = { owner: "shared", paused: false, status: {} } } = {}) {
  const html = `<body>${toolsButton()}<button type="button" class="icon" id="inspector-toggle" aria-pressed="true" aria-controls="inspector">I</button>` +
    `<aside class="side" id="inspector" aria-label="Inspector"></aside>${toolsMarkup({ platform })}</body>`;
  const document = parseMarkup(html);
  document.body = document.documentElement.children[0];
  const timers = fakeTimers();
  const calls = [];
  const fetch = async (path, init = {}) => {
    const body = init.body ? JSON.parse(init.body) : null;
    const entry = { path, method: init.method || "GET", headers: init.headers || {}, body, op: body && body.op };
    calls.push(entry);
    const answer = (await reply(entry)) || (path === "/tools/call" ? defaultReply(platform, body.op, body.payload) : { status: 200, body: { ok: true, running: true, lines: [], next: 0, dropped: 0, error: null } });
    return { ok: answer.status < 400, status: answer.status, statusText: "", json: async () => answer.body };
  };
  const sandbox = {
    document, fetch, view, csrf: "csrf-123", TextEncoder, console,
    $: (id) => document.getElementById(id),
    url: (path) => path,
    note: () => {},
    canSendInput: () => canSend(),
    setInterval: timers.setInterval, clearInterval: timers.clear, setTimeout: timers.setTimeout, clearTimeout: timers.clear,
  };
  sandbox.window = sandbox;
  const context = createContext(sandbox);
  new Script(toolsScript(), { filename: "tools-script.js" }).runInContext(context);
  const $ = (id) => document.getElementById(id);
  const ops = (op) => calls.filter((call) => call.op === op);
  return { document, $, calls, ops, timers, sandbox };
}

// --- markup ---------------------------------------------------------------------------------

describe("toolsButton and toolsMarkup", () => {
  test("the toggle names the drawer it controls and starts unpressed", () => {
    const button = parseMarkup(toolsButton()).getElementById("tools-toggle");
    assert.equal(button.localName, "button");
    assert.equal(button.getAttribute("type"), "button");
    assert.equal(button.getAttribute("aria-controls"), "tools");
    assert.equal(button.getAttribute("aria-pressed"), "false");
    assert.equal(button.getAttribute("aria-label"), "Tools");
    assert.equal(button.querySelector("svg").getAttribute("aria-hidden"), "true");
  });

  for (const platform of ["android", "ios"]) {
    test(`${platform}: tablist, tabs and tabpanels carry their roles and links`, () => {
      const document = parseMarkup(toolsMarkup({ platform }));
      const aside = document.getElementById("tools");
      assert.equal(aside.localName, "aside");
      assert.ok(aside.hidden, "the drawer starts hidden");
      assert.equal(aside.getAttribute("aria-label"), "Tools");
      assert.equal(aside.getAttribute("data-platform"), platform);
      const tablist = document.querySelectorAll("[role=tablist]");
      assert.equal(tablist.length, 1);
      assert.ok(tablist[0].getAttribute("aria-label"));
      const tabs = document.querySelectorAll("[role=tab]");
      assert.deepEqual(tabs.map((tab) => tab.textContent), ["App", "Simulate", "Network", "Mocks", "Logs"]);
      assert.deepEqual(TOOLS_TABS.map(({ label }) => label), ["App", "Simulate", "Network", "Mocks", "Logs"]);
      const panels = document.querySelectorAll("[role=tabpanel]");
      assert.equal(panels.length, 5);
      tabs.forEach((tab, index) => {
        assert.equal(tab.parentNode, tablist[0]);
        const panel = document.getElementById(tab.getAttribute("aria-controls"));
        assert.ok(panel, `${tab.id} controls an existing panel`);
        assert.equal(panel.getAttribute("role"), "tabpanel");
        assert.equal(panel.getAttribute("aria-labelledby"), tab.id);
        assert.equal(tab.getAttribute("aria-selected"), String(index === 0));
        assert.equal(tab.getAttribute("tabindex"), index === 0 ? "0" : "-1");
        assert.equal(panel.hidden, index !== 0);
      });
    });

    test(`${platform}: every field has a label and every icon button a name`, () => {
      const document = parseMarkup(toolsMarkup({ platform }));
      const labelled = new Set(document.querySelectorAll("label").map((label) => label.getAttribute("for")).filter(Boolean));
      for (const field of document.querySelectorAll("input,select,textarea")) {
        const wrapped = field.closest("label");
        assert.ok(labelled.has(field.id) || wrapped || field.getAttribute("aria-label"), `${field.id} has a label`);
      }
      for (const button of document.querySelectorAll("button")) {
        assert.equal(button.getAttribute("type") === "button" || button.getAttribute("type") === "submit", true, `${button.id} has a type`);
        assert.ok(button.textContent.trim() || button.getAttribute("aria-label"), `${button.id || button.className} has a name`);
      }
      for (const dialog of document.querySelectorAll("[role=alertdialog]")) {
        assert.ok(document.getElementById(dialog.getAttribute("aria-labelledby")));
        assert.ok(document.getElementById(dialog.getAttribute("aria-describedby")));
      }
      assert.equal(document.querySelectorAll("[role=log]").length, 1);
    });
  }

  test("iOS shows the permissions note, push, Clear and no read-back or network simulation", () => {
    const document = parseMarkup(toolsMarkup({ platform: "ios" }));
    assert.match(document.getElementById("tools-perms-note").textContent, /cannot report whether a permission is granted/);
    assert.ok(document.querySelector("[data-sim-group=push]"));
    assert.ok(document.querySelector("[data-sim=push]"));
    assert.ok(document.getElementById("tools-loc-clear"));
    assert.equal(document.getElementById("tools-loc-get"), null);
    assert.equal(document.querySelector("[data-sim-group=network]"), null);
    assert.ok(document.getElementById("tools-battery-state"));
    assert.deepEqual(document.querySelectorAll("[data-sim=biometric]").map((button) => button.getAttribute("data-action")),
      ["match", "nonmatch", "enroll", "unenroll"]);
    assert.match(document.getElementById("tools-panel-app").textContent, /cannot report its location/);
  });

  test("Android hides push and Clear, and shows read-back and network simulation", () => {
    const document = parseMarkup(toolsMarkup({ platform: "android" }));
    assert.equal(document.getElementById("tools-perms-note"), null);
    assert.equal(document.querySelector("[data-sim=push]"), null);
    assert.equal(document.getElementById("tools-push-payload"), null);
    assert.equal(document.getElementById("tools-loc-clear"), null);
    assert.ok(document.getElementById("tools-loc-get"));
    assert.ok(document.querySelector("[data-sim-group=network]"));
    assert.equal(document.getElementById("tools-battery-state"), null);
    assert.deepEqual(document.querySelectorAll("[data-sim=biometric]").map((button) => button.getAttribute("data-action")), ["match"]);
  });

  test("the platform also comes from context.options and defaults to Android", () => {
    assert.match(toolsMarkup({ options: { platform: "ios" } }), /data-platform="ios"/);
    assert.match(toolsMarkup({}), /data-platform="android"/);
    assert.match(toolsMarkup(), /data-platform="android"/);
  });

  test("the mocks note and the network confirmation name their effect", () => {
    const document = parseMarkup(toolsMarkup({ platform: "android" }));
    assert.equal(document.getElementById("tools-mocks-note").textContent, "Mocks apply to every Autonom session on this Mac.");
    assert.match(document.getElementById("tools-net-confirm-text").textContent, /decrypts and records this session's HTTP\(S\) traffic/);
    assert.ok(document.getElementById("tools-net-confirm").hidden);
    assert.ok(document.getElementById("tools-mock-confirm").hidden);
  });

  test("the markup holds no script, event handler attribute or data from the context", () => {
    const markup = toolsMarkup({ platform: "android", app_id: "<img src=x onerror=alert(1)>", target_id: "evil\"" });
    assert.doesNotMatch(markup, /<script|\son[a-z]+=|onerror|evil/i);
  });
});

// --- script and styles as text ---------------------------------------------------------------

describe("toolsScript and toolsStyles as text", () => {
  const script = toolsScript();

  test("the script parses, has no backticks and never writes HTML", () => {
    assert.doesNotThrow(() => new Script(script));
    assert.equal(script.includes("`"), false);
    assert.doesNotMatch(script, /innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
    assert.equal(/<\/script/i.test(script), false, "the script cannot end its own script element");
  });

  test("the script still parses inside a template literal the way renderPage inlines it", () => {
    const page = new Function("toolsScript", "return `<script>\n${toolsScript()}\n</script>`;")(toolsScript);
    const inner = page.match(/<script>([\s\S]*)<\/script>/)[1];
    assert.doesNotThrow(() => new Script(inner));
    assert.equal(inner.trim(), script.trim());
  });

  test("the styles use the page tokens, have dark-mode rules and a phone sheet from the page's 760 px breakpoint", () => {
    const css = toolsStyles();
    assert.match(css, /@media \(prefers-color-scheme:dark\)\{/);
    // The page switches to its one-column layout (and hides the inspector toggle) at 760 px, so
    // the sheet starts at the same width: no band between 721 and 760 px where the drawer would
    // render in flow below the stage. This also covers the contract's 720 px phone range.
    const phone = css.indexOf("@media (max-width:760px){");
    assert.ok(phone >= 0, "a 760 px phone block");
    const block = css.slice(phone, css.indexOf("\n}\n", phone));
    assert.match(block, /\.tools\{position:fixed;left:0;right:0;bottom:0;/, "the drawer is a fixed full-width sheet in that block");
    assert.match(block, /\.actions \.icon\.tools-toggle\{display:grid/, "the toggle stays visible in the same block");
    assert.match(block, /body\.tools-open\{overflow:hidden\}/);
    assert.doesNotMatch(css, /max-width:7[0-5][0-9]px/, "no second, narrower sheet breakpoint");
    for (const token of ["--surface", "--raised", "--text", "--text-2", "--line", "--fill", "--accent", "--r-sm", "--mono"]) {
      assert.ok(css.includes(`var(${token})`), `uses ${token}`);
    }
    assert.match(css, /body\.tools-open aside\.side\{display:none\}/, "the inspector hides while the drawer is open");
    assert.match(css, /clamp\(440px,34vw,520px\)/, "desktop drawer width between 440 and 520 px");
    assert.match(css, /\.actions \.icon\.tools-toggle\{display:grid/, "the toggle stays visible at phone width");
    const opened = (css.match(/\{/g) || []).length;
    const closed = (css.match(/\}/g) || []).length;
    assert.equal(opened, closed, "braces balance");
    assert.doesNotMatch(css, /`/);
  });
});

// --- the script in a page context --------------------------------------------------------------

describe("toolsScript in the page context", () => {
  test("opening the drawer requests context once and fills the app id", async () => {
    const page = boot({ platform: "android" });
    const { $, ops } = page;
    assert.ok($("tools").hidden);
    assert.equal(page.calls.length, 0, "nothing is requested before the drawer opens");
    click($("tools-toggle"));
    await flush();
    assert.equal($("tools").hidden, false);
    assert.equal($("tools-toggle").getAttribute("aria-pressed"), "true");
    assert.ok(page.document.body.classList.contains("tools-open"));
    assert.equal($("inspector-toggle").getAttribute("aria-pressed"), "false");
    assert.equal(ops("context").length, 1);
    const [contextCall] = ops("context");
    assert.equal(contextCall.method, "POST");
    assert.equal(contextCall.path, "/tools/call");
    assert.deepEqual(contextCall.body, { op: "context", payload: {} });
    assert.equal(contextCall.headers["X-Autonom-CSRF"], "csrf-123");
    assert.equal(contextCall.headers["X-Autonom-Origin"], "human");
    assert.equal(contextCall.headers["Content-Type"], "application/json");
    assert.equal($("tools-app-id").value, "com.example.app");
    assert.match($("tools-app-source").textContent, /session/);
    assert.deepEqual(ops("permissions.list").map((call) => call.body.payload), [{ app_id: "com.example.app" }]);
    const rows = $("tools-perms").children;
    assert.equal(rows.length, 2);
    assert.match(rows[0].textContent, /camera/);
    assert.match(rows[0].textContent, /Granted/);
    assert.match(rows[1].textContent, /Denied/);

    click($("tools-close"));
    await flush();
    assert.ok($("tools").hidden);
    assert.equal($("tools-toggle").getAttribute("aria-pressed"), "false");
    assert.equal(page.document.activeElement, $("tools-toggle"), "focus returns to the toggle");
    click($("tools-toggle"));
    await flush();
    assert.equal(ops("context").length, 1, "the context is fetched only on the first open");
  });

  test("a control_refused reply renders its message, code and hint as text", async () => {
    const page = boot({
      platform: "android",
      reply: ({ op }) => op === "permissions.set"
        ? refused(403, "control_refused", "an agent controls the device", "Take control in the Canvas first.")
        : null,
    });
    const { $, ops } = page;
    click($("tools-toggle"));
    await flush();
    const revoke = $("tools-perms").querySelector("[data-perm-action=revoke]");
    assert.equal(revoke.getAttribute("data-perm"), "android.permission.CAMERA");
    click(revoke);
    await flush();
    assert.deepEqual(ops("permissions.set").map((call) => call.body.payload),
      [{ app_id: "com.example.app", service: "android.permission.CAMERA", action: "revoke" }]);
    const box = $("tools-msg-app");
    assert.equal(box.hidden, false);
    assert.equal(box.getAttribute("data-kind"), "error");
    assert.match(box.textContent, /an agent controls the device \(control_refused\)/);
    assert.match(box.textContent, /Take control in the Canvas first\./);
    assert.equal(revoke.disabled, false, "the control is usable again after the refusal");
  });

  test("a timed-out change says the outcome is unknown and re-reads the state it may have changed", async () => {
    const timeout = () => ({ status: 504, body: { ok: false, error: "the tools call took longer than 60 s", error_code: "timeout" } });
    let enabled = true;
    let mocksListFails = false;
    const page = boot({
      platform: "android",
      reply: ({ op }) => {
        if (op === "mocks.list") {
          if (mocksListFails) return refused(502, "tools_unavailable", "the tools process is gone", null);
          return ok({ mocks: [{ id: "1", match: { url_glob: "*/a*" }, response: { status: 200 }, enabled, hits: 0 }] });
        }
        if (op === "mocks.disable") { enabled = false; return timeout(); }
        if (op === "mocks.enable") return timeout();
        if (op === "permissions.set" || op === "network.start" || op === "simulate") return timeout();
        return null;
      },
    });
    const { $, ops } = page;
    click($("tools-toggle"));
    await flush();

    // Mocks: the disable was applied before the timeout; the re-read shows it.
    click($("tools-tab-mocks"));
    await flush();
    const listsBefore = ops("mocks.list").length;
    click($("tools-mocks").querySelector("[data-mock-action=disable]"));
    await flush();
    assert.equal(ops("mocks.disable").length, 1, "the change is sent once and never retried");
    assert.equal(ops("mocks.list").length, listsBefore + 1, "mocks.list is read again");
    const mocksBox = $("tools-msg-mocks");
    assert.equal(mocksBox.getAttribute("data-kind"), "error");
    assert.match(mocksBox.textContent, /outcome is unknown/);
    assert.match(mocksBox.textContent, /\(timeout\)/);
    assert.match(mocksBox.textContent, /current state was re-read/);
    const toggle = $("tools-mocks").querySelector("[data-mock=\"1\"].tools-switch");
    assert.equal(toggle.textContent, "Off", "the switch follows the re-read state, not the stale one");
    assert.equal(toggle.disabled, false);

    // A re-read that fails too is named in the hint, and the outcome is still unknown.
    mocksListFails = true;
    click($("tools-mocks").querySelector("[data-mock-action=enable]"));
    await flush();
    assert.equal(ops("mocks.enable").length, 1);
    assert.match(mocksBox.textContent, /outcome is unknown/);
    assert.match(mocksBox.textContent, /Re-reading the current state also failed: the tools process is gone \(tools_unavailable\)/);

    // Permissions: permissions.list is read again for the loaded app.
    const permsBefore = ops("permissions.list").length;
    click($("tools-perms").querySelector("[data-perm-action=revoke]"));
    await flush();
    assert.equal(ops("permissions.set").length, 1);
    assert.deepEqual(ops("permissions.list").slice(permsBefore).map((call) => call.body.payload), [{ app_id: "com.example.app" }]);
    assert.match($("tools-msg-app").textContent, /outcome is unknown/);
    assert.equal($("tools-perms").children.length, 2);

    // Network: network.status is read again after a timed-out start.
    click($("tools-tab-network"));
    await flush();
    const statusBefore = ops("network.status").length;
    click($("tools-net-start"));
    click($("tools-net-confirm-go"));
    await flush();
    assert.equal(ops("network.start").length, 1);
    assert.equal(ops("network.status").length, statusBefore + 1, "network.status is read again");
    assert.match($("tools-msg-network").textContent, /outcome is unknown/);

    // Simulate has nothing to re-read, so the hint says to check the device.
    click($("tools-tab-simulate"));
    await flush();
    click(page.document.querySelector("[data-sim=appearance][data-action=dark]"));
    await flush();
    assert.equal(ops("simulate").length, 1);
    assert.match($("tools-msg-simulate").textContent, /outcome is unknown/);
    assert.match($("tools-msg-simulate").textContent, /check the device before trying again/);
  });

  test("hostile names render as text, never as markup", async () => {
    const hostile = "<img src=x onerror=alert(1)>";
    const page = boot({
      platform: "android",
      reply: ({ op }) => op === "permissions.list"
        ? ok({ readable: true, permissions: [{ name: hostile, alias: null, granted: true }] })
        : op === "permissions.set" ? refused(404, "app_not_installed", hostile, hostile) : null,
    });
    const { $ } = page;
    click($("tools-toggle"));
    await flush();
    assert.match($("tools-perms").textContent, /<img src=x onerror=alert\(1\)>/);
    click($("tools-perms").querySelector("[data-perm-action=grant]"));
    await flush();
    assert.match($("tools-msg-app").textContent, /<img src=x/);
    assert.equal(page.document.querySelectorAll("img").length, 0);
  });

  test("device changes are disabled with the reason while the page cannot send input", async () => {
    let canSend = false;
    const view = { owner: "agent", paused: false, status: {} };
    const page = boot({ platform: "android", canSend: () => canSend, view });
    const { $ } = page;
    click($("tools-toggle"));
    await flush();
    assert.equal($("tools-lock").hidden, false);
    assert.match($("tools-lock").textContent, /An agent has control/);
    const mutating = page.document.querySelectorAll("[data-mutate]");
    assert.ok(mutating.length > 10);
    assert.ok(mutating.every((control) => control.disabled), "every mutating control is disabled");
    assert.equal($("tools-loc-get").disabled, false, "reads stay available");
    assert.equal($("tools-perms-load").disabled, false);
    click($("tools-perms").querySelector("[data-perm-action=revoke]"));
    await flush();
    assert.equal(page.ops("permissions.set").length, 0);

    view.paused = true;
    await page.timers.tick(500);
    assert.match($("tools-lock").textContent, /Input is paused/);
    canSend = true;
    view.paused = false;
    await page.timers.tick(500);
    assert.ok($("tools-lock").hidden);
    assert.ok(page.document.querySelectorAll("[data-mutate]").every((control) => !control.disabled));
  });

  test("tabs move with the arrow keys, Home and End; Escape closes the drawer", async () => {
    const page = boot({ platform: "ios" });
    const { $ } = page;
    click($("tools-toggle"));
    await flush();
    assert.equal(page.document.activeElement, $("tools-tab-app"), "opening focuses the selected tab");
    const key = (name) => fire(page.document.activeElement, "keydown", { key: name });
    const selected = () => page.document.querySelectorAll("[role=tab]").filter((tab) => tab.getAttribute("aria-selected") === "true").map((tab) => tab.id);
    assert.equal(key("ArrowRight").defaultPrevented, true);
    assert.deepEqual(selected(), ["tools-tab-simulate"]);
    assert.equal(page.document.activeElement, $("tools-tab-simulate"));
    assert.equal($("tools-panel-simulate").hidden, false);
    assert.equal($("tools-panel-app").hidden, true);
    assert.equal($("tools-tab-simulate").getAttribute("tabindex"), "0");
    assert.equal($("tools-tab-app").getAttribute("tabindex"), "-1");
    key("End");
    assert.deepEqual(selected(), ["tools-tab-logs"]);
    key("Home");
    assert.deepEqual(selected(), ["tools-tab-app"]);
    key("ArrowLeft");
    assert.deepEqual(selected(), ["tools-tab-logs"], "arrows wrap around");
    click($("tools-tab-mocks"));
    assert.deepEqual(selected(), ["tools-tab-mocks"]);
    fire($("tools-mock-glob"), "keydown", { key: "Escape" });
    await flush();
    assert.ok($("tools").hidden);
    assert.equal(page.document.activeElement, $("tools-toggle"));
  });

  test("the inspector toggle closes the drawer: they are mutually exclusive", async () => {
    const page = boot({ platform: "android" });
    const { $ } = page;
    page.document.body.classList.add("no-inspector");
    click($("tools-toggle"));
    await flush();
    click($("inspector-toggle"));
    assert.ok($("tools").hidden);
    assert.equal(page.document.body.classList.contains("tools-open"), false);
    assert.equal(page.document.body.classList.contains("no-inspector"), false);
    assert.equal($("inspector-toggle").getAttribute("aria-pressed"), "true");
  });

  test("Simulate shows only what context.simulate lists and sends the chosen action", async () => {
    const android = boot({ platform: "android", reply: ({ op }) => op === "simulate" ? ok({ control: "appearance", value: "dark", verified: true, verification: "read_back" }) : null });
    click(android.$("tools-toggle"));
    click(android.$("tools-tab-simulate"));
    await flush();
    const visible = (page) => page.document.querySelectorAll("[data-sim]").filter((button) => !button.hidden && !button.closest("[hidden]"))
      .map((button) => `${button.getAttribute("data-sim")}:${button.getAttribute("data-action")}`);
    assert.deepEqual(visible(android), ["biometric:match", "battery:set", "battery:reset", "network:online", "network:offline", "appearance:light", "appearance:dark"]);
    click(android.document.querySelector("[data-sim=appearance][data-action=dark]"));
    await flush();
    assert.deepEqual(android.ops("simulate").map((call) => call.body.payload), [{ control: "appearance", action: "dark", values: {} }]);
    assert.match(android.$("tools-msg-simulate").textContent, /confirmed on the device/);

    const ios = boot({ platform: "ios", reply: ({ op }) => op === "simulate" ? refused(409, "unsupported_capability", "push needs a booted Simulator", "Boot it first.") : null });
    click(ios.$("tools-toggle"));
    click(ios.$("tools-tab-simulate"));
    await flush();
    assert.deepEqual(visible(ios), ["push:send", "biometric:match", "biometric:nonmatch", "biometric:enroll", "biometric:unenroll",
      "battery:set", "battery:reset", "appearance:light", "appearance:dark"]);
    click(ios.document.querySelector("[data-sim=push]"));
    await flush();
    const [push] = ios.ops("simulate");
    assert.equal(push.body.payload.control, "push");
    assert.equal(push.body.payload.values.app_id, "com.example.App");
    assert.equal(typeof push.body.payload.values.payload, "object");
    assert.match(ios.$("tools-msg-simulate").textContent, /push needs a booted Simulator \(unsupported_capability\)Boot it first\./);

    ios.$("tools-push-payload").value = "[1,2]";
    click(ios.document.querySelector("[data-sim=push]"));
    await flush();
    assert.equal(ios.ops("simulate").length, 1, "an invalid payload is refused in the page");
    assert.match(ios.$("tools-msg-simulate").textContent, /must be a JSON object/);
    ios.$("tools-push-payload").value = JSON.stringify({ aps: { alert: "x".repeat(5000) } });
    click(ios.document.querySelector("[data-sim=push]"));
    await flush();
    assert.equal(ios.ops("simulate").length, 1);
    assert.match(ios.$("tools-msg-simulate").textContent, /larger than 4 KB/);
  });

  test("Android location: validation in the page, set, and an honest requested-only read-back", async () => {
    const page = boot({
      platform: "android",
      reply: ({ op }) => op === "location.get"
        ? ok({ latitude: 37.422, longitude: -122.084, provider: "gps", requested: { latitude: 52.3702, longitude: 4.8952 }, delivered: false })
        : op === "location.set" ? ok({ latitude: 52.3702, longitude: 4.8952, via: "emulator_console" }) : null,
    });
    const { $, ops } = page;
    click($("tools-toggle"));
    await flush();
    assert.equal($("tools-loc-clear"), null, "Android has no Clear");
    assert.equal($("tools-loc-get").hidden, false);
    $("tools-lat").value = "91";
    $("tools-lon").value = "-181";
    click($("tools-loc-set"));
    await flush();
    assert.equal(ops("location.set").length, 0);
    assert.match($("tools-msg-app").textContent, /Latitude must be a number from -90 to 90\. \(invalid_coordinates\)/);
    click(page.document.querySelector("[data-loc-preset=Amsterdam]"));
    await flush();
    assert.deepEqual(ops("location.set").map((call) => call.body.payload), [{ latitude: 52.3702, longitude: 4.8952 }]);
    click($("tools-loc-get"));
    await flush();
    assert.equal(ops("location.get").length, 1);
    assert.match($("tools-loc-readout").textContent, /^Requested only .*52\.3702, 4\.8952$/);
  });

  test("iOS location: Clear is offered, read-back is not, and the permission state is never shown", async () => {
    const page = boot({ platform: "ios", reply: ({ op }) => op === "location.clear" ? ok({ cleared: true }) : null });
    const { $, ops } = page;
    click($("tools-toggle"));
    await flush();
    assert.equal($("tools-loc-clear").hidden, false);
    assert.doesNotMatch($("tools-perms").textContent, /Granted|Denied|Unknown/);
    assert.equal($("tools-perms").querySelectorAll("[data-perm-action]").length, 6);
    $("tools-lat").value = "35.6764";
    $("tools-lon").value = "139.65";
    click($("tools-loc-set"));
    await flush();
    assert.match($("tools-loc-readout").textContent, /Last set here: 35\.6764, 139\.6500/);
    click($("tools-loc-clear"));
    await flush();
    assert.equal(ops("location.clear").length, 1);
    assert.equal($("tools-loc-readout").textContent, "No location set here.");
  });

  test("Network start asks first: cancel sends nothing, confirm sends acknowledged true", async () => {
    let running = false;
    const page = boot({
      platform: "android",
      reply: ({ op }) => {
        if (op === "network.start") { running = true; return ok({ running: true, port: 8080 }); }
        if (op === "network.status") return ok({ proxy: { running, port: running ? 8080 : null }, attached: false, recent_flow_count: 0 });
        return null;
      },
    });
    const { $, ops } = page;
    click($("tools-toggle"));
    await flush();
    click($("tools-tab-network"));
    await flush();
    assert.equal(ops("network.status").length, 1);
    assert.equal($("tools-net-proxy").textContent, "Stopped");
    click($("tools-net-start"));
    assert.equal($("tools-net-confirm").hidden, false);
    assert.match($("tools-net-confirm-text").textContent, /decrypts and records this session's HTTP\(S\) traffic/);
    assert.equal(page.document.activeElement, $("tools-net-cancel"));
    click($("tools-net-cancel"));
    await flush();
    assert.ok($("tools-net-confirm").hidden);
    assert.equal(ops("network.start").length, 0, "cancel sends nothing");
    click($("tools-net-start"));
    click($("tools-net-confirm-go"));
    await flush();
    assert.deepEqual(ops("network.start").map((call) => call.body.payload), [{ acknowledged: true }]);
    assert.equal($("tools-net-proxy").textContent, "Running on port 8080");
    assert.ok($("tools-net-start").hidden);
    assert.equal($("tools-net-attach").hidden, false);
    click($("tools-net-attach"));
    await flush();
    assert.deepEqual(ops("network.attach").map((call) => call.body.payload), [{ acknowledged: true }],
      "attach after the confirmed start needs no second confirmation");
  });

  test("Network without a session on this device shows the hint and never polls", async () => {
    const hint = "Start one with: autonom session start --platform android --serial emulator-5580";
    const page = boot({
      platform: "android",
      reply: ({ op }) => op === "network.status" ? refused(409, "no_active_session", "no Autonom session on this device", hint) : null,
    });
    const { $, ops } = page;
    click($("tools-toggle"));
    await flush();
    click($("tools-tab-network"));
    await flush();
    await page.timers.tick(5000);
    assert.equal($("tools-net-nosession").hidden, false);
    assert.equal($("tools-net-body").hidden, true);
    assert.equal($("tools-net-nosession-hint").textContent, hint);
    assert.equal(ops("network.requests").length, 0);
    assert.equal(ops("network.start").length, 0);
  });

  test("requests poll with since_id every second only while the Network tab is visible, one at a time", async () => {
    const hostileUrl = "https://api.example.com/items?<img src=x onerror=alert(1)>";
    let batch = [
      { id: "f2", method: "POST", host: "api.example.com", path: "/items", url: "https://api.example.com/items", status: 201, duration_ms: 12 },
      { id: "f1", method: "GET", host: "api.example.com", path: "/<b>bold</b>", url: hostileUrl, status: 503, duration_ms: 40, mocked: true },
    ];
    let hold = null;
    const page = boot({
      platform: "android",
      reply: ({ op, body }) => {
        if (op === "network.requests") {
          if (hold) return hold;
          const result = ok({ requests: batch, count: batch.length, total_matched: batch.length, truncated: false });
          batch = [];
          return result;
        }
        if (op === "network.request") {
          // As autonom network requests show --id: the flow sits under "request".
          return ok({ request: { id: body.payload.id, method: "GET", url: hostileUrl, host: "api.example.com", status: 503, duration_ms: 40,
            request_headers_preview: { authorization: "[REDACTED]" }, response_headers_preview: { "content-type": "text/html" },
            request_body_preview: null, response_body_preview: "<script>alert(1)</script>" } });
        }
        return null;
      },
    });
    const { $, ops } = page;
    click($("tools-toggle"));
    await flush();
    click($("tools-tab-network"));
    await flush();
    assert.equal(ops("network.requests").length, 1);
    assert.deepEqual(ops("network.requests")[0].body.payload, { max: 100 });
    const rows = $("tools-req-list").querySelectorAll("[data-req]");
    assert.deepEqual(rows.map((row) => row.getAttribute("data-req")), ["f2", "f1"], "newest first");
    assert.match(rows[1].textContent, /<b>bold<\/b>/);
    await page.timers.tick(1000);
    assert.equal(ops("network.requests").length, 2);
    assert.equal(ops("network.requests")[1].body.payload.since_id, "f2");

    click(rows[1]);
    await flush();
    assert.deepEqual(ops("network.request").map((call) => call.body.payload), [{ id: "f1" }]);
    assert.equal($("tools-req-detail").hidden, false);
    assert.match($("tools-req-detail-body").textContent, /\[REDACTED\]/);
    assert.match($("tools-req-detail-body").textContent, /<script>alert\(1\)<\/script>/);
    assert.equal($("tools-req-detail-title").textContent, "GET 503", "the detail reads the flow inside result.request");
    assert.ok($("tools-req-detail-body").textContent.includes(hostileUrl), "the detail shows the flow's URL");
    assert.match($("tools-req-detail-body").textContent, /content-type/);
    assert.equal(page.document.querySelectorAll("img,script").length, 0, "no markup from network data");

    click($("tools-req-mock"));
    await flush();
    assert.equal($("tools-tab-mocks").getAttribute("aria-selected"), "true");
    assert.equal($("tools-mock-glob").value, "https://api.example.com/items");
    assert.equal($("tools-mock-ignore-query").checked, true);
    assert.equal($("tools-mock-method").value, "GET");
    assert.equal($("tools-mock-host").value, "api.example.com");
    assert.equal($("tools-mock-status").value, "503");
    assert.match($("tools-msg-mocks").textContent, /Filled from request f1\./);
    const before = ops("network.requests").length;
    await page.timers.tick(5000);
    assert.equal(ops("network.requests").length, before, "no polling while the Network tab is hidden");

    click($("tools-tab-network"));
    await flush();
    hold = new Promise(() => {});
    await page.timers.tick(3000);
    assert.equal(ops("network.requests").length, before + 2, "a pending request blocks further polls");
  });

  test("filters restart the list without a cursor", async () => {
    const page = boot({
      platform: "android",
      reply: ({ op }) => op === "network.requests" ? ok({ requests: [{ id: "f9", method: "GET", host: "a.test", path: "/", status: 200 }] }) : null,
    });
    const { $, ops } = page;
    click($("tools-toggle"));
    await flush();
    click($("tools-tab-network"));
    await flush();
    $("tools-req-host").value = "a.test";
    $("tools-req-method").value = "GET";
    $("tools-req-status").value = "200";
    $("tools-req-mocked").value = "false";
    fire($("tools-req-mocked"), "change");
    await flush();
    const last = ops("network.requests").at(-1).body.payload;
    assert.deepEqual(last, { max: 100, host: "a.test", method: "GET", status: 200, mocked: false });
  });

  test("Mocks: the machine-wide note, add with validation, toggle, edit and clear after confirmation", async () => {
    let mocks = [{ id: "1", match: { url_glob: "*/a*", method: "GET", host: "api.example.com", ignore_query: false }, response: { status: 200, headers: { "X-A": "1" } }, enabled: true, note: "slow path", hits: 3 }];
    const page = boot({
      platform: "android",
      reply: ({ op, body }) => {
        if (op === "mocks.list") return ok({ mocks, registry: "/tmp/r.json" });
        if (op === "mocks.add") { mocks = [...mocks, { id: "2", match: { url_glob: body.payload.url_glob }, response: { status: body.payload.status }, enabled: true, hits: 0 }]; return ok(mocks.at(-1)); }
        if (op === "mocks.clear") { const removed = mocks.length; mocks = []; return ok({ removed }); }
        if (op === "mocks.update" && body.payload.status === 600) return refused(400, "invalid_value", "status must be 100-599", "Use a real HTTP status.");
        return null;
      },
    });
    const { $, ops } = page;
    click($("tools-toggle"));
    await flush();
    click($("tools-tab-mocks"));
    await flush();
    assert.equal($("tools-mocks-note").textContent, "Mocks apply to every Autonom session on this Mac.");
    assert.match($("tools-mocks").textContent, /GET \*\/a\*/);
    assert.match($("tools-mocks").textContent, /3 hits/);

    fire($("tools-mock-form"), "submit");
    await flush();
    assert.equal(ops("mocks.add").length, 0);
    assert.match($("tools-msg-mocks").textContent, /selector_required/);
    $("tools-mock-glob").value = "*/autonom-tools-test*";
    $("tools-mock-status").value = "99";
    fire($("tools-mock-form"), "submit");
    await flush();
    assert.equal(ops("mocks.add").length, 0);
    assert.match($("tools-msg-mocks").textContent, /from 100 to 599/);
    $("tools-mock-status").value = "418";
    $("tools-mock-headers").value = "Content-Type: text/plain\nnot a header";
    fire($("tools-mock-form"), "submit");
    await flush();
    assert.equal(ops("mocks.add").length, 0);
    $("tools-mock-headers").value = "Content-Type: text/plain";
    $("tools-mock-body").value = "mocked";
    fire($("tools-mock-form"), "submit");
    await flush();
    assert.deepEqual(ops("mocks.add").map((call) => call.body.payload), [{
      url_glob: "*/autonom-tools-test*", status: 418, ignore_query: false, headers: { "Content-Type": "text/plain" }, body: "mocked",
    }]);
    assert.match($("tools-msg-mocks").textContent, /Mock 2 added/);
    assert.equal($("tools-mocks").children.length, 2);

    click($("tools-mocks").querySelector("[data-mock-action=disable]"));
    await flush();
    assert.deepEqual(ops("mocks.disable").map((call) => call.body.payload), [{ id: "1" }]);

    click($("tools-mocks").querySelector("[data-mock-action=edit]"));
    assert.equal($("tools-mock-form-title").textContent, "Edit mock 1");
    assert.equal($("tools-mock-headers").value, "X-A: 1");
    $("tools-mock-status").value = "600";
    fire($("tools-mock-form"), "submit");
    await flush();
    assert.equal(ops("mocks.update").length, 0, "an out-of-range status is refused in the page");
    assert.equal($("tools-mock-method").value, "GET");
    assert.equal($("tools-mock-host").value, "api.example.com");
    assert.equal($("tools-mock-note").value, "slow path");
    $("tools-mock-status").value = "201";
    $("tools-mock-method").value = "";
    $("tools-mock-host").value = "";
    $("tools-mock-note").value = "";
    fire($("tools-mock-form"), "submit");
    await flush();
    const [update] = ops("mocks.update");
    assert.equal(update.body.payload.id, "1");
    assert.equal(update.body.payload.status, 201);
    assert.equal("body" in update.body.payload, false, "an empty body keeps the current one");
    assert.equal(update.body.payload.method, "", "an emptied method is sent so the tools process clears it");
    assert.equal(update.body.payload.host, "", "an emptied host is sent so it is cleared");
    assert.equal(update.body.payload.note, "", "an emptied note is sent so it is cleared");

    click($("tools-mock-clear"));
    assert.equal($("tools-mock-confirm").hidden, false);
    click($("tools-mock-cancel"));
    assert.equal(ops("mocks.clear").length, 0, "cancel sends nothing");
    click($("tools-mock-clear"));
    click($("tools-mock-clear-go"));
    await flush();
    assert.equal(ops("mocks.clear").length, 1);
    assert.match($("tools-msg-mocks").textContent, /Removed 2 mocks/);
  });

  test("Logs follow while visible, cap the rows, filter, pause and stop when the drawer closes", async () => {
    let seq = 0;
    const lines = (count, level = "I") => Array.from({ length: count }, () => {
      seq += 1;
      return { seq, kind: "line", source: "device", ts: "t", text: `10-07 12:00:00.000 ${level}/Tag( 100): line ${seq}` };
    });
    let pending = lines(3).concat(lines(1, "E"));
    const page = boot({
      platform: "android",
      reply: ({ path, method }) => {
        if (path.startsWith("/tools/logs?") && method === "GET") {
          const out = pending;
          pending = [];
          return { status: 200, body: { ok: true, running: true, package: null, lines: out, next: out.length ? out.at(-1).seq : seq, dropped: 0, error: null } };
        }
        if (path === "/tools/logs") return { status: 200, body: { ok: true, running: true } };
        return null;
      },
    });
    const { $, calls } = page;
    const feedPosts = () => calls.filter((call) => call.path === "/tools/logs" && call.method === "POST");
    const reads = () => calls.filter((call) => call.path.startsWith("/tools/logs?"));
    click($("tools-toggle"));
    await flush();
    assert.equal(reads().length, 0, "no log polling outside the Logs tab");
    click($("tools-tab-logs"));
    await flush();
    assert.deepEqual(feedPosts().map((call) => call.body), [{ active: true, package: null }]);
    assert.equal(feedPosts()[0].headers["X-Autonom-CSRF"], "csrf-123");
    assert.equal(reads()[0].path, "/tools/logs?after=0&limit=500");
    assert.equal($("tools-log").children.length, 4);
    assert.equal($("tools-log").children[3].getAttribute("data-level"), "E");
    assert.match($("tools-log-state").textContent, /Following the device log/);

    pending = lines(2100);
    await page.timers.tick(700);
    assert.equal(reads().at(-1).path, "/tools/logs?after=4&limit=500");
    assert.equal($("tools-log").children.length, 2000, "at most 2000 rows in the DOM");
    assert.match($("tools-log").lastChild.textContent, /line 2104$/);

    $("tools-log-level").value = "4";
    fire($("tools-log-level"), "change");
    assert.equal($("tools-log").children.length, 0, "the error line fell out of the 2000-line buffer");
    pending = lines(1, "E").concat(lines(1, "W"));
    await page.timers.tick(700);
    assert.equal($("tools-log").children.length, 1);
    $("tools-log-level").value = "0";
    $("tools-log-filter").value = "line 2106";
    fire($("tools-log-filter"), "input");
    assert.equal($("tools-log").children.length, 1);
    $("tools-log-filter").value = "";
    fire($("tools-log-filter"), "input");

    click($("tools-log-pause"));
    assert.equal($("tools-log-pause").getAttribute("aria-pressed"), "true");
    const shown = $("tools-log").children.length;
    pending = lines(5);
    await page.timers.tick(700);
    assert.equal($("tools-log").children.length, shown, "paused: polling continues, rendering stops");
    assert.match($("tools-log-count").textContent, /5 new while paused/);
    click($("tools-log-pause"));
    assert.match($("tools-log").lastChild.textContent, /line 2111$/);
    click($("tools-log-clear"));
    assert.equal($("tools-log").children.length, 0);

    click($("tools-tab-app"));
    const before = reads().length;
    await page.timers.tick(3000);
    assert.equal(reads().length, before, "no log polling while the Logs tab is hidden");
    click($("tools-close"));
    await flush();
    assert.deepEqual(feedPosts().at(-1).body, { active: false });
    await page.timers.tick(3000);
    assert.equal(reads().length, before);
  });

  test("a slower, older permissions reply never replaces the list of the app now loaded", async () => {
    const releases = [];
    const page = boot({
      platform: "android",
      reply: ({ op, body }) => {
        if (op !== "permissions.list") return null;
        const app = body.payload.app_id;
        const answer = ok({ app_id: app, readable: true, permissions: [{ name: `android.permission.${app === "com.b" ? "READ_CONTACTS" : "CAMERA"}`, alias: null, granted: app !== "com.b" }] });
        return new Promise((resolve) => releases.push({ app, release: () => resolve(answer) }));
      },
    });
    const { $, ops } = page;
    click($("tools-toggle"));
    await flush();
    assert.deepEqual(releases.map((item) => item.app), ["com.example.app"], "context started the first load");
    $("tools-app-id").value = "com.b";
    fire($("tools-app-id"), "input");
    click($("tools-perms-load"));
    await flush();
    assert.deepEqual(releases.map((item) => item.app), ["com.example.app", "com.b"]);
    releases[1].release();
    await flush();
    releases[0].release();
    await flush();
    assert.match($("tools-perms").textContent, /READ_CONTACTS/);
    assert.doesNotMatch($("tools-perms").textContent, /CAMERA/, "the late reply for the old app is dropped");
    click($("tools-perms").querySelector("[data-perm-action=revoke]"));
    await flush();
    assert.deepEqual(ops("permissions.set").map((call) => call.body.payload),
      [{ app_id: "com.b", service: "android.permission.READ_CONTACTS", action: "revoke" }]);

    $("tools-app-id").value = "com.c";
    fire($("tools-app-id"), "input");
    assert.equal($("tools-perms").children.length, 0, "editing the app id clears the list of another app");
    assert.match($("tools-perms-empty").textContent, /Press Load/);
  });

  test("a package change drops log lines of the old feed that were still in flight", async () => {
    let held = null;
    const page = boot({
      platform: "android",
      reply: ({ path, method }) => {
        if (path.startsWith("/tools/logs?") && method === "GET") {
          if (held) return held.promise;
          return { status: 200, body: { ok: true, running: true, package: null, lines: [], next: 0, dropped: 0, error: null } };
        }
        if (path === "/tools/logs") return { status: 200, body: { ok: true, running: true } };
        return null;
      },
    });
    const { $, calls } = page;
    const reads = () => calls.filter((call) => call.path.startsWith("/tools/logs?"));
    click($("tools-toggle"));
    await flush();
    click($("tools-tab-logs"));
    await flush();
    let release;
    held = { promise: new Promise((resolve) => { release = resolve; }) };
    await page.timers.tick(700);
    const inFlight = reads().length;
    $("tools-log-package").value = "com.b";
    click($("tools-log-apply"));
    await flush();
    assert.deepEqual(calls.filter((call) => call.path === "/tools/logs" && call.method === "POST").at(-1).body, { active: true, package: "com.b" });
    held = null;
    release({ status: 200, body: { ok: true, running: true, package: null, lines: [{ seq: 41, kind: "line", text: "OLD-ALL-APPS line" }], next: 41, dropped: 0, error: null } });
    await flush();
    assert.equal($("tools-log").children.length, 0, "the old feed's lines are not shown under the new package");
    await page.timers.tick(700);
    assert.ok(reads().length > inFlight);
    assert.equal(reads().at(-1).path, "/tools/logs?after=0&limit=500", "the cursor of the old feed is not kept");
  });

  test("iOS log levels come from the ndjson messageType and the line reads as text", async () => {
    const record = (seq, messageType, eventMessage) => ({ seq, kind: "line", source: "device", ts: "t",
      text: JSON.stringify({ timestamp: "2026-10-07 12:00:00.000000+0000", processImagePath: "/Applications/Example.app/Example", messageType, subsystem: "com.example", eventMessage }) });
    let pending = [
      { seq: 1, kind: "line", source: "device", ts: "t", text: "Filtering the log data using \"subsystem == com.example\"" },
      record(2, "Debug", "autonom-tools-marker-debug"), record(3, "Info", "autonom-tools-marker-info"),
      record(4, "Default", "autonom-tools-marker-default"), record(5, "Error", "autonom-tools-marker-1"),
      record(6, "Fault", "autonom-tools-marker-fault"),
    ];
    const page = boot({
      platform: "ios",
      reply: ({ path, method }) => {
        if (path.startsWith("/tools/logs?") && method === "GET") {
          const out = pending;
          pending = [];
          return { status: 200, body: { ok: true, running: true, package: null, lines: out, next: out.length ? out.at(-1).seq : 6, dropped: 0, error: null } };
        }
        return null;
      },
    });
    const { $ } = page;
    click($("tools-toggle"));
    await flush();
    click($("tools-tab-logs"));
    await flush();
    const rows = () => $("tools-log").children;
    assert.equal(rows().length, 6, "all levels show everything");
    assert.equal(rows()[4].getAttribute("data-level"), "Error");
    assert.equal(rows()[4].textContent, "2026-10-07 12:00:00.000000+0000 Example Error com.example autonom-tools-marker-1");
    assert.doesNotMatch($("tools-log").textContent, /eventMessage|\{"/, "records render as a compact line, not raw JSON");
    $("tools-log-level").value = "3";
    fire($("tools-log-level"), "change");
    assert.deepEqual(rows().map((row) => row.getAttribute("data-level")), ["Error", "Fault"], "errors and faults");
    $("tools-log-level").value = "1";
    fire($("tools-log-level"), "change");
    assert.deepEqual(rows().map((row) => row.getAttribute("data-level")), ["Info", "Default", "Error", "Fault"], "info and above");
    $("tools-log-level").value = "2";
    fire($("tools-log-level"), "change");
    $("tools-log-filter").value = "marker-1";
    fire($("tools-log-filter"), "input");
    assert.equal(rows().length, 1);
    assert.match(rows()[0].textContent, /autonom-tools-marker-1$/);
  });

  test("an iOS log feed that cannot start shows the server's reason", async () => {
    const reason = "Start a session on this Simulator: autonom session start --platform ios --udid 3760A5A8 --log-stream";
    const page = boot({
      platform: "ios",
      reply: ({ path, method }) => path.startsWith("/tools/logs?") && method === "GET"
        ? { status: 200, body: { ok: true, running: false, package: null, lines: [], next: 0, dropped: 0, error: reason } }
        : null,
    });
    const { $ } = page;
    click($("tools-toggle"));
    await flush();
    click($("tools-tab-logs"));
    await flush();
    assert.equal($("tools-log-state").textContent, reason);
    assert.equal($("tools-log-state").getAttribute("data-kind"), "error");
  });

  test("an invalid log package is refused in the page", async () => {
    const page = boot({ platform: "android" });
    const { $, calls } = page;
    click($("tools-toggle"));
    await flush();
    click($("tools-tab-logs"));
    await flush();
    $("tools-log-package").value = "com.example; rm -rf /";
    click($("tools-log-apply"));
    await flush();
    assert.match($("tools-msg-logs").textContent, /letters, digits, dots and underscores/);
    assert.equal(calls.filter((call) => call.path === "/tools/logs" && call.method === "POST").length, 1);
    $("tools-log-package").value = "com.example.app";
    click($("tools-log-apply"));
    await flush();
    assert.deepEqual(calls.filter((call) => call.path === "/tools/logs" && call.method === "POST").at(-1).body, { active: true, package: "com.example.app" });
  });

  test("a failing context request shows its error and is retried on the next open", async () => {
    let fail = true;
    const page = boot({ platform: "android", reply: ({ op }) => op === "context" && fail ? { status: 502, body: { ok: false, error: "tools process is not running", error_code: "tools_unavailable", hint: null } } : null });
    const { $, ops } = page;
    click($("tools-toggle"));
    await flush();
    assert.match($("tools-msg-app").textContent, /tools process is not running \(tools_unavailable\)/);
    fail = false;
    click($("tools-toggle"));
    click($("tools-toggle"));
    await flush();
    assert.equal(ops("context").length, 2);
    assert.equal($("tools-app-id").value, "com.example.app");
  });

  // Regression: the server counts each lost seq once, in the read whose (after, next] range holds
  // it, so the page adds the reads up instead of keeping the last non-zero value.
  test("log lines dropped by the Canvas buffer are summed over the reads of one feed", async () => {
    const drops = [3, 0, 2];
    let seq = 0;
    const page = boot({
      platform: "android",
      reply: ({ path, method }) => {
        if (path.startsWith("/tools/logs?") && method === "GET") {
          seq += 1;
          const dropped = drops.length ? drops.shift() : 0;
          return { status: 200, body: { ok: true, running: true, package: null, lines: [{ seq, kind: "line", text: "line " + seq }], next: seq, dropped, error: null } };
        }
        if (path === "/tools/logs") return { status: 200, body: { ok: true, running: true } };
        return null;
      },
    });
    const { $ } = page;
    click($("tools-toggle"));
    await flush();
    click($("tools-tab-logs"));
    await flush();
    assert.match($("tools-log-count").textContent, /· 3 dropped by the Canvas buffer$/);
    await page.timers.tick(700);
    assert.match($("tools-log-count").textContent, /· 3 dropped by the Canvas buffer$/, "a read without drops keeps the total");
    await page.timers.tick(700);
    assert.match($("tools-log-count").textContent, /· 5 dropped by the Canvas buffer$/, "the next drops add to it");
    $("tools-log-package").value = "com.example.other";
    click($("tools-log-apply"));
    await flush();
    assert.doesNotMatch($("tools-log-count").textContent, /dropped/, "a new feed starts its own count");
  });

  // Regression: against the real LogFeed, a line lost past the first page of a backlog was
  // counted again on each page until the cursor passed it, so the page showed 2 for 1 drop.
  test("the drop count against the real log feed counts one lost line once over paged reads", async () => {
    const feed = new LogFeed({ spawnFollow: () => { throw new Error("not used"); } });
    const run = { ended: false, stopping: true };
    feed.run = run;
    for (let i = 1; i <= 1200; i += 1) {
      if (i === 900) feed.dropOne();
      else feed.onLine(run, Buffer.from(JSON.stringify({ kind: "line", ts: "2026-10-07T00:00:00Z", text: "line " + i })));
    }
    try {
      const page = boot({
        platform: "android",
        reply: ({ path, method }) => {
          if (path.startsWith("/tools/logs?") && method === "GET") {
            const query = new URL("http://canvas" + path).searchParams;
            return { status: 200, body: feed.read(Number(query.get("after")), Number(query.get("limit"))) };
          }
          if (path === "/tools/logs") return { status: 200, body: { ok: true, running: true } };
          return null;
        },
      });
      const { $ } = page;
      click($("tools-toggle"));
      await flush();
      click($("tools-tab-logs"));
      await flush();
      for (let i = 0; i < 4; i += 1) await page.timers.tick(700);
      assert.match($("tools-log-count").textContent, /· 1 dropped by the Canvas buffer$/);
    } finally {
      feed.run = null;
      clearTimeout(feed.idleTimer);
    }
  });

  // Regression: the verdict on the re-read comes from the re-read itself. A network.status that
  // fails with no_active_session writes nothing into the message box (it shows the no-session
  // callout), yet the re-read failed and the hint must say so.
  test("a timed-out change reports its own re-read failure even when that failure leaves the message box empty", async () => {
    let session = true;
    const page = boot({
      platform: "android",
      reply: ({ op }) => {
        if (op === "network.status") {
          return session
            ? ok({ proxy: { running: false, port: null }, attached: false, recent_flow_count: 0 })
            : refused(409, "no_active_session", "No Autonom session is on this device", "autonom session start --serial emulator-5580");
        }
        if (op === "network.start") { session = false; return { status: 504, body: { ok: false, error: "the tools call took longer than 60 s", error_code: "timeout" } }; }
        return null;
      },
    });
    const { $, ops } = page;
    click($("tools-toggle"));
    await flush();
    click($("tools-tab-network"));
    await flush();
    click($("tools-net-start"));
    click($("tools-net-confirm-go"));
    await flush();
    assert.equal(ops("network.start").length, 1);
    assert.equal(ops("network.status").length, 2, "network.status is read again once");
    const box = $("tools-msg-network");
    assert.match(box.textContent, /outcome is unknown/);
    assert.match(box.textContent, /Re-reading the current state also failed: No Autonom session is on this device \(no_active_session\)/);
    assert.doesNotMatch(box.textContent, /current state was re-read/);
  });

  // Regression: a timed-out location change re-reads the location where the device can report it
  // (location.get on Android), never the permission list, and says plainly when it cannot.
  test("a timed-out location change re-reads the location on Android and says it cannot on iOS", async () => {
    const timeout = { status: 504, body: { ok: false, error: "the tools call took longer than 60 s", error_code: "timeout" } };
    const android = boot({
      platform: "android",
      reply: ({ op }) => op === "location.set" ? timeout
        : op === "location.get" ? ok({ latitude: 52.3702, longitude: 4.8952, requested: { latitude: 52.3702, longitude: 4.8952 }, delivered: true })
          : null,
    });
    click(android.$("tools-toggle"));
    await flush();
    const permsBefore = android.ops("permissions.list").length;
    click(android.document.querySelector("[data-loc-preset=Amsterdam]"));
    await flush();
    assert.equal(android.ops("location.set").length, 1, "the change is sent once and never retried");
    assert.equal(android.ops("location.get").length, 1, "the location is read back");
    assert.equal(android.ops("permissions.list").length, permsBefore, "the permission list is not what a location change touches");
    assert.match(android.$("tools-loc-readout").textContent, /^Delivered: 52\.3702, 4\.8952$/);
    assert.match(android.$("tools-msg-app").textContent, /outcome is unknown/);
    assert.match(android.$("tools-msg-app").textContent, /The location was read back/);

    // The read-back fails too: its own error is named.
    const failing = boot({
      platform: "android",
      reply: ({ op }) => op === "location.set" ? timeout
        : op === "location.get" ? refused(502, "backend_failed", "the emulator console did not answer", null) : null,
    });
    click(failing.$("tools-toggle"));
    await flush();
    click(failing.document.querySelector("[data-loc-preset=Tokyo]"));
    await flush();
    assert.match(failing.$("tools-msg-app").textContent, /Re-reading the current state also failed: the emulator console did not answer \(backend_failed\)/);

    const ios = boot({ platform: "ios", reply: ({ op }) => op === "location.clear" || op === "location.set" ? timeout : null });
    click(ios.$("tools-toggle"));
    await flush();
    const iosPerms = ios.ops("permissions.list").length;
    click(ios.$("tools-loc-clear"));
    await flush();
    assert.equal(ios.ops("location.clear").length, 1);
    assert.equal(ios.ops("location.get").length, 0, "the Simulator cannot report its location");
    assert.equal(ios.ops("permissions.list").length, iosPerms);
    assert.match(ios.$("tools-msg-app").textContent, /outcome is unknown/);
    assert.match(ios.$("tools-msg-app").textContent, /cannot report its location, so it could not be re-read/);
    assert.doesNotMatch(ios.$("tools-msg-app").textContent, /current state was re-read/);
    assert.match(ios.$("tools-loc-readout").textContent, /^Unknown/);
  });
});
