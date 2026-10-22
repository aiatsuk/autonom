/** A Fold size must not inherit the running phone AVD camera inset. */
export class DisplayChrome {
  constructor(run, isEmulator) {
    this.run = run;
    this.isEmulator = isEmulator;
    this.original = null;
    this.disabled = new Set();
  }

  async apply(preset, assertActive = () => {}) {
    const fold = preset === "pixel-fold" || preset === "pixel-fold-closed";
    if (!this.isEmulator || (!fold && this.original === null)) return;
    if (this.original === null) {
      const { stdout } = await this.run(["shell", "cmd", "overlay", "list", "--user", "0"]);
      // Only built-in emulator model artwork; never touch application/user overlays.
      this.original = [...stdout.matchAll(/^\[x\] (com\.android\.(?:internal|systemui)\.emulation\.pixel_[a-z0-9_]+)\s*$/gm)].map((match) => match[1]).filter((name) => !name.includes("fold"));
    }
    for (const name of this.original) {
      assertActive();
      if (fold === this.disabled.has(name)) continue;
      // Record before dispatch: a timed-out adb command may still have applied.
      if (fold) this.disabled.add(name);
      await this.run(["shell", "cmd", "overlay", fold ? "disable" : "enable", "--user", "0", name]);
      if (!fold) this.disabled.delete(name);
    }
  }

  async restore() {
    const failures = [];
    for (const name of this.disabled) {
      try {
        await this.run(["shell", "cmd", "overlay", "enable", "--user", "0", name]);
        this.disabled.delete(name);
      } catch (error) { failures.push(error.message); }
    }
    if (failures.length) throw new Error("Display artwork restore failed: " + failures.join("; "));
  }
}
