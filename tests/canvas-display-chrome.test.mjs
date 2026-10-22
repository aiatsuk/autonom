import test from "node:test";
import assert from "node:assert/strict";
import { DisplayChrome } from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-display-chrome.mjs";
const model = "com.android.internal.emulation.pixel_6_pro";
const systemui = "com.android.systemui.emulation.pixel_6_pro";
function fixture() {
  const calls = [];
  const run = async (args) => {
    calls.push(args);
    return {stdout: `[x] ${model}\n[x] ${systemui}\n[x] com.android.internal.emulation.pixel_fold\n[x] app.custom.overlay\n`};
  };
  return { calls, run, chrome: new DisplayChrome(run, true) };
}
test("Fold removes only active emulator model overlays, restores default, and restores on stop", async () => {
  const {chrome, calls} = fixture();
  await chrome.apply("pixel-10"); assert.equal(calls.length, 0);
  await chrome.apply("pixel-fold"); assert.equal(calls.length, 3);
  assert.deepEqual([...chrome.disabled], [model, systemui]);
  await chrome.apply("pixel-fold-closed"); assert.equal(calls.length, 3);
  await chrome.apply("default"); assert.equal(chrome.disabled.size, 0);
  assert.deepEqual(calls.slice(3).map(x => [x[3], x.at(-1)]), [["enable",model],["enable",systemui]]);
  await chrome.apply("pixel-fold"); await chrome.restore();
  assert.equal(chrome.disabled.size, 0);
  assert.ok(calls.every(x => !x.includes("app.custom.overlay")));
});
test("physical devices never change overlays", async () => {
  const chrome = new DisplayChrome(() => { throw new Error("Unexpected command"); }, false);
  await chrome.apply("pixel-fold"); await chrome.restore();
});
test("a timed-out disable remains tracked for restoration", async () => {
  const {run} = fixture(); let fail = true;
  const chrome = new DisplayChrome(async args => {
    if (args[3] === "disable" && fail) throw new Error("timeout");
    return run(args);
  }, true);
  await assert.rejects(chrome.apply("pixel-fold"), /timeout/);
  assert.deepEqual([...chrome.disabled], [model]);
  fail = false; await chrome.restore(); assert.equal(chrome.disabled.size, 0);
});
test("stop prevents further changes and restores all overlays despite one failure", async () => {
  const {run} = fixture(); let fail = false;
  const chrome = new DisplayChrome(async args => {
    if (fail && args[3] === "enable" && args.at(-1) === model) throw new Error("restore timeout");
    return run(args);
  }, true);
  await assert.rejects(chrome.apply("pixel-fold", () => {throw new Error("stopping");}), /stopping/);
  assert.equal(chrome.disabled.size, 0);
  await chrome.apply("pixel-fold"); fail = true;
  await assert.rejects(chrome.restore(), /restore timeout/);
  assert.deepEqual([...chrome.disabled], [model]);
  fail = false; await chrome.restore(); assert.equal(chrome.disabled.size, 0);
});
