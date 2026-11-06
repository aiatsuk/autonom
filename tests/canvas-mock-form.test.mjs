// Unit tests of the Canvas Mocks form module (MR-14): the pure fill / payload / summary
// functions, the markup and the inlined script as text, and a round trip of rules through the
// real tools process (scripts/autonom_canvas_tools.py) in a temporary AUTONOM_HOME: mocks ops
// never reach a device, so no device, Canvas or network is involved.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createInterface } from "node:readline";
import test, { describe } from "node:test";
import { Script } from "node:vm";

import {
  mockFormCore,
  mockFormController,
  mockFormMarkup,
  mockFormScript,
  mockFormStyles,
} from "../plugins/autonom/skills/android-emulator-browser/scripts/canvas-mock-form.mjs";

const ROOT = resolve(import.meta.dirname, "..");
const core = mockFormCore();
const ALL_KINDS = ["query", "request_headers", "body_contains", "body_json", "remove_headers", "json_set", "json_remove", "replace"];

/** A form with the given fields over the empty one; rows merge per kind. */
function form(fields = {}) {
  const base = core.empty();
  return { ...base, ...fields, rows: { ...base.rows, ...(fields.rows || {}) } };
}
const fieldsOf = (result) => result.errors.map((error) => [error.field, error.index, error.part]);

// --- markup, styles and script as text ---------------------------------------------------------

describe("markup, styles and script", () => {
  const markup = mockFormMarkup('<option value="">Any method</option><option value="GET">GET</option>');

  test("the form offers both modes, every row kind and a message slot beside each field", () => {
    assert.match(markup, /<input type="radio" name="tools-mock-mode" id="tools-mock-mode-respond" value="respond" checked><span>Fake response<\/span>/);
    assert.match(markup, /<input type="radio" name="tools-mock-mode" id="tools-mock-mode-rewrite" value="rewrite"><span>Edit real response<\/span>/);
    assert.match(markup, /<legend class="sr">Response source<\/legend>/);
    assert.match(markup, />Match request<\/h4>/);
    for (const kind of ALL_KINDS) assert.match(markup, new RegExp(`<button type="button" class="btn small" data-mock-add="${kind}">\\+ `), kind);
    for (const id of ["tools-mock-glob-error", "tools-mock-match-error", "tools-mock-conditions-error", "tools-mock-response-error",
      "tools-mock-headers-error", "tools-mock-remove-headers-error", "tools-mock-body-error", "tools-mock-edits-error", "tools-mock-note-error"]) {
      assert.match(markup, new RegExp(`<p class="tools-field-error" id="${id}" role="status" hidden></p>`), id);
    }
    assert.match(markup, /<select id="tools-mock-timing"[^>]*><option value="none">None<\/option><option value="delay">Delay<\/option><option value="hang" id="tools-mock-timing-hang">Hang<\/option><\/select>/);
    assert.match(markup, /<select id="tools-mock-body-mode"[^>]*><option value="keep">Keep the real body<\/option><option value="replace">Replace it whole<\/option><option value="edit">Edit fields or text<\/option><\/select>/);
    // Rewrite-only parts start hidden; the one submit button is Save.
    for (const id of ["tools-mock-rewrite-headers", "tools-mock-body-choice", "tools-mock-body-edits", "tools-mock-delay-field"]) {
      assert.match(markup, new RegExp(`id="${id}" hidden>`), id);
    }
    assert.equal(markup.match(/type="submit"/g).length, 1);
    assert.match(markup, /<button type="submit" class="btn primary" id="tools-mock-save" data-mutate>Add mock<\/button>/);
    const ids = [...markup.matchAll(/ id="([^"]+)"/g)].map((match) => match[1]);
    assert.equal(new Set(ids).size, ids.length, "ids are unique");
  });

  test("every field has a visible label, a wrapping label or an aria-label", () => {
    const labelled = new Set([...markup.matchAll(/<label[^>]* for="([^"]+)"/g)].map((match) => match[1]));
    for (const [tag, id, attributes] of [...markup.matchAll(/<(input|select|textarea)\b[^>]*?id="([^"]+)"([^>]*)>/g)].map((match) => [match[1], match[2], match[0]])) {
      const wrapped = new RegExp(`<label[^>]*><input[^>]*id="${id}"`).test(markup);
      assert.ok(labelled.has(id) || wrapped || /aria-label="/.test(attributes), `${tag}#${id} has a label`);
    }
  });

  test("styles use the drawer tokens, add no colors and keep rows inside 360 px", () => {
    const css = mockFormStyles();
    assert.doesNotMatch(css, /#[0-9a-f]{3,8}\b|rgba?\(/i, "no literal colors");
    for (const token of ["--fill", "--text-2", "--t-danger", "--tab-selected-bg", "--tab-shadow", "--r-sm", "--control-small"]) assert.ok(css.includes(`var(${token})`), token);
    assert.match(css, /\.tools-mock-row\{display:grid;grid-template-columns:46px minmax\(0,1fr\) minmax\(0,1fr\) var\(--control-small\)/);
    assert.match(css, /#tools-mocks \.tools-mock-badge\{/);
    assert.equal((css.match(/\{/g) || []).length, (css.match(/\}/g) || []).length, "braces balance");
  });

  test("the inlined script parses, has no backticks and never writes HTML", () => {
    const script = mockFormScript();
    assert.doesNotThrow(() => new Script(`(function(){${script}})`));
    assert.equal(script.includes("`"), false);
    assert.doesNotMatch(script, /innerHTML|outerHTML|insertAdjacentHTML|document\.write/);
    assert.equal(/<\/script/i.test(script), false);
    // The inlined core is the same code the tests run.
    const inlined = new Script(`(function(){${script};return MOCK_FORM})()`).runInNewContext({ TextEncoder });
    assert.equal(JSON.stringify(inlined.payload(form({ url_glob: "*/a" }), null)), JSON.stringify(core.payload(form({ url_glob: "*/a" }), null)));
    assert.equal(typeof mockFormController, "function");
  });
});

// --- values -----------------------------------------------------------------------------------

describe("values", () => {
  test("a JSON value is shown so that reading it back gives the same value", () => {
    for (const value of ["pro", "2", "", " 2", "true", "null", "<redacted>", 2, 2.5, -1, true, false, null, { a: [1, "x"] }, [1, 2], "a=b", "{bad"]) {
      const shown = core.formatValue(value);
      assert.deepEqual(core.parseValue(shown), value, JSON.stringify(value));
    }
    assert.equal(core.formatValue("pro"), "pro");
    assert.equal(core.formatValue("2"), '"2"', "a string that looks like a number is quoted");
    assert.equal(core.formatValue(2), "2");
  });

  test("headers parse as Name: value lines; rewrite names must be header tokens", () => {
    assert.deepEqual(core.parseHeaders("A: 1\n\nB:  two  \r\nA: 3", false), { headers: { A: "3", B: "two" } });
    assert.match(core.parseHeaders("not a header", false).error, /name, a colon and a value/);
    assert.match(core.parseHeaders(": x", false).error, /name, a colon/);
    assert.deepEqual(core.parseHeaders("Bad Name: x", false), { headers: { "Bad Name": "x" } });
    assert.match(core.parseHeaders("Bad Name: x", true).error, /valid header name/);
    const many = Array.from({ length: 65 }, (_, index) => `H${index}: v`).join("\n");
    assert.match(core.parseHeaders(many, false).error, /At most 64 headers/);
  });
});

// --- fill ---------------------------------------------------------------------------------------

const LISTED_REWRITE = {
  id: "m_3", enabled: true, mode: "rewrite", note: "edit the profile", hits: 2,
  match: {
    url_glob: "*/profile*", method: "GET", host: "api.example.com", ignore_query: false,
    query: [{ name: "page", value: "2" }, { name: "debug", value: null }, { name: "token", value: "<redacted>" }],
    headers: [{ name: "X-Client", value: "ios*" }, { name: "Authorization", value: "<redacted>" }],
    body: { contains: ["needle"], json: [{ path: "user.role", value: "admin" }, { path: "items.0.qty", value: 2 }, { path: "code", value: "2" }] },
  },
  response: { status: 503, headers: { "Retry-After": "5" }, body_path: null },
  rewrite: { remove_headers: ["ETag"], json_set: [{ path: "user.plan", value: "pro" }, { path: "auth.token", value: "<redacted>" }], json_remove: ["user.trial_ends"], replace: [{ find: "Free", with: "Pro" }] },
  delay_s: 1.5,
};

describe("fill", () => {
  test("every field and row of a rewrite rule, values as typed", () => {
    const values = core.fill(LISTED_REWRITE);
    assert.equal(values.id, "m_3");
    assert.equal(values.mode, "rewrite");
    assert.deepEqual([values.url_glob, values.method, values.host, values.ignore_query, values.note], ["*/profile*", "GET", "api.example.com", false, "edit the profile"]);
    assert.deepEqual([values.status, values.headers, values.body, values.timing, values.delay, values.body_mode], ["503", "Retry-After: 5", "", "delay", "1.5", "edit"]);
    assert.deepEqual(values.rows.query, [{ a: "page", b: "2" }, { a: "debug", b: "" }, { a: "token", b: "<redacted>" }]);
    assert.deepEqual(values.rows.request_headers, [{ a: "X-Client", b: "ios*" }, { a: "Authorization", b: "<redacted>" }]);
    assert.deepEqual(values.rows.body_contains, [{ a: "needle", b: "" }]);
    assert.deepEqual(values.rows.body_json, [{ a: "user.role", b: "admin" }, { a: "items.0.qty", b: "2" }, { a: "code", b: '"2"' }]);
    assert.deepEqual(values.rows.remove_headers, [{ a: "ETag", b: "" }]);
    assert.deepEqual(values.rows.json_set, [{ a: "user.plan", b: "pro" }, { a: "auth.token", b: "<redacted>" }]);
    assert.deepEqual(values.rows.json_remove, [{ a: "user.trial_ends", b: "" }]);
    assert.deepEqual(values.rows.replace, [{ a: "Free", b: "Pro" }]);
    assert.deepEqual([values.has_body, values.stored_status, values.stored_hang, values.stored_delay], [false, 503, false, 1.5]);
  });

  test("an empty stored value stays empty, apart from no value (any value)", () => {
    const values = core.fill({ id: "4", match: { url_glob: "*", query: [{ name: "flag", value: "" }, { name: "debug", value: null }], headers: [{ name: "X-Empty", value: "" }] }, response: { status: 200 } });
    assert.deepEqual(values.rows.query, [{ a: "flag", b: "", empty: true }, { a: "debug", b: "" }]);
    const edited = structuredClone(values);
    edited.rows.query.push({ a: "lang", b: "" });
    edited.rows.request_headers.push({ a: "X-Any", b: "" });
    const { payload } = core.payload(edited, values);
    assert.deepEqual(payload.query, [{ name: "flag", value: "" }, { name: "debug", value: null }, { name: "lang", value: null }]);
    assert.deepEqual(payload.request_headers, [{ name: "X-Empty", value: "" }, { name: "X-Any", value: null }]);
  });

  test("a fake response with a body and a hang; a rewrite without status; hand-edited junk", () => {
    const respond = core.fill({ id: 7, match: { url_glob: "*/a", method: "post" }, response: { status: 404, headers: {}, body_path: "/x/7.body" }, hang: true });
    assert.deepEqual([respond.id, respond.mode, respond.method, respond.status, respond.timing, respond.body_mode, respond.has_body], ["7", "respond", "POST", "404", "hang", "replace", true]);
    const rewrite = core.fill({ id: "m_1", mode: "rewrite", match: { url_glob: "*/b" }, response: { status: null, headers: {}, body_path: null } });
    assert.deepEqual([rewrite.status, rewrite.stored_status, rewrite.body_mode, rewrite.timing], ["", null, "keep", "none"]);
    const junk = core.fill({ match: { query: "x", headers: [null, { name: 3 }] }, response: null, rewrite: [], delay_s: "2" });
    assert.deepEqual(junk.rows.query, []);
    assert.deepEqual(junk.rows.request_headers, [{ a: "", b: "" }, { a: "3", b: "" }]);
    assert.deepEqual([junk.status, junk.timing, junk.id], ["200", "none", null]);
    assert.deepEqual(core.fill(null).rows.json_set, []);
  });
});

// --- payload: adding ---------------------------------------------------------------------------

describe("payload for mocks.add", () => {
  test("a plain fake response keeps the existing shape", () => {
    assert.deepEqual(core.payload(form({ url_glob: " */items* ", status: "418", headers: "Content-Type: text/plain", body: "mocked" }), null),
      { payload: { url_glob: "*/items*", status: 418, ignore_query: false, headers: { "Content-Type": "text/plain" }, body: "mocked" }, errors: [] });
  });

  test("MR-14-S01: Edit real response with one JSON set sends mode rewrite and that edit", () => {
    const result = core.payload(form({ mode: "rewrite", status: "", url_glob: "*/profile", body_mode: "edit", rows: { json_set: [{ a: "user.plan", b: '"pro"' }] } }), null);
    assert.deepEqual(result, { payload: { url_glob: "*/profile", mode: "rewrite", ignore_query: false, headers: {}, json_set: [{ path: "user.plan", value: "pro" }] }, errors: [] });
  });

  test("every condition kind and every edit kind, with timing, method, host and note", () => {
    const result = core.payload(form({
      mode: "rewrite", url_glob: "*/profile*", method: "get", host: "api.example.com", note: "n", status: "503",
      headers: "Retry-After: 5", timing: "delay", delay: "1.5", body_mode: "edit",
      rows: {
        query: [{ a: " page ", b: "2" }, { a: "debug", b: "" }, { a: "", b: "" }],
        request_headers: [{ a: "X-Client", b: " ios* " }],
        body_contains: [{ a: "needle", b: "" }],
        body_json: [{ a: "user.role", b: "admin" }, { a: "items.0.qty", b: "2" }, { a: "code", b: '"2"' }],
        remove_headers: [{ a: "ETag", b: "" }],
        json_set: [{ a: "user.plan", b: '"pro"' }, { a: "flags", b: '{"beta":true}' }],
        json_remove: [{ a: "user.trial_ends", b: "" }],
        replace: [{ a: "Free", b: "" }],
      },
    }), null);
    assert.deepEqual(result.errors, []);
    assert.deepEqual(result.payload, {
      url_glob: "*/profile*", mode: "rewrite", status: 503, ignore_query: false, headers: { "Retry-After": "5" },
      method: "GET", host: "api.example.com", note: "n", delay_s: 1.5,
      query: [{ name: "page", value: "2" }, { name: "debug", value: null }],
      request_headers: [{ name: "X-Client", value: "ios*" }],
      body_contains: ["needle"],
      body_json: [{ path: "user.role", value: "admin" }, { path: "items.0.qty", value: 2 }, { path: "code", value: "2" }],
      remove_headers: ["ETag"],
      json_set: [{ path: "user.plan", value: "pro" }, { path: "flags", value: { beta: true } }],
      json_remove: ["user.trial_ends"],
      replace: [{ find: "Free", with: "" }],
    });
  });

  test("rows that do not apply in the chosen mode or body choice are not sent", () => {
    const rows = { remove_headers: [{ a: "ETag", b: "" }], json_set: [{ a: "a", b: "1" }], query: [{ a: "q", b: "" }] };
    const respond = core.payload(form({ url_glob: "*", rows }), null).payload;
    assert.deepEqual(Object.keys(respond).filter((key) => ALL_KINDS.includes(key)), ["query"], "edits never go with a fake response");
    const keep = core.payload(form({ url_glob: "*", mode: "rewrite", status: "", body_mode: "keep", rows }), null).payload;
    assert.deepEqual(Object.keys(keep).filter((key) => ALL_KINDS.includes(key)), ["query", "remove_headers"]);
    const replace = core.payload(form({ url_glob: "*", mode: "rewrite", status: "", body_mode: "replace", body: '{"a":1}', rows }), null).payload;
    assert.equal(replace.body, '{"a":1}');
    assert.equal("json_set" in replace, false, "a whole body and body edits exclude each other");
  });

  test("timing: a delay, a hang, none", () => {
    assert.equal(core.payload(form({ url_glob: "*", timing: "delay", delay: " .5 " }), null).payload.delay_s, 0.5);
    assert.equal(core.payload(form({ url_glob: "*", timing: "delay", delay: "120" }), null).payload.delay_s, 120);
    assert.equal(core.payload(form({ url_glob: "*", timing: "hang", delay: "3" }), null).payload.hang, true);
    const none = core.payload(form({ url_glob: "*", timing: "none", delay: "3" }), null).payload;
    assert.equal("delay_s" in none || "hang" in none, false);
  });
});

// --- validation ----------------------------------------------------------------------------------

describe("validation before sending (MR-05, MR-11)", () => {
  const cases = [
    ["no URL glob", { url_glob: "  " }, [["url_glob", null, null]], /Enter a URL glob/],
    ["status 700", { url_glob: "*", status: "700" }, [["status", null, null]], /from 100 to 599/],
    ["status 99", { url_glob: "*", status: "99" }, [["status", null, null]], /from 100 to 599/],
    ["status text", { url_glob: "*", status: "2xx" }, [["status", null, null]], /whole number/],
    ["a fake response needs a status", { url_glob: "*", status: "" }, [["status", null, null]], /from 100 to 599/],
    ["rewrite status 700", { url_glob: "*", mode: "rewrite", status: "700" }, [["status", null, null]], /keep the real one/],
    ["a header line without a colon", { url_glob: "*", headers: "Content-Type: x\nnot a header" }, [["headers", null, null]], /a colon/],
    ["a rewrite header with a space", { url_glob: "*", mode: "rewrite", status: "", headers: "Bad Name: x" }, [["headers", null, null]], /valid header name/],
    ["a JSON-looking body that does not parse", { url_glob: "*", body: " {bad" }, [["body", null, null]], /not valid JSON/],
    ["a body over 32 KB", { url_glob: "*", body: "é".repeat(16 * 1024 + 1) }, [["body", null, null]], /larger than 32 KB/],
    ["a whole new body left empty", { url_glob: "*", mode: "rewrite", status: "", body_mode: "replace" }, [["body", null, null]], /new body/],
    ["delay 0", { url_glob: "*", timing: "delay", delay: "0" }, [["delay", null, null]], /above 0 and up to 120/],
    ["delay 121", { url_glob: "*", timing: "delay", delay: "121" }, [["delay", null, null]], /up to 120/],
    ["delay 1e1", { url_glob: "*", timing: "delay", delay: "1e1" }, [["delay", null, null]], /seconds/],
    ["delay -1", { url_glob: "*", timing: "delay", delay: "-1" }, [["delay", null, null]], /seconds/],
    ["delay empty", { url_glob: "*", timing: "delay", delay: "" }, [["delay", null, null]], /seconds/],
    ["a rewrite that hangs", { url_glob: "*", mode: "rewrite", status: "", timing: "hang" }, [["timing", null, null]], /cannot hang/],
    ["a long host", { url_glob: "*", host: "h".repeat(256) }, [["host", null, null]], /host is longer/],
    ["a long note", { url_glob: "*", note: "n".repeat(501) }, [["note", null, null]], /note is longer/],
    ["a query value without a name", { url_glob: "*", rows: { query: [{ a: "ok", b: "" }, { a: " ", b: "2" }] } }, [["query", 1, "a"]], /Enter a name/],
    ["a request header name with a space", { url_glob: "*", rows: { request_headers: [{ a: "Bad Name", b: "x" }] } }, [["request_headers", 0, "a"]], /valid header name/],
    ["a long query value", { url_glob: "*", rows: { query: [{ a: "q", b: "v".repeat(2049) }] } }, [["query", 0, "b"]], /2048 characters/],
    ["a body JSON path with an empty part", { url_glob: "*", rows: { body_json: [{ a: "a..b", b: "1" }] } }, [["body_json", 0, "a"]], /empty part/],
    ["a body JSON value without a path", { url_glob: "*", rows: { body_json: [{ a: "", b: "1" }] } }, [["body_json", 0, "a"]], /dotted path/],
    ["a long body text", { url_glob: "*", rows: { body_contains: [{ a: "x".repeat(2049), b: "" }] } }, [["body_contains", 0, "a"]], /2048/],
    ["a header to remove with a space", { url_glob: "*", mode: "rewrite", status: "", rows: { remove_headers: [{ a: "Bad Name", b: "" }] } }, [["remove_headers", 0, "a"]], /valid header name/],
    ["a JSON set number too large", { url_glob: "*", mode: "rewrite", status: "", body_mode: "edit", rows: { json_set: [{ a: "a", b: "1e400" }] } }, [["json_set", 0, "b"]], /too large/],
    ["a JSON set value too long", { url_glob: "*", mode: "rewrite", status: "", body_mode: "edit", rows: { json_set: [{ a: "a", b: "x".repeat(2047) }] } }, [["json_set", 0, "b"]], /2048/],
    ["a JSON remove path ending in a dot", { url_glob: "*", mode: "rewrite", status: "", body_mode: "edit", rows: { json_remove: [{ a: "a.", b: "" }] } }, [["json_remove", 0, "a"]], /empty part/],
    ["a replacement without text to find", { url_glob: "*", mode: "rewrite", status: "", body_mode: "edit", rows: { replace: [{ a: "", b: "Pro" }] } }, [["replace", 0, "a"]], /text to find/],
    ["21 query conditions", { url_glob: "*", rows: { query: Array.from({ length: 21 }, (_, index) => ({ a: "q" + index, b: "" })) } }, [["query", null, null]], /At most 20 query conditions/],
  ];
  for (const [name, fields, expected, message] of cases) {
    test(name, () => {
      const result = core.payload(form(fields), null);
      assert.equal(result.payload, null, "nothing to send");
      assert.deepEqual(fieldsOf(result), expected);
      assert.match(result.errors[0].message, message);
      assert.equal(result.errors[0].code, name === "no URL glob" ? "selector_required" : result.errors[0].code);
    });
  }

  test("several problems are all reported, each at its field", () => {
    const result = core.payload(form({ status: "700", rows: { request_headers: [{ a: "Bad Name", b: "x" }], body_json: [{ a: "a..b", b: "1" }] } }), null);
    assert.deepEqual(fieldsOf(result), [["url_glob", null, null], ["status", null, null], ["request_headers", 0, "a"], ["body_json", 0, "a"]]);
    assert.equal(result.errors[0].code, "selector_required");
    assert.equal(result.errors[1].code, "invalid_value");
  });

  test("conditions and edits over 32 KB together are refused", () => {
    const rows = (prefix) => Array.from({ length: 20 }, (_, index) => ({ a: prefix + index, b: "v".repeat(2000) }));
    const result = core.payload(form({ url_glob: "*", rows: { query: rows("q"), request_headers: rows("H") } }), null);
    assert.deepEqual(fieldsOf(result), [["lists", null, null]]);
    assert.match(result.errors[0].message, /32 KB together/);
  });
});

// --- payload: updating --------------------------------------------------------------------------

describe("payload for mocks.update", () => {
  test("MR-14-S02: saving a filled rule without changes sends only the base fields", () => {
    const values = core.fill(LISTED_REWRITE);
    assert.deepEqual(core.payload(values, values), { payload: {
      id: "m_3", url_glob: "*/profile*", status: 503, ignore_query: false, method: "GET", host: "api.example.com", note: "edit the profile", delay_s: 1.5,
    }, errors: [] });
    const plain = core.fill({ id: "1", match: { url_glob: "*/a*", method: null, host: null, ignore_query: true }, response: { status: 200, headers: { "X-A": "1" }, body_path: "/b" }, hang: true, note: null });
    assert.deepEqual(core.payload(plain, plain).payload, { id: "1", url_glob: "*/a*", status: 200, ignore_query: true, method: "", host: "", note: "", hang: true });
    const realStatus = core.fill({ id: "2", mode: "rewrite", match: { url_glob: "*/b" }, response: { status: null, headers: {}, body_path: "/b" } });
    assert.deepEqual(core.payload(realStatus, realStatus).payload, { id: "2", url_glob: "*/b", ignore_query: false, method: "", host: "", note: "" });
  });

  test("a changed list is sent whole, masked values included; the others stay out", () => {
    const original = core.fill(LISTED_REWRITE);
    const edited = structuredClone(original);
    edited.rows.query.push({ a: "lang", b: "en" });
    edited.headers = "Retry-After: 9";
    const { payload, errors } = core.payload(edited, original);
    assert.deepEqual(errors, []);
    assert.deepEqual(payload.query, [{ name: "page", value: "2" }, { name: "debug", value: null }, { name: "token", value: "<redacted>" }, { name: "lang", value: "en" }]);
    assert.deepEqual(payload.headers, { "Retry-After": "9" });
    for (const key of ALL_KINDS.filter((kind) => kind !== "query")) assert.equal(key in payload, false, key);
    // A removed row sends the shorter list; removing the last one sends an empty list (clears it).
    const fewer = structuredClone(original);
    fewer.rows.replace = [];
    assert.deepEqual(core.payload(fewer, original).payload.replace, []);
  });

  test("switching a rewrite rule to a fake response clears its edits and asks for a status", () => {
    const original = core.fill(LISTED_REWRITE);
    const switched = { ...structuredClone(original), mode: "respond", status: "201" };
    const { payload } = core.payload(switched, original);
    assert.equal(payload.mode, "respond");
    assert.equal(payload.status, 201);
    for (const key of ["remove_headers", "json_set", "json_remove", "replace"]) assert.deepEqual(payload[key], [], key);
    for (const key of ["query", "request_headers", "body_contains", "body_json"]) assert.equal(key in payload, false, key);
  });

  test("switching a held fake response to an edit keeps the real status and drops the hang", () => {
    const original = core.fill({ id: "5", match: { url_glob: "*/a" }, response: { status: 404, headers: {}, body_path: "/b" }, hang: true });
    const switched = { ...structuredClone(original), mode: "rewrite", status: "", timing: "none" };
    const { payload } = core.payload(switched, original);
    assert.equal(payload.mode, "rewrite");
    assert.equal(payload.clear_status, true);
    assert.equal(payload.hang, false);
    assert.equal("body" in payload || "clear_body" in payload, false, "the whole body stays (Replace it whole, left empty)");
  });

  test("the body choice of a stored whole body: keep drops it, edit drops it and sends the edits", () => {
    const original = core.fill({ id: "6", mode: "rewrite", match: { url_glob: "*/a" }, response: { status: null, headers: {}, body_path: "/b" } });
    assert.equal(original.body_mode, "replace");
    assert.equal(core.payload({ ...structuredClone(original), body_mode: "keep" }, original).payload.clear_body, true);
    const edit = core.payload({ ...structuredClone(original), body_mode: "edit", rows: { ...original.rows, json_set: [{ a: "a", b: "1" }] } }, original).payload;
    assert.equal(edit.clear_body, true);
    assert.deepEqual(edit.json_set, [{ path: "a", value: 1 }]);
    const fresh = core.payload({ ...structuredClone(original), body: "new" }, original).payload;
    assert.equal(fresh.body, "new");
  });

  test("timing on update: a stored delay cannot be removed here; a stored hang can", () => {
    const delayed = core.fill({ id: "8", match: { url_glob: "*/a" }, response: { status: 200 }, delay_s: 2 });
    const removed = core.payload({ ...structuredClone(delayed), timing: "none" }, delayed);
    assert.deepEqual(fieldsOf(removed), [["timing", null, null]]);
    assert.match(removed.errors[0].message, /cannot be removed here/);
    assert.equal(core.payload({ ...structuredClone(delayed), timing: "hang" }, delayed).payload.hang, true);
    const held = core.fill({ id: "9", match: { url_glob: "*/a" }, response: { status: 200 }, hang: true });
    assert.equal(core.payload({ ...structuredClone(held), timing: "none" }, held).payload.hang, false);
    assert.equal(core.payload({ ...structuredClone(held), timing: "delay", delay: "3" }, held).payload.delay_s, 3);
  });

  test("an empty status on a rewrite rule that had one returns it to the real status", () => {
    const original = core.fill(LISTED_REWRITE);
    const { payload } = core.payload({ ...structuredClone(original), status: "" }, original);
    assert.equal(payload.clear_status, true);
    assert.equal("status" in payload, false);
  });
});

// --- list summary ---------------------------------------------------------------------------------

describe("summary", () => {
  test("a rewrite rule: Edit badge, condition summary, edits and hits", () => {
    const info = core.summary(LISTED_REWRITE);
    assert.equal(info.rewrite, true);
    assert.equal(info.title, "GET */profile*");
    assert.equal(info.conditions, "page=2 · debug · +1 query param · +2 headers · +1 body text · +3 body fields");
    assert.equal(info.detail, "503 · page=2 · debug · +1 query param · +2 headers · +1 body text · +3 body fields · 6 edits · 2 hits");
    assert.equal(info.tooltip, "*/profile* · #m_3 · api.example.com · edit the profile");
    assert.doesNotMatch(JSON.stringify(info), /abc123/);
  });

  test("the spec's example, a fake response and a rewrite keeping the real status", () => {
    assert.equal(core.summary({ match: { query: [{ name: "page", value: "2" }], headers: [{ name: "X", value: null }] } }).conditions, "page=2 · +1 header");
    const plain = core.summary({ id: "1", match: { url_glob: "*/a*", method: "GET" }, response: { status: 200 }, hits: 3 });
    assert.deepEqual([plain.rewrite, plain.title, plain.detail], [false, "GET */a*", "200 · 3 hits"]);
    const real = core.summary({ id: "2", mode: "rewrite", match: { url_glob: "*/b" }, response: { status: null, headers: {}, body_path: "/x" } });
    assert.equal(real.detail, "Real status · 1 edit · 0 hits");
    assert.equal(core.summary(null).detail, "— · 0 hits");
  });
});

// --- the real tools process ---------------------------------------------------------------------

/** The Canvas tools process of this checkout in a temporary AUTONOM_HOME; mocks ops only. */
async function toolsProcess(t) {
  const home = await mkdtemp(join(tmpdir(), "autonom-mock-form-"));
  t.after(() => rm(home, { recursive: true, force: true }));
  const python = process.env.AUTONOM_TEST_PYTHON || "python3";
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
  t.after(() => { child.stdin.end(); child.kill(); });
  let next = 0;
  const call = (op, payload = {}) => new Promise((resolveReply, reject) => {
    next += 1;
    waiting.set(next, resolveReply);
    child.once("exit", (code) => reject(new Error(`the tools process exited (${code}): ${stderr}`)));
    child.stdin.write(JSON.stringify({ id: next, op, origin: "human", payload }) + "\n");
  });
  const ok = async (op, payload) => {
    const reply = await call(op, payload);
    assert.equal(reply.ok, true, `${op}: ${JSON.stringify(reply)}`);
    return reply.result;
  };
  const registry = async () => JSON.parse(await readFile(join(home, "home", "mocks", "registry.json"), "utf8")).mocks;
  const stored = async (id) => (await registry()).find((rule) => rule.id === id);
  const listed = async (id) => (await ok("mocks.list")).mocks.find((rule) => rule.id === id);
  return { ok, call, registry, stored, listed };
}
const withoutStamp = ({ updated_at: _stamp, ...rule }) => rule;

const FIXTURES = {
  "every condition and edit, secrets included": {
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
  "a fake JSON response that hangs": {
    url_glob: "*/held*", method: "POST", body: '{"ok": false}', hang: true, query: [{ name: "debug", value: null }], body_contains: ["a"],
  },
  "a whole new body on the real response, after a delay": {
    url_glob: "*/whole*", mode: "rewrite", body: "plain text", delay_s: 0.5, headers: { "X-Edited": "1" },
  },
  "an old-style rule with nothing new": { url_glob: "https://api.test/items", ignore_query: true, status: 500, headers: { "Content-Type": "text/plain" }, body: "x" },
};

describe("round trip through the real tools process (MR-14-S02, RISK-005)", () => {
  test("Edit then Save without changes stores the same rule, secrets kept", async (t) => {
    const tools = await toolsProcess(t);
    for (const [name, fixture] of Object.entries(FIXTURES)) {
      const { id } = await tools.ok("mocks.add", fixture);
      const before = await tools.stored(id);
      const shown = await tools.listed(id);
      assert.doesNotMatch(JSON.stringify(shown), /abc123|resp-secret/, `${name}: the list is masked`);
      const values = core.fill(shown);
      const { payload, errors } = core.payload(values, values);
      assert.deepEqual(errors, [], name);
      assert.doesNotMatch(JSON.stringify(payload), /abc123|resp-secret/, `${name}: the payload carries no secret`);
      await tools.ok("mocks.update", payload);
      const after = await tools.stored(id);
      assert.ok(after.updated_at, `${name}: the update was stored`);
      assert.deepEqual(withoutStamp(after), withoutStamp(before), name);
    }
  });

  test("a changed row is stored and the masked values next to it keep their secrets", async (t) => {
    const tools = await toolsProcess(t);
    const { id } = await tools.ok("mocks.add", FIXTURES["every condition and edit, secrets included"]);
    const before = await tools.stored(id);
    const values = core.fill(await tools.listed(id));
    const edited = structuredClone(values);
    edited.rows.query.push({ a: "lang", b: "en" });
    edited.rows.request_headers[0].b = "android*";
    edited.rows.json_set.push({ a: "user.seats", b: "5" });
    edited.headers += "\nX-New: 1";
    const { payload, errors } = core.payload(edited, values);
    assert.deepEqual(errors, []);
    await tools.ok("mocks.update", payload);
    const after = await tools.stored(id);
    assert.deepEqual(after.match.query, [...before.match.query, { name: "lang", value: "en" }]);
    assert.deepEqual(after.match.headers, [{ name: "X-Client", value: "android*" }, { name: "Authorization", value: "Bearer abc123" }]);
    assert.deepEqual(after.rewrite.json_set, [...before.rewrite.json_set, { path: "user.seats", value: 5 }]);
    assert.deepEqual(after.response.headers, { ...before.response.headers, "X-New": "1" });
    assert.deepEqual(after.match.body, before.match.body);
    assert.deepEqual(after.rewrite.replace, before.rewrite.replace);
  });

  test("mode switches the bridge accepts, built from the form", async (t) => {
    const tools = await toolsProcess(t);
    const { id } = await tools.ok("mocks.add", FIXTURES["every condition and edit, secrets included"]);
    let values = core.fill(await tools.listed(id));
    let result = core.payload({ ...structuredClone(values), mode: "respond", status: "201", timing: "delay", delay: "1.5" }, values);
    await tools.ok("mocks.update", result.payload);
    let after = await tools.stored(id);
    assert.equal("mode" in after, false);
    assert.equal("rewrite" in after, false, "the edits are gone with the fake response");
    assert.equal(after.response.status, 201);
    assert.equal(after.match.headers[1].value, "Bearer abc123");

    const held = await tools.ok("mocks.add", FIXTURES["a fake JSON response that hangs"]);
    values = core.fill(await tools.listed(held.id));
    result = core.payload({ ...structuredClone(values), mode: "rewrite", status: "", timing: "none" }, values);
    await tools.ok("mocks.update", result.payload);
    after = await tools.stored(held.id);
    assert.deepEqual([after.mode, "status" in after.response, "hang" in after], ["rewrite", false, false]);
    assert.ok(after.response.body_path, "the whole body stays");

    values = core.fill(await tools.listed(held.id));
    result = core.payload({ ...structuredClone(values), body_mode: "edit", rows: { ...values.rows, replace: [{ a: "false", b: "true" }] } }, values);
    await tools.ok("mocks.update", result.payload);
    after = await tools.stored(held.id);
    assert.equal(after.response.body_path, null, "edits replace the whole body");
    assert.deepEqual(after.rewrite, { replace: [{ find: "false", with: "true" }] });
  });

  test("a payload the page would refuse is refused by the bridge too", async (t) => {
    const tools = await toolsProcess(t);
    for (const payload of [
      { url_glob: "*", request_headers: [{ name: "Bad Name", value: "x" }] },
      { url_glob: "*", body_json: [{ path: "a..b", value: 1 }] },
      { url_glob: "*", status: 700 },
      { url_glob: "*", mode: "rewrite", hang: true },
    ]) {
      const reply = await tools.call("mocks.add", payload);
      assert.equal(reply.ok, false, JSON.stringify(payload));
      assert.equal(reply.error_code, "invalid_value");
    }
    assert.deepEqual(await tools.ok("mocks.list").then((result) => result.mocks), []);
  });
});
