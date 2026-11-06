#!/usr/bin/env node
/**
 * Live check of the Canvas Mocks form in Chromium (VER-004, MR-14).
 *
 *   node tests/live/canvas_mock_form_shots.mjs --out DIR [--playwright DIR] [--python PATH]
 *
 * Not part of `node --test`. It renders this checkout's real device page (renderPage) in
 * headless Chromium and serves it from a routed origin; no Canvas server, device or network
 * is used (every host name is mapped to "not found", WebSockets are closed). The drawer's
 * /tools/call requests are answered here: mocks.* ops go to this checkout's real tools process
 * (scripts/autonom_canvas_tools.py) in a temporary AUTONOM_HOME, so payloads are checked and
 * stored exactly as the Canvas would store them; the other ops get fixed replies.
 *
 * Checks, at 1280 px and 360 px wide:
 *   layout    the empty form in both modes, a filled edit and the invalid state: nothing scrolls
 *             sideways (page, drawer body, form) and no form control lacks an accessible name
 *   add       MR-14-S01: Edit real response, a glob ending in /profile, JSON set user.plan = "pro", Save:
 *             mocks.add gets exactly that payload and the list shows the rule with the Edit badge
 *   round     MR-14-S02: rules made with every condition and edit kind (secrets included), a
 *             held fake JSON response and a whole new body with a delay: Edit, Save unchanged,
 *             and the stored rule is identical apart from updated_at
 *   invalid   a bad header name, a bad JSON path and status 700: a message beside each field,
 *             each field marked invalid, and no call made
 *
 * Writes screenshots and result.json to --out, prints the same JSON, exits 0 when every check is
 * ok, 1 otherwise, 2 on bad arguments. The temporary home is removed at the end. Playwright comes
 * from --playwright, AUTONOM_PLAYWRIGHT, ~/pr/platform/node_modules/playwright, then a
 * `playwright` package Node can resolve.
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

import { renderPage } from "../../plugins/autonom/skills/android-emulator-browser/scripts/canvas-device-page.mjs";

const ROOT = resolve(import.meta.dirname, "../..");
const ORIGIN = "http://canvas.test";
const USAGE = "usage: node tests/live/canvas_mock_form_shots.mjs --out DIR [--playwright DIR] [--python PATH]";

let args;
try {
  ({ values: args } = parseArgs({ options: { out: { type: "string" }, playwright: { type: "string" }, python: { type: "string" }, help: { type: "boolean" } } }));
} catch (error) {
  console.error(`${error.message}\n${USAGE}`);
  process.exit(2);
}
if (args.help) { console.log(USAGE); process.exit(0); }
if (!args.out) { console.error(USAGE); process.exit(2); }
const OUT = resolve(args.out);

const checks = [];
const screenshots = [];
function check(name, ok, detail = {}) { checks.push({ name, ok: Boolean(ok), ...detail }); return Boolean(ok); }

// --- the real tools process, mocks ops only ---------------------------------------------------

async function startTools(home) {
  const python = args.python || process.env.AUTONOM_TEST_PYTHON || "python3";
  const child = spawn(python, ["-B", join(ROOT, "scripts/autonom_canvas_tools.py"), "--platform", "android", "--target", "emulator-5580",
    "--tool", "/usr/bin/false", "--captures-dir", join(home, "captures")], {
    cwd: ROOT, stdio: ["pipe", "pipe", "pipe"],
    env: { ...process.env, AUTONOM_HOME: join(home, "home"), XDG_STATE_HOME: join(home, "state"), AUTONOM_CAPTURES_DIR: join(home, "captures"), PYTHONDONTWRITEBYTECODE: "1" },
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const waiting = new Map();
  createInterface({ input: child.stdout }).on("line", (line) => {
    const reply = JSON.parse(line);
    waiting.get(reply.id)?.(reply);
    waiting.delete(reply.id);
  });
  let exited = null;
  child.on("exit", (code) => { exited = code; for (const done of waiting.values()) done({ ok: false, error: `tools process exited (${code}): ${stderr.slice(-400)}`, error_code: "tools_unavailable" }); waiting.clear(); });
  let next = 0;
  const call = (op, payload = {}) => new Promise((done) => {
    if (exited !== null) { done({ ok: false, error: "tools process exited", error_code: "tools_unavailable" }); return; }
    next += 1;
    waiting.set(next, done);
    child.stdin.write(JSON.stringify({ id: next, op, origin: "human", payload }) + "\n");
  });
  const stop = () => new Promise((done) => {
    if (exited !== null) { done(); return; }
    child.once("exit", () => done());
    child.stdin.end();
    setTimeout(() => child.kill("SIGKILL"), 3000).unref();
  });
  const registry = async () => JSON.parse(await readFile(join(home, "home", "mocks", "registry.json"), "utf8")).mocks;
  return { call, stop, registry };
}

// --- the stubbed call layer --------------------------------------------------------------------

const CONTEXT = {
  platform: "android", target_id: "emulator-5580", emulator: true, session: null, app_id: null,
  privacy_services: null, android_permission_aliases: {}, simulate: {}, location_readable: true,
  location_clearable: false, network_available: true, permissions_readable: true,
};
const CANNED = {
  context: CONTEXT,
  "network.status": { proxy: { running: false, port: null }, attached: false, recent_flow_count: 0 },
  "network.requests": { requests: [], count: 0, total_matched: 0, truncated: false },
  "location.get": { latitude: 37.422, longitude: -122.084, delivered: true },
};

function makeStub(tools) {
  const seen = [];
  async function answer(op, payload) {
    seen.push({ op, payload });
    if (op.startsWith("mocks.")) {
      const reply = await tools.call(op, payload);
      if (reply.ok) return { status: 200, body: { ok: true, result: reply.result } };
      return { status: 400, body: { ok: false, error: reply.error, error_code: reply.error_code, hint: reply.hint ?? null, capability: null } };
    }
    return { status: 200, body: { ok: true, result: CANNED[op] || {} } };
  }
  return { seen, answer, ops: (op) => seen.filter((entry) => entry.op === op) };
}

// --- Playwright ---------------------------------------------------------------------------------

async function loadPlaywright() {
  const candidates = [args.playwright, process.env.AUTONOM_PLAYWRIGHT, join(homedir(), "pr/platform/node_modules/playwright")].filter(Boolean);
  for (const directory of candidates) {
    const entry = join(directory, "index.mjs");
    if (existsSync(entry)) return await import(pathToFileURL(entry).href);
  }
  try {
    return await import("playwright");
  } catch {
    throw new Error(`Playwright was not found (tried ${candidates.join(", ") || "nothing"} and the playwright package); pass --playwright <node_modules/playwright>`);
  }
}

const HTML = renderPage({ serial: "emulator-5580", id: null, options: { platform: "android" } });

async function openPage(browser, stub, viewport, colorScheme = "light") {
  const page = await browser.newPage({ viewport, colorScheme });
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error && error.message || error)));
  if (typeof page.routeWebSocket === "function") await page.routeWebSocket(/.*/, (socket) => socket.close());
  await page.route("**/*", async (route) => {
    const request = route.request(), address = new URL(request.url());
    if (address.origin !== ORIGIN) { await route.abort("addressunreachable"); return; }
    if (address.pathname === "/" && request.method() === "GET") {
      await route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: HTML });
      return;
    }
    if (address.pathname.endsWith("/tools/call") && request.method() === "POST") {
      const { op, payload } = JSON.parse(request.postData() || "{}");
      const reply = await stub.answer(op, payload || {});
      await route.fulfill({ status: reply.status, contentType: "application/json", body: JSON.stringify(reply.body) });
      return;
    }
    await route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ ok: false, error: "not served in this check" }) });
  });
  await page.goto(`${ORIGIN}/`);
  await page.waitForSelector("#tools-toggle");
  await page.click("#tools-toggle");
  await page.click("#tools-tab-mocks");
  await page.waitForFunction(() => document.getElementById("tools-panel-mocks") && !document.getElementById("tools-panel-mocks").hidden);
  await page.waitForTimeout(150);
  return { page, errors };
}

/** Sideways scroll and unnamed controls, measured in the page. */
function measure(page) {
  return page.evaluate(() => {
    const form = document.getElementById("tools-mock-form"), body = document.querySelector("#tools .tools-body");
    const visible = (node) => node.getClientRects().length > 0 && getComputedStyle(node).visibility !== "hidden";
    const name = (node) => {
      if (node.getAttribute("aria-label")) return node.getAttribute("aria-label").trim();
      const by = node.getAttribute("aria-labelledby");
      if (by) return by.split(/\s+/).map((id) => (document.getElementById(id) || {}).textContent || "").join(" ").trim();
      if (node.labels && node.labels.length) return [...node.labels].map((label) => label.textContent).join(" ").trim();
      return node.localName === "button" ? node.textContent.trim() : "";
    };
    const controls = [...form.querySelectorAll("input,select,textarea,button")].filter((node) => visible(node) || node.type === "radio");
    const unlabeled = controls.filter((node) => !name(node)).map((node) => node.id || node.className || node.localName);
    const right = form.getBoundingClientRect().right;
    const overflowing = [...form.querySelectorAll("*")].filter((node) => visible(node) && node.getBoundingClientRect().right > right + 0.5).map((node) => node.id || node.className || node.localName);
    return {
      width: window.innerWidth, page_scroll_width: document.documentElement.scrollWidth,
      drawer_scroll_width: body.scrollWidth, drawer_client_width: body.clientWidth,
      form_scroll_width: form.scrollWidth, form_client_width: form.clientWidth,
      controls: controls.length, unlabeled, overflowing: overflowing.slice(0, 10),
    };
  });
}
function layoutOk(metrics) {
  return metrics.page_scroll_width <= metrics.width && metrics.drawer_scroll_width <= metrics.drawer_client_width
    && metrics.form_scroll_width <= metrics.form_client_width && metrics.unlabeled.length === 0 && metrics.overflowing.length === 0;
}

async function shoot(page, name, { form = false } = {}) {
  const file = `${name}.png`;
  if (form) {
    const size = page.viewportSize();
    const needed = await page.evaluate(() => {
      const panel = document.getElementById("tools-panel-mocks");
      return Math.ceil(panel.scrollHeight + 200);
    });
    await page.setViewportSize({ width: size.width, height: Math.max(size.height, Math.min(needed, 4000)) });
    await page.waitForTimeout(100);
    await page.locator("#tools").screenshot({ path: join(OUT, file) });
    await page.setViewportSize(size);
  } else {
    await page.screenshot({ path: join(OUT, file) });
  }
  screenshots.push(file);
  return file;
}

async function pickMode(page, mode) { await page.click(mode === "rewrite" ? "label:has(#tools-mock-mode-rewrite)" : "label:has(#tools-mock-mode-respond)"); }
async function addRow(page, kind, a, b) {
  await page.click(`[data-mock-add="${kind}"]`);
  const fields = page.locator(`.tools-mock-row[data-kind="${kind}"]`).last().locator("input");
  await fields.nth(0).fill(a);
  if (b !== undefined) await fields.nth(1).fill(b);
}
async function fillRewriteExample(page) {
  await pickMode(page, "rewrite");
  await page.fill("#tools-mock-glob", "*/profile*");
  await addRow(page, "query", "page", "2");
  await addRow(page, "request_headers", "X-Client", "ios*");
  await addRow(page, "body_json", "user.role", "admin");
  await page.fill("#tools-mock-status", "203");
  await page.fill("#tools-mock-headers", "Retry-After: 5");
  await addRow(page, "remove_headers", "ETag");
  await page.selectOption("#tools-mock-body-mode", "edit");
  await addRow(page, "json_set", "user.plan", '"pro"');
  await addRow(page, "json_remove", "user.trial_ends");
  await addRow(page, "replace", "Free", "Pro");
  await page.selectOption("#tools-mock-timing", "delay");
  await page.fill("#tools-mock-delay", "1.5");
}
async function fillInvalid(page) {
  await page.fill("#tools-mock-glob", "*/items*");
  await page.fill("#tools-mock-status", "700");
  await addRow(page, "request_headers", "Bad Name", "x");
  await addRow(page, "body_json", "user..role", "admin");
  await page.click("#tools-mock-save");
  await page.waitForTimeout(150);
}

const FIXTURES = {
  every_kind: {
    url_glob: "*/profile*", mode: "rewrite", method: "get", host: "api.example.com", status: 503, headers: { "Retry-After": "5", Authorization: "Bearer resp-secret" },
    note: "edit the profile", delay_s: 1.5,
    query: [{ name: "page", value: "2" }, { name: "debug", value: null }, { name: "flag", value: "" }, { name: "token", value: "abc123" }],
    request_headers: [{ name: "X-Client", value: "ios*" }, { name: "Authorization", value: "Bearer abc123" }],
    body_contains: ["needle"],
    body_json: [{ path: "user.role", value: "admin" }, { path: "items.0.qty", value: 2 }, { path: "code", value: "2" }, { path: "auth.password", value: "abc123" }],
    remove_headers: ["ETag"],
    json_set: [{ path: "user.plan", value: "pro" }, { path: "flags", value: { beta: true, list: [1, null] } }, { path: "auth.token", value: "abc123" }],
    json_remove: ["user.trial_ends"],
    replace: [{ find: "Free", with: "Pro" }, { find: "x", with: "" }],
  },
  held_json: { url_glob: "*/held*", method: "POST", body: '{"ok": false}', hang: true, query: [{ name: "debug", value: null }], body_contains: ["a"] },
  whole_body_delay: { url_glob: "*/whole*", mode: "rewrite", body: "plain text", delay_s: 0.5, headers: { "X-Edited": "1" } },
};
const withoutStamp = (rule) => { const copy = { ...rule }; delete copy.updated_at; return copy; };

async function main() {
  await mkdir(OUT, { recursive: true });
  const home = await mkdtemp(join(tmpdir(), "autonom-mock-form-live-"));
  const tools = await startTools(home);
  const stub = makeStub(tools);
  let browser = null;
  try {
    const listed = await tools.call("mocks.list");
    check("tools process answers mocks.list in a temporary home", listed.ok && listed.result.mocks.length === 0, { error: listed.ok ? null : listed.error });
    const { chromium } = await loadPlaywright();
    browser = await chromium.launch({ headless: true, args: ["--host-resolver-rules=MAP * ~NOTFOUND"] });

    // 1. Layout at both widths: the empty form in each mode, a filled edit, dark appearance.
    for (const [label, viewport] of [["desktop", { width: 1280, height: 900 }], ["phone", { width: 360, height: 780 }]]) {
      const { page, errors } = await openPage(browser, stub, viewport);
      const input = await page.evaluate(() => typeof canSendInput === "function" && canSendInput());
      check(`${label}: the drawer may change mocks`, input && await page.isEnabled("#tools-mock-save"));
      let metrics = await measure(page);
      check(`${label}: empty fake-response form fits and every control is named`, layoutOk(metrics), { metrics });
      await shoot(page, `${label}-page-respond`);
      await shoot(page, `${label}-form-respond`, { form: true });
      await pickMode(page, "rewrite");
      metrics = await measure(page);
      check(`${label}: empty edit form fits and every control is named`, layoutOk(metrics), { metrics });
      await fillRewriteExample(page);
      metrics = await measure(page);
      check(`${label}: a filled edit with every row kind fits and every control is named`, layoutOk(metrics), { metrics });
      await shoot(page, `${label}-form-rewrite-filled`, { form: true });
      check(`${label}: no script errors`, errors.length === 0, { errors });
      await page.close();

      const dark = await openPage(browser, stub, viewport, "dark");
      await fillRewriteExample(dark.page);
      await shoot(dark.page, `${label}-form-rewrite-dark`, { form: true });
      check(`${label}: dark appearance renders without script errors`, dark.errors.length === 0, { errors: dark.errors });
      await dark.page.close();
    }

    // 2. MR-14-S01 at desktop width.
    {
      const { page } = await openPage(browser, stub, { width: 1280, height: 900 });
      const before = stub.ops("mocks.add").length;
      await pickMode(page, "rewrite");
      await page.fill("#tools-mock-glob", "*/profile");
      await page.selectOption("#tools-mock-body-mode", "edit");
      await addRow(page, "json_set", "user.plan", '"pro"');
      await page.click("#tools-mock-save");
      await page.waitForFunction(() => /added/.test(document.getElementById("tools-msg-mocks").textContent));
      await page.waitForSelector("#tools-mocks .tools-mock-badge");
      const sent = stub.ops("mocks.add").slice(before).map((entry) => entry.payload);
      const expected = [{ url_glob: "*/profile", mode: "rewrite", ignore_query: false, headers: {}, json_set: [{ path: "user.plan", value: "pro" }] }];
      check("MR-14-S01: mocks.add gets mode rewrite and the JSON set", JSON.stringify(sent) === JSON.stringify(expected), { sent, expected });
      const stored = (await tools.registry()).find((rule) => rule.match.url_glob === "*/profile");
      check("MR-14-S01: the rule is stored as an edit of the real response", stored && stored.mode === "rewrite" && stored.response.status === null
        && JSON.stringify(stored.rewrite) === JSON.stringify({ json_set: [{ path: "user.plan", value: "pro" }] }), { stored });
      const row = await page.evaluate(() => [...document.querySelectorAll("#tools-mocks .tools-item")].map((item) => ({
        badge: (item.querySelector(".tools-mock-badge") || {}).textContent || null, title: item.querySelector("b").textContent, detail: item.querySelector("small").textContent })));
      check("MR-14-S01: the list shows the rule with the Edit badge", row.some((item) => item.badge === "Edit" && item.title === "*/profile"), { rows: row });
      await shoot(page, "desktop-page-added");
      await page.close();
    }

    // 3. MR-14-S02 round trips, one width each, alternating.
    for (const [index, [name, fixture]] of Object.entries(FIXTURES).entries()) {
      const added = await tools.call("mocks.add", fixture);
      if (!check(`round trip ${name}: fixture stored`, added.ok, { error: added.error })) continue;
      const id = added.result.id;
      const before = (await tools.registry()).find((rule) => rule.id === id);
      const label = index % 2 ? "phone" : "desktop";
      const { page, errors } = await openPage(browser, stub, label === "phone" ? { width: 360, height: 780 } : { width: 1280, height: 900 });
      await page.click(`#tools-mocks [data-mock-action="edit"][data-mock="${id}"]`);
      const metrics = await measure(page);
      check(`round trip ${name}: the filled form fits at ${label} width`, layoutOk(metrics), { metrics });
      if (name === "every_kind") {
        await shoot(page, `${label}-form-edit-every-kind`, { form: true });
        const shown = await page.evaluate(() => JSON.stringify([...document.querySelectorAll("#tools-mock-form input,#tools-mock-form textarea")].map((node) => node.value)));
        check("round trip every_kind: no secret is shown in the form", !/abc123|resp-secret/.test(shown));
      }
      const updates = stub.ops("mocks.update").length;
      await page.click("#tools-mock-save");
      await page.waitForFunction(() => /saved/.test(document.getElementById("tools-msg-mocks").textContent));
      const [update] = stub.ops("mocks.update").slice(updates).map((entry) => entry.payload);
      const after = (await tools.registry()).find((rule) => rule.id === id);
      const same = JSON.stringify(withoutStamp(after)) === JSON.stringify(withoutStamp(before));
      check(`round trip ${name}: saving unchanged stores the same rule`, same && Boolean(after.updated_at), {
        payload: update, ...(same ? {} : { before, after }),
      });
      check(`round trip ${name}: the update carries no secret and no list`, !/abc123|resp-secret/.test(JSON.stringify(update))
        && !["query", "request_headers", "body_contains", "body_json", "remove_headers", "json_set", "json_remove", "replace"].some((key) => key in update), { payload: update });
      check(`round trip ${name}: no script errors`, errors.length === 0, { errors });
      await page.close();
    }

    // 4. Invalid values at both widths.
    for (const [label, viewport] of [["desktop", { width: 1280, height: 900 }], ["phone", { width: 360, height: 780 }]]) {
      const { page } = await openPage(browser, stub, viewport);
      const adds = stub.ops("mocks.add").length;
      await fillInvalid(page);
      const state = await page.evaluate(() => {
        const slotOf = (node) => document.getElementById(node.getAttribute("aria-describedby") || "");
        return [...document.querySelectorAll("#tools-mock-form [aria-invalid=true]")].map((node) => {
          const slot = slotOf(node);
          return { field: node.id || node.getAttribute("aria-label"), message: slot && !slot.hidden ? slot.textContent : null,
            beside: Boolean(slot) && (slot.parentElement === node.parentElement || slot.previousElementSibling === node || node.closest(".tools-mock-response") === slot.previousElementSibling) };
        });
      });
      const fields = state.map((item) => item.field).sort();
      check(`${label}: invalid values are marked with a message beside each field`, JSON.stringify(fields) === JSON.stringify(["Request body JSON path", "Request header name", "tools-mock-status"])
        && state.every((item) => item.message && item.beside), { state });
      check(`${label}: no call is made for invalid values`, stub.ops("mocks.add").length === adds);
      const metrics = await measure(page);
      check(`${label}: the invalid state fits`, layoutOk(metrics), { metrics });
      await shoot(page, `${label}-form-invalid`, { form: true });
      await page.close();
    }
  } catch (error) {
    check("the run finished", false, { error: String(error && error.stack || error) });
  } finally {
    if (browser) await browser.close().catch(() => {});
    await tools.stop();
    await rm(home, { recursive: true, force: true });
  }
  const result = { ok: checks.every((item) => item.ok), checks, screenshots, out: OUT };
  await writeFile(join(OUT, "result.json"), JSON.stringify(result, null, 2) + "\n");
  console.log(JSON.stringify(result, null, 2));
  process.exitCode = result.ok ? 0 : 1;
}

await main();
