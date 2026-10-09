// The Canvas workspace: its tabs (each a split of 1-4 tiles with its own /c/<id> URL), where
// each device is placed, and the file that keeps them across restarts. Also the state base
// the Canvas files live under, the one-Canvas-per-workspace lock and atomic JSON writes.
import { randomBytes } from "node:crypto";
import {
  chmodSync, closeSync, mkdirSync, openSync, readFileSync, renameSync, rmSync, writeFileSync, writeSync,
} from "node:fs";
import { chmod, mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

import { CanvasError, deviceIdFor } from "./canvas-devices.mjs";
import { StructuredError } from "./ios-idb-companion.mjs";

export const MAX_TABS = 8;
export const MAX_TILES = 4;
export const WORKSPACE_SCHEMA = "autonom-canvas-workspace/v1";
export const WORKSPACE_NAME_PATTERN = /^[A-Za-z0-9._-]{1,40}$/;
export const TAB_ID_PATTERN = /^t_[0-9a-f]{8}$/;
export const SAVE_DEBOUNCE_MS = 500;
const TAB_NAME_MAX = 40;
// Control characters (C0, DEL, C1) are not allowed in a tab name.
const CONTROL_CHARACTERS = /[\u0000-\u001f\u007f-\u009f]/;

/** $AUTONOM_HOME, else $XDG_STATE_HOME/autonom, else ~/.local/state/autonom. */
export function stateBase(env = process.env) {
  if (env.AUTONOM_HOME) return env.AUTONOM_HOME;
  if (env.XDG_STATE_HOME) return join(env.XDG_STATE_HOME, "autonom");
  return join(env.HOME || homedir(), ".local", "state", "autonom");
}

export function workspacesDir(env = process.env) {
  return join(stateBase(env), "canvas", "workspaces");
}

export function workspaceFile(name, env = process.env) {
  return join(workspacesDir(env), `${name}.json`);
}

export function workspaceLockFile(name, env = process.env) {
  return join(workspacesDir(env), `${name}.lock`);
}

/** A directory made (or tightened) to 0700. */
export function ensurePrivateDirSync(directory) {
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  try { chmodSync(directory, 0o700); } catch {}
}

/** Write JSON through a temporary file and a rename, file 0600, directory 0700. */
export function writeJsonAtomicSync(path, value) {
  ensurePrivateDirSync(dirname(path));
  const temp = `${path}.tmp-${process.pid}-${randomBytes(4).toString("hex")}`;
  writeFileSync(temp, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  try {
    renameSync(temp, path);
  } catch (error) {
    rmSync(temp, { force: true });
    throw error;
  }
}

export async function writeJsonAtomic(path, value) {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  try { await chmod(dirname(path), 0o700); } catch {}
  const temp = `${path}.tmp-${process.pid}-${randomBytes(4).toString("hex")}`;
  await writeFile(temp, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600 });
  try {
    await rename(temp, path);
  } catch (error) {
    await rm(temp, { force: true });
    throw error;
  }
}

/** Whether a process id is alive (a process of another user counts as alive). */
export function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === "EPERM";
  }
}

/**
 * Take the lock of one workspace: a file created with O_EXCL holding {pid, port}. A lock
 * whose pid is dead is replaced; a live one refuses with workspace_in_use. Returns
 * {path, update(port), release()}.
 */
export function acquireWorkspaceLock(path, { pid = process.pid, port = null, isAlive = pidAlive } = {}) {
  ensurePrivateDirSync(dirname(path));
  for (let attempt = 0; attempt < 3; attempt += 1) {
    let fd;
    try {
      fd = openSync(path, "wx", 0o600);
    } catch (error) {
      if (error?.code !== "EEXIST") throw error;
      let holder = null;
      try {
        holder = JSON.parse(readFileSync(path, "utf8"));
      } catch {}
      if (holder && Number.isInteger(holder.pid) && holder.pid !== pid && isAlive(holder.pid)) {
        throw new StructuredError("workspace_in_use",
          `Workspace ${workspaceNameOf(path)} is already open in the Canvas on port ${holder.port ?? "unknown"}`, {
            hint: "Open that Canvas, stop it with `autonom canvas stop`, or pass --workspace with another name.",
            port: holder.port ?? null, pid: holder.pid,
          });
      }
      // Dead (or unreadable, or our own) holder: replace it.
      rmSync(path, { force: true });
      continue;
    }
    try {
      writeSync(fd, JSON.stringify({ pid, port }));
    } finally {
      closeSync(fd);
    }
    let released = false;
    return {
      path,
      update(nextPort) {
        if (released) return;
        writeJsonAtomicSync(path, { pid, port: nextPort });
      },
      release() {
        if (released) return;
        released = true;
        try {
          const holder = JSON.parse(readFileSync(path, "utf8"));
          if (holder.pid !== pid) return;
        } catch {}
        rmSync(path, { force: true });
      },
    };
  }
  throw new StructuredError("workspace_in_use", `Could not take the lock ${path}`, { hint: "Try again." });
}

function workspaceNameOf(path) {
  const base = path.split(/[\\/]/).pop() ?? "";
  return base.replace(/\.lock$/, "");
}

/** A trimmed tab name: 1-40 characters without control characters, else invalid_value. */
export function cleanTabName(value) {
  if (typeof value !== "string") throw new CanvasError(400, "invalid_value", "name must be a string");
  const name = value.trim();
  if (!name || [...name].length > TAB_NAME_MAX || CONTROL_CHARACTERS.test(name)) {
    throw new CanvasError(400, "invalid_value", `name must be 1-${TAB_NAME_MAX} characters without control characters`);
  }
  return name;
}

function cleanLayout(value) {
  if (!Number.isInteger(value) || value < 1 || value > MAX_TILES) {
    throw new CanvasError(400, "invalid_value", `layout must be an integer from 1 to ${MAX_TILES}`);
  }
  return value;
}

function newTabId() {
  return `t_${randomBytes(4).toString("hex")}`;
}

/** The tabs of one workspace and where every device is placed. */
export class Workspace {
  #name;
  #file;
  #now;
  #newId;
  #log;
  #tabs = [];
  #active = null;
  #refs = new Map();
  #revision = 0;
  #saveTimer = null;
  #saving = Promise.resolve();
  #closed = false;

  constructor({ name, file = null, now = () => new Date(), newId = newTabId, log = (line) => console.error(line) }) {
    if (!WORKSPACE_NAME_PATTERN.test(name ?? "")) {
      throw new CanvasError(400, "invalid_value", "workspace name must be 1-40 letters, digits, . _ -");
    }
    this.#name = name;
    this.#file = file;
    this.#now = now;
    this.#newId = newId;
    this.#log = log;
  }

  /** Open a workspace: read its file (unless `file` is null) and make sure one tab exists. */
  static async open({ name, file = null, now, newId, log } = {}) {
    const workspace = new Workspace({ name, file, now, newId, log });
    if (file) await workspace.#load();
    if (!workspace.#tabs.length) {
      workspace.#addTab({ name: null, layout: 1 });
      workspace.#active = workspace.#tabs[0].id;
      workspace.#changed();
    }
    return workspace;
  }

  get name() {
    return this.#name;
  }

  get revision() {
    return this.#revision;
  }

  get persisted() {
    return this.#file !== null;
  }

  get file() {
    return this.#file;
  }

  tabs() {
    return this.#tabs.map((tab) => this.#view(tab));
  }

  tab(id) {
    const tab = this.#find(id);
    return tab ? this.#view(tab) : null;
  }

  activeTab() {
    return this.tab(this.#active);
  }

  createTab({ name, layout = 1 } = {}) {
    if (this.#tabs.length >= MAX_TABS) {
      throw new CanvasError(409, "tab_limit", `A workspace holds at most ${MAX_TABS} tabs`, "Close a tab first.");
    }
    const clean = name === undefined || name === null ? null : cleanTabName(name);
    const tab = this.#addTab({ name: clean, layout: cleanLayout(layout) });
    this.#changed();
    return this.#view(tab);
  }

  updateTab(id, { name, layout } = {}) {
    const tab = this.#require(id);
    const nextName = name === undefined ? tab.name : cleanTabName(name);
    let slots = tab.slots;
    if (layout !== undefined) {
      const nextLayout = cleanLayout(layout);
      const filled = tab.slots.filter(Boolean);
      if (nextLayout < filled.length) {
        throw new CanvasError(409, "layout_too_small",
          `Tab ${tab.name} holds ${filled.length} devices; layout ${nextLayout} has fewer tiles`,
          "Detach or move devices first.");
      }
      if (nextLayout !== tab.layout) {
        slots = tab.slots.length <= nextLayout
          ? [...tab.slots, ...Array(nextLayout - tab.slots.length).fill(null)]
          : [...filled, ...Array(nextLayout - filled.length).fill(null)];
      }
      tab.layout = nextLayout;
    }
    tab.name = nextName;
    tab.slots = slots;
    this.#changed();
    return this.#view(tab);
  }

  /** Remove a tab; returns its device ids. The last tab is replaced by a fresh "Canvas 1". */
  closeTab(id) {
    const tab = this.#require(id);
    const devices = tab.slots.filter(Boolean).map((slot) => slot.device);
    for (const device of devices) this.#refs.delete(device);
    const index = this.#tabs.indexOf(tab);
    this.#tabs.splice(index, 1);
    let created = null;
    if (!this.#tabs.length) {
      created = this.#addTab({ name: null, layout: 1 });
    }
    if (this.#active === id) {
      this.#active = (this.#tabs[Math.min(index, this.#tabs.length - 1)] ?? this.#tabs[0]).id;
    }
    this.#changed();
    return { closed: id, devices, created: created ? this.#view(created) : null };
  }

  activate(id) {
    const tab = this.#require(id);
    if (this.#active !== id) {
      this.#active = id;
      this.#changed();
    }
    return this.#view(tab);
  }

  /**
   * Place a device: `tab` defaults to the active tab, `slot` to its first empty slot.
   * A device already in this tab keeps its place; one in another tab is refused.
   */
  place(ref, { tab: tabId, slot } = {}) {
    const id = ref.id ?? deviceIdFor(ref.platform, ref.target);
    const tab = this.#require(tabId ?? this.#active);
    const where = this.#locate(id);
    if (where) {
      if (where.tab.id === tab.id) return { tab: tab.id, slot: where.slot };
      throw new CanvasError(409, "device_in_other_tab", `Device ${id} is in tab ${where.tab.name}`,
        "Move it here instead.", { device: id, tab: where.tab.id });
    }
    const index = this.#slotFor(tab, slot);
    tab.slots[index] = { device: id, present: ref.present ?? true };
    this.#refs.set(id, { id, platform: ref.platform, target: ref.target, name: ref.name ?? ref.target });
    this.#changed();
    return { tab: tab.id, slot: index };
  }

  /** Move a placed device to another tab or slot. */
  move(id, { tab: tabId, slot } = {}) {
    const where = this.#locate(id);
    if (!where) throw new CanvasError(404, "device_not_found", `Device ${id} is not placed in this workspace`);
    const tab = this.#require(tabId);
    if (where.tab === tab && (slot === undefined || slot === null || slot === where.slot)) {
      return { tab: tab.id, slot: where.slot };
    }
    const index = this.#slotFor(tab, slot);
    const entry = where.tab.slots[where.slot];
    where.tab.slots[where.slot] = null;
    tab.slots[index] = entry;
    this.#changed();
    return { tab: tab.id, slot: index };
  }

  /** Empty the slot of a device (detach): no absent ref is kept. */
  remove(id) {
    const where = this.#locate(id);
    this.#refs.delete(id);
    if (!where) return false;
    where.tab.slots[where.slot] = null;
    this.#changed();
    return true;
  }

  /** Clear one slot that holds an absent ref; a present device is refused (slot_live). */
  clearSlot(tabId, slot) {
    const tab = this.#require(tabId);
    if (!Number.isInteger(slot) || slot < 0 || slot >= tab.layout) {
      throw new CanvasError(400, "invalid_value", `slot must be an integer from 0 to ${tab.layout - 1}`);
    }
    const entry = tab.slots[slot];
    if (entry?.present) {
      throw new CanvasError(409, "slot_live", `Device ${entry.device} is attached in this slot`, "Detach it instead.");
    }
    if (entry) {
      tab.slots[slot] = null;
      this.#refs.delete(entry.device);
      this.#changed();
    }
    return this.#view(tab);
  }

  /** Mark a placed device present (attached) or absent (its target is not running). */
  setPresent(id, present) {
    const where = this.#locate(id);
    if (!where) return false;
    const entry = where.tab.slots[where.slot];
    if (entry.present === Boolean(present)) return true;
    entry.present = Boolean(present);
    this.#revision += 1;
    try {
      this.onChange?.(this);
    } catch {}
    return true;
  }

  locate(id) {
    const where = this.#locate(id);
    return where ? { tab: where.tab.id, slot: where.slot } : null;
  }

  ref(id) {
    return this.#refs.get(id) ?? null;
  }

  /** Every placed device with its tab and slot, in tab and slot order. */
  refs() {
    const out = [];
    for (const tab of this.#tabs) {
      tab.slots.forEach((entry, slot) => {
        if (!entry) return;
        const ref = this.#refs.get(entry.device) ?? { id: entry.device };
        out.push({ ...ref, present: entry.present, tab: tab.id, slot });
      });
    }
    return out;
  }

  /** The first tab with an empty slot, from the active tab on, or null. */
  firstTabWithRoom() {
    const ordered = [this.#find(this.#active), ...this.#tabs.filter((tab) => tab.id !== this.#active)];
    return ordered.find((tab) => tab && tab.slots.some((slot) => slot === null))?.id ?? null;
  }

  /** Write the file now (no-op for an ephemeral workspace). */
  async flush() {
    clearTimeout(this.#saveTimer);
    this.#saveTimer = null;
    if (!this.#file) return;
    this.#saving = this.#saving.catch(() => {}).then(() => writeJsonAtomic(this.#file, this.#serialize()));
    return this.#saving;
  }

  /** Write the file now, synchronously (process exit paths). */
  flushSync() {
    clearTimeout(this.#saveTimer);
    this.#saveTimer = null;
    if (!this.#file) return;
    writeJsonAtomicSync(this.#file, this.#serialize());
  }

  /** Stop scheduling saves (after the final flush). */
  close() {
    this.#closed = true;
    clearTimeout(this.#saveTimer);
    this.#saveTimer = null;
  }

  #serialize() {
    return {
      schema: WORKSPACE_SCHEMA,
      name: this.#name,
      updated_at: this.#now().toISOString(),
      active_tab: this.#active,
      tabs: this.#tabs.map((tab) => ({
        id: tab.id,
        name: tab.name,
        layout: tab.layout,
        created_at: tab.created_at,
        slots: tab.slots.map((entry) => {
          if (!entry) return null;
          const ref = this.#refs.get(entry.device);
          if (!ref) return null;
          return { platform: ref.platform, target: ref.target, name: ref.name ?? null };
        }),
      })),
    };
  }

  async #load() {
    let text;
    try {
      text = await readFile(this.#file, "utf8");
    } catch (error) {
      if (error?.code === "ENOENT") return;
      this.#quarantine(`could not be read (${error.message})`);
      return;
    }
    try {
      this.#restore(JSON.parse(text));
    } catch (error) {
      this.#tabs = [];
      this.#refs.clear();
      this.#active = null;
      this.#quarantine(`is not a valid workspace file (${error.message})`);
    }
  }

  #quarantine(why) {
    const target = `${this.#file}.corrupt-${Date.now()}`;
    try {
      renameSync(this.#file, target);
      this.#log(`Canvas workspace file ${this.#file} ${why}; moved it to ${target} and started empty`);
    } catch {
      this.#log(`Canvas workspace file ${this.#file} ${why}; started empty`);
    }
  }

  #restore(data) {
    if (!data || typeof data !== "object" || data.schema !== WORKSPACE_SCHEMA) throw new Error("unknown schema");
    if (!Array.isArray(data.tabs) || data.tabs.length > MAX_TABS) throw new Error("tabs must be a list of at most 8");
    const seenTabs = new Set();
    const tabs = [];
    const refs = new Map();
    for (const raw of data.tabs) {
      if (!raw || typeof raw !== "object" || !TAB_ID_PATTERN.test(raw.id ?? "") || seenTabs.has(raw.id)) {
        throw new Error("a tab has an invalid or repeated id");
      }
      seenTabs.add(raw.id);
      const layout = cleanLayout(raw.layout);
      if (!Array.isArray(raw.slots) || raw.slots.length !== layout) throw new Error("slots do not match the layout");
      const slots = raw.slots.map((slot) => {
        if (slot === null) return null;
        if (!slot || typeof slot !== "object") throw new Error("a slot is invalid");
        const id = deviceIdFor(slot.platform, slot.target);
        if (refs.has(id)) throw new Error(`device ${id} is placed twice`);
        const name = typeof slot.name === "string" && slot.name ? slot.name.slice(0, 200) : slot.target;
        refs.set(id, { id, platform: slot.platform, target: slot.target, name });
        return { device: id, present: false };
      });
      tabs.push({
        id: raw.id,
        name: cleanTabName(raw.name),
        layout,
        slots,
        created_at: typeof raw.created_at === "string" ? raw.created_at : this.#now().toISOString(),
      });
    }
    this.#tabs = tabs;
    this.#refs = refs;
    this.#active = tabs.some((tab) => tab.id === data.active_tab) ? data.active_tab : (tabs[0]?.id ?? null);
  }

  #addTab({ name, layout }) {
    let id = this.#newId();
    while (this.#find(id)) id = this.#newId();
    const tab = {
      id, name: name ?? this.#defaultName(), layout, slots: Array(layout).fill(null),
      created_at: this.#now().toISOString(),
    };
    this.#tabs.push(tab);
    return tab;
  }

  #defaultName() {
    const taken = new Set(this.#tabs.map((tab) => tab.name));
    for (let n = 1; ; n += 1) if (!taken.has(`Canvas ${n}`)) return `Canvas ${n}`;
  }

  #find(id) {
    return this.#tabs.find((tab) => tab.id === id) ?? null;
  }

  #require(id) {
    const tab = this.#find(id);
    if (!tab) throw new CanvasError(404, "tab_not_found", `No tab ${id} in this workspace`);
    return tab;
  }

  #locate(id) {
    for (const tab of this.#tabs) {
      const slot = tab.slots.findIndex((entry) => entry?.device === id);
      if (slot >= 0) return { tab, slot };
    }
    return null;
  }

  #slotFor(tab, slot) {
    if (slot === undefined || slot === null) {
      const index = tab.slots.indexOf(null);
      if (index < 0) {
        throw new CanvasError(409, "tab_full", `Tab ${tab.name} has no empty tile`,
          "Pick a larger layout, another tab, or a new tab.", { tab: tab.id });
      }
      return index;
    }
    if (!Number.isInteger(slot) || slot < 0 || slot >= MAX_TILES) {
      throw new CanvasError(400, "invalid_value", `slot must be an integer from 0 to ${MAX_TILES - 1}`);
    }
    if (slot >= tab.layout) {
      throw new CanvasError(409, "tab_full", `Tab ${tab.name} has ${tab.layout} tiles`,
        "Pick a larger layout first.", { tab: tab.id });
    }
    if (tab.slots[slot] !== null) {
      throw new CanvasError(409, "slot_taken", `Tile ${slot + 1} of tab ${tab.name} is taken`,
        "Pick an empty tile.", { tab: tab.id, slot });
    }
    return slot;
  }

  #view(tab) {
    return {
      id: tab.id,
      name: tab.name,
      layout: tab.layout,
      slots: tab.slots.map((entry) => (entry ? { device: entry.device, present: entry.present } : null)),
      created_at: tab.created_at,
      url: `/c/${tab.id}`,
    };
  }

  #changed() {
    this.#revision += 1;
    try {
      this.onChange?.(this);
    } catch {}
    if (!this.#file || this.#closed) return;
    clearTimeout(this.#saveTimer);
    this.#saveTimer = setTimeout(() => {
      this.#saveTimer = null;
      this.flush().catch((error) => this.#log(`Canvas workspace file could not be saved: ${error.message}`));
    }, SAVE_DEBOUNCE_MS);
    this.#saveTimer.unref?.();
  }
}
