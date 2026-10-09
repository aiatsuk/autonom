// Unit tests of canvas-workspace.mjs: tabs, placing and moving devices, limits, the
// workspace file (round trip, corrupt file, ephemeral) and the one-Canvas lock. Every file
// goes to a temporary directory.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, readdirSync, statSync } from "node:fs";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  MAX_TABS,
  MAX_TILES,
  WORKSPACE_SCHEMA,
  Workspace,
  acquireWorkspaceLock,
  stateBase,
  workspaceFile,
  workspaceLockFile,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-workspace.mjs";

const A = { id: "android~emulator-5580", platform: "android", target: "emulator-5580", name: "Pixel" };
const B = { id: "ios~45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", platform: "ios",
  target: "45A342A1-AA2F-4F40-8EFD-F0D0C590ECE5", name: "iPhone 17" };
const C = { id: "android~emulator-5584", platform: "android", target: "emulator-5584", name: "Split" };

async function tempDir(t) {
  const directory = await mkdtemp(join(tmpdir(), "autonom-workspace-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  return directory;
}

function ids() {
  let n = 0;
  return () => `t_${(++n).toString(16).padStart(8, "0")}`;
}

test("the state base follows AUTONOM_HOME, then XDG_STATE_HOME, then ~/.local/state", () => {
  assert.equal(stateBase({ AUTONOM_HOME: "/x", XDG_STATE_HOME: "/y", HOME: "/h" }), "/x");
  assert.equal(stateBase({ XDG_STATE_HOME: "/y", HOME: "/h" }), "/y/autonom");
  assert.equal(stateBase({ HOME: "/h" }), "/h/.local/state/autonom");
  assert.equal(workspaceFile("default", { AUTONOM_HOME: "/x" }), "/x/canvas/workspaces/default.json");
  assert.equal(workspaceLockFile("w", { AUTONOM_HOME: "/x" }), "/x/canvas/workspaces/w.lock");
});

test("tabs: a new workspace has Canvas 1; create, rename, layout and the limits", async () => {
  const workspace = await Workspace.open({ name: "default", newId: ids() });
  assert.equal(workspace.persisted, false);
  const [first] = workspace.tabs();
  assert.deepEqual({ ...first, created_at: null },
    { id: "t_00000001", name: "Canvas 1", layout: 1, slots: [null], created_at: null, url: "/c/t_00000001" });
  assert.equal(workspace.activeTab().id, first.id);
  const second = workspace.createTab({ layout: 2 });
  assert.equal(second.name, "Canvas 2");
  assert.deepEqual(second.slots, [null, null]);
  const named = workspace.createTab({ name: "  Phones  ", layout: 4 });
  assert.equal(named.name, "Phones");
  const before = workspace.revision;
  assert.equal(workspace.updateTab(second.id, { name: "Tablets" }).name, "Tablets");
  assert.ok(workspace.revision > before, "every change increments the revision");
  for (const bad of ["", "   ", "x".repeat(41), "bad\u0007name", 7]) {
    assert.throws(() => workspace.updateTab(second.id, { name: bad }), { code: "invalid_value", status: 400 });
  }
  for (const bad of [0, 5, 1.5, "2"]) {
    assert.throws(() => workspace.createTab({ layout: bad }), { code: "invalid_value" });
  }
  assert.throws(() => workspace.updateTab("t_ffffffff", { name: "x" }), { code: "tab_not_found", status: 404 });
  while (workspace.tabs().length < MAX_TABS) workspace.createTab();
  assert.throws(() => workspace.createTab(), { code: "tab_limit", status: 409 });
  assert.equal(MAX_TILES, 4);
  // The default name takes the smallest unused number.
  const fresh = await Workspace.open({ name: "x", newId: ids() });
  const two = fresh.createTab();
  fresh.createTab();
  fresh.closeTab(two.id);
  assert.equal(fresh.createTab().name, "Canvas 2");
});

test("place, move, remove: one tab per device, full tabs and taken slots", async () => {
  const workspace = await Workspace.open({ name: "w", newId: ids() });
  const one = workspace.activeTab();
  const two = workspace.createTab({ layout: 2 });
  assert.deepEqual(workspace.place(A), { tab: one.id, slot: 0 }, "the active tab's first empty slot");
  assert.deepEqual(workspace.place(A), { tab: one.id, slot: 0 }, "placing again returns its place");
  assert.throws(() => workspace.place(B), { code: "tab_full", status: 409 });
  assert.throws(() => workspace.place(A, { tab: two.id }), (error) => error.code === "device_in_other_tab" &&
    error.extra.device === A.id && error.extra.tab === one.id);
  assert.deepEqual(workspace.place(B, { tab: two.id, slot: 1 }), { tab: two.id, slot: 1 });
  assert.throws(() => workspace.place(C, { tab: two.id, slot: 1 }), { code: "slot_taken" });
  assert.throws(() => workspace.place(C, { tab: one.id, slot: 3 }), { code: "tab_full" });
  assert.throws(() => workspace.place(C, { tab: "t_00000099" }), { code: "tab_not_found" });
  assert.deepEqual(workspace.move(A.id, { tab: two.id }), { tab: two.id, slot: 0 });
  assert.deepEqual(workspace.tab(one.id).slots, [null]);
  assert.deepEqual(workspace.tab(two.id).slots, [{ device: A.id, present: true }, { device: B.id, present: true }]);
  assert.throws(() => workspace.move(C.id, { tab: one.id }), { code: "device_not_found" });
  assert.throws(() => workspace.move(B.id, { tab: two.id, slot: 0 }), { code: "slot_taken" });
  assert.deepEqual(workspace.locate(B.id), { tab: two.id, slot: 1 });
  assert.equal(workspace.refs().length, 2);
  assert.equal(workspace.remove(A.id), true);
  assert.deepEqual(workspace.tab(two.id).slots, [null, { device: B.id, present: true }], "detach empties the slot");
  assert.equal(workspace.locate(A.id), null);
  assert.equal(workspace.ref(A.id), null, "no absent ref is kept");
});

test("layout changes keep filled slots in order and refuse to drop devices", async () => {
  const workspace = await Workspace.open({ name: "w", newId: ids() });
  const tab = workspace.createTab({ layout: 4 });
  workspace.place(A, { tab: tab.id, slot: 1 });
  workspace.place(B, { tab: tab.id, slot: 3 });
  assert.throws(() => workspace.updateTab(tab.id, { layout: 1 }), { code: "layout_too_small", status: 409 });
  const two = workspace.updateTab(tab.id, { layout: 2 });
  assert.deepEqual(two.slots.map((slot) => slot?.device ?? null), [A.id, B.id], "compacted in order");
  const three = workspace.updateTab(tab.id, { layout: 3 });
  assert.deepEqual(three.slots.map((slot) => slot?.device ?? null), [A.id, B.id, null]);
});

test("closing a tab returns its devices; closing the last tab makes a fresh Canvas 1", async () => {
  const workspace = await Workspace.open({ name: "w", newId: ids() });
  const first = workspace.activeTab();
  const second = workspace.createTab({ layout: 2, name: "Pair" });
  workspace.place(A, { tab: second.id });
  workspace.place(B, { tab: second.id });
  workspace.activate(second.id);
  const closed = workspace.closeTab(second.id);
  assert.deepEqual(closed, { closed: second.id, devices: [A.id, B.id], created: null });
  assert.equal(workspace.activeTab().id, first.id, "the active tab moves to a remaining one");
  assert.equal(workspace.locate(A.id), null);
  const last = workspace.closeTab(first.id);
  assert.equal(last.created.name, "Canvas 1");
  assert.deepEqual(workspace.tabs().map((tab) => tab.id), [last.created.id]);
  assert.equal(workspace.activeTab().id, last.created.id);
  assert.throws(() => workspace.activate(first.id), { code: "tab_not_found" });
});

test("slots: clear removes an absent ref and refuses a present device", async () => {
  const workspace = await Workspace.open({ name: "w", newId: ids() });
  const tab = workspace.createTab({ layout: 2 });
  workspace.place(A, { tab: tab.id });
  workspace.place({ ...B, present: false }, { tab: tab.id });
  assert.throws(() => workspace.clearSlot(tab.id, 0), { code: "slot_live", status: 409 });
  assert.deepEqual(workspace.clearSlot(tab.id, 1).slots, [{ device: A.id, present: true }, null]);
  assert.throws(() => workspace.clearSlot(tab.id, 5), { code: "invalid_value" });
  assert.throws(() => workspace.clearSlot("t_0000abcd", 0), { code: "tab_not_found" });
  workspace.setPresent(A.id, false);
  assert.deepEqual(workspace.tab(tab.id).slots[0], { device: A.id, present: false });
});

test("persistence: every field survives a round trip, files are private, absent on reload", async (t) => {
  const directory = await tempDir(t);
  const file = join(directory, "canvas", "workspaces", "default.json");
  const workspace = await Workspace.open({ name: "default", file, newId: ids() });
  const first = workspace.activeTab();
  const second = workspace.createTab({ name: "Pair", layout: 3 });
  workspace.place(A, { tab: first.id });
  workspace.place(B, { tab: second.id, slot: 2 });
  workspace.place({ ...C, present: false }, { tab: second.id, slot: 0 });
  workspace.activate(second.id);
  await workspace.flush();
  assert.equal(statSync(file).mode & 0o777, 0o600);
  assert.equal(statSync(join(directory, "canvas", "workspaces")).mode & 0o777, 0o700);
  const saved = JSON.parse(await readFile(file, "utf8"));
  assert.equal(saved.schema, WORKSPACE_SCHEMA);
  assert.equal(saved.name, "default");
  assert.equal(saved.active_tab, second.id);
  assert.ok(!Number.isNaN(Date.parse(saved.updated_at)));
  assert.deepEqual(saved.tabs.map(({ created_at: created, ...rest }) => { assert.ok(created); return rest; }), [
    { id: first.id, name: "Canvas 1", layout: 1, slots: [{ platform: "android", target: A.target, name: "Pixel" }] },
    { id: second.id, name: "Pair", layout: 3, slots: [
      { platform: "android", target: C.target, name: "Split" }, null,
      { platform: "ios", target: B.target, name: "iPhone 17" }] },
  ]);
  assert.ok(!JSON.stringify(saved).includes("token") && !JSON.stringify(saved).includes("session"));

  const again = await Workspace.open({ name: "default", file });
  assert.deepEqual(again.tabs().map(({ id, name, layout, created_at: created, url }) => ({ id, name, layout, created, url })),
    workspace.tabs().map(({ id, name, layout, created_at: created, url }) => ({ id, name, layout, created, url })));
  assert.equal(again.activeTab().id, second.id);
  assert.deepEqual(again.tab(second.id).slots,
    [{ device: C.id, present: false }, null, { device: B.id, present: false }], "restored devices start absent");
  assert.deepEqual(again.ref(B.id), { id: B.id, platform: "ios", target: B.target, name: "iPhone 17" });
  // Absent refs that were never removed keep being saved.
  again.createTab();
  await again.flush();
  const resaved = JSON.parse(await readFile(file, "utf8"));
  assert.equal(resaved.tabs[1].slots[2].target, B.target);
  assert.equal(resaved.tabs.length, 3);
});

test("persistence: saves are debounced and flush writes at once", async (t) => {
  const directory = await tempDir(t);
  const file = join(directory, "w.json");
  const workspace = await Workspace.open({ name: "w", file });
  await workspace.flush();
  workspace.createTab({ name: "Later" });
  const quick = JSON.parse(await readFile(file, "utf8"));
  assert.equal(quick.tabs.length, 1, "not written at once");
  await new Promise((resolvePromise) => setTimeout(resolvePromise, 700));
  const later = JSON.parse(await readFile(file, "utf8"));
  assert.equal(later.tabs.length, 2, "written after the debounce");
});

test("persistence: a corrupt file is moved aside and the workspace starts empty", async (t) => {
  const directory = await tempDir(t);
  for (const content of ["{not json", JSON.stringify({ schema: "other" }),
    JSON.stringify({ schema: WORKSPACE_SCHEMA, tabs: [{ id: "bad", name: "x", layout: 1, slots: [null] }] }),
    JSON.stringify({ schema: WORKSPACE_SCHEMA, tabs: [{ id: "t_00000001", name: "x", layout: 2, slots: [null] }] }),
    JSON.stringify({ schema: WORKSPACE_SCHEMA, tabs: [
      { id: "t_00000001", name: "x", layout: 1, slots: [{ platform: "android", target: "a" }] },
      { id: "t_00000002", name: "y", layout: 1, slots: [{ platform: "android", target: "a" }] }] })]) {
    const file = join(directory, "default.json");
    await writeFile(file, content);
    const lines = [];
    const workspace = await Workspace.open({ name: "default", file, log: (line) => lines.push(line) });
    assert.equal(workspace.tabs().length, 1);
    assert.equal(workspace.tabs()[0].name, "Canvas 1");
    assert.equal(lines.length, 1);
    assert.match(lines[0], /corrupt-\d+/);
    const moved = readdirSync(directory).filter((name) => name.startsWith("default.json.corrupt-"));
    assert.equal(moved.length, 1, content);
    assert.equal(existsSync(file), false);
    await rm(join(directory, moved[0]));
  }
});

test("ephemeral: an ephemeral workspace neither reads nor writes a file", async (t) => {
  const directory = await tempDir(t);
  const workspace = await Workspace.open({ name: "default", file: null });
  workspace.place(A);
  await workspace.flush();
  workspace.flushSync();
  assert.deepEqual(readdirSync(directory), []);
  assert.equal(workspace.persisted, false);
});

test("lock: one Canvas per workspace; a dead holder is replaced", async (t) => {
  const directory = await tempDir(t);
  const path = join(directory, "canvas", "workspaces", "default.lock");
  const lock = acquireWorkspaceLock(path, { pid: process.pid, port: null });
  assert.equal(statSync(path).mode & 0o777, 0o600);
  lock.update(4321);
  assert.deepEqual(JSON.parse(await readFile(path, "utf8")), { pid: process.pid, port: 4321 });
  // Another live process holds it.
  const holder = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], { stdio: "ignore" });
  t.after(() => holder.kill("SIGKILL"));
  lock.release();
  assert.equal(existsSync(path), false);
  const theirs = acquireWorkspaceLock(path, { pid: holder.pid, port: 5555 });
  assert.throws(() => acquireWorkspaceLock(path, { pid: process.pid }), (error) =>
    error.code === "workspace_in_use" && error.extra.port === 5555 && error.extra.pid === holder.pid &&
    typeof error.hint === "string");
  void theirs;
  // The holder dies: the lock is stale and replaced.
  holder.kill("SIGKILL");
  await new Promise((resolvePromise) => holder.once("exit", resolvePromise));
  const mine = acquireWorkspaceLock(path, { pid: process.pid, port: 1 });
  assert.equal(JSON.parse(await readFile(path, "utf8")).pid, process.pid);
  mine.release();
  // An unreadable lock is replaced too.
  await writeFile(path, "garbage");
  acquireWorkspaceLock(path, { pid: process.pid }).release();
  assert.equal(existsSync(path), false);
});
