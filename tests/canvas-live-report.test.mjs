import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { withoutTypedText } from "./live/live-report.mjs";

test("withoutTypedText replaces each typed word in any case, and leaves other text alone", () => {
  assert.equal(withoutTypedText('exit 1: {"nodes":[{"text":"Bluetooth"}]} bluetooth BLUETOOTH', ["bluetooth"]),
    'exit 1: {"nodes":[{"text":"[typed text]"}]} [typed text] [typed text]');
  assert.equal(withoutTypedText("a.b a+b", ["a.b", "", "a+b"]), "[typed text] [typed text]");
  assert.equal(withoutTypedText("axb", ["a.b"]), "axb", "a word is matched literally, not as a pattern");
  assert.equal(withoutTypedText(undefined, ["x"]), "");
});

test("the iOS live script records ui tree errors without the typed words and reports a copy taken before typing", () => {
  const source = readFileSync(new URL("./live/canvas_ios_live.mjs", import.meta.url), "utf8");
  assert.match(source, /const output = withoutTypedText\(\(tree\.stderr \|\| tree\.stdout\)\.trim\(\), \[SEARCH_TEXT, CLIP_TEXT\]\);/);
  assert.match(source, /uiTreeErrors\.push\(`\$\{why\}: \$\{output\.slice\(0, 300\)\}`\)/);
  assert.match(source, /report\.ui_tree_errors = \[\.\.\.uiTreeErrors\]/);
});
