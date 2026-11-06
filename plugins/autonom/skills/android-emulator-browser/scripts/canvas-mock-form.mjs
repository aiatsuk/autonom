import { canvasIconData } from "./canvas-ui.mjs";
// The Mocks form of the Tools drawer (MR-14): a fake response or an edit of the real one, the
// request conditions it matches on, the edits and the timing. Two parts:
//   mockFormCore()        pure functions: the empty form, a stored rule turned into form
//                         values (fill), form values turned into a checked mocks.add /
//                         mocks.update payload (payload), and the list row summary.
//   mockFormController()  the DOM wiring: rows that can be added and removed, the mode and
//                         body choices, reading the fields and showing messages beside them.
// Both are plain functions that toolsScript() inlines with Function.prototype.toString, so
// they avoid backticks and never write HTML; node tests import the same code.

/**
 * The pure half. Self-contained (no outer names), so it runs the same in node and inlined in
 * the page. Form values are strings as typed; `rows` holds one list of {a, b} per kind.
 */
export function mockFormCore() {
  const MAX_ITEMS = 20, MAX_VALUE = 2048, MAX_BODY = 32 * 1024, MAX_LISTS = 32 * 1024;
  const MAX_HEADERS = 64, MAX_HEADER_NAME = 256, MAX_HEADER_VALUE = 8192;
  const MAX_DELAY = 120, MAX_GLOB = 2048, MAX_HOST = 255, MAX_NOTE = 500;
  // RFC 7230 token characters (\x60 is the backquote).
  const TOKEN = /^[!#$%&'*+.^_\x60|~0-9A-Za-z-]+$/;
  const DECIMAL = /^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$/;
  const CONDITIONS = ["query", "request_headers", "body_contains", "body_json"];
  const BODY_EDITS = ["json_set", "json_remove", "replace"];
  const EDITS = ["remove_headers"].concat(BODY_EDITS);
  const LISTS = CONDITIONS.concat(EDITS);
  // tag: the short word at the start of a row; a/b: the screen-reader names of its fields;
  // hintA/hintB: placeholders; noun: one row; many: the count limit message.
  const KINDS = {
    query: { section: "conditions", tag: "Query", pair: true, a: "Query parameter name", b: "Query parameter value", hintA: "Name", hintB: "Any value", noun: "query condition", many: "query conditions" },
    request_headers: { section: "conditions", tag: "Header", pair: true, a: "Request header name", b: "Request header value", hintA: "Header", hintB: "Any value", noun: "header condition", many: "header conditions" },
    body_contains: { section: "conditions", tag: "Body", pair: false, a: "Text the request body contains", hintA: "Text in the body", noun: "body text condition", many: "body text conditions" },
    body_json: { section: "conditions", tag: "JSON", pair: true, a: "Request body JSON path", b: "Request body JSON value", hintA: "user.id", hintB: "Value", noun: "body field condition", many: "body field conditions" },
    remove_headers: { section: "headers", tag: "Remove", pair: false, a: "Response header to remove", hintA: "ETag", noun: "header to remove", many: "headers to remove" },
    json_set: { section: "body", tag: "Set", pair: true, a: "JSON path to set", b: "Value to set", hintA: "user.plan", hintB: "\"pro\"", noun: "field to set", many: "fields to set" },
    json_remove: { section: "body", tag: "Remove", pair: false, a: "JSON path to remove", hintA: "user.trial_ends", noun: "field to remove", many: "fields to remove" },
    replace: { section: "body", tag: "Replace", pair: true, a: "Text to find", b: "Replacement text", hintA: "Find", hintB: "Replace with", noun: "text replacement", many: "text replacements" },
  };
  const TOO_LONG = "Keep this to " + MAX_VALUE + " characters or fewer.";
  const HEADER_NAME = "Use a valid header name, such as X-Client (no spaces).";
  const PATH_HINT = "Enter a dotted path such as user.id or items.0.id.";

  const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const object = (value) => (isObject(value) ? value : {});
  const list = (value) => (Array.isArray(value) ? value : []);
  const string = (value) => (value === undefined || value === null ? "" : String(value));
  function utf8(text) { return new TextEncoder().encode(text).length; }
  function parses(text) { try { JSON.parse(text); return true; } catch { return false; } }
  /** Typed text as the CLI reads PATH=VALUE: JSON when it parses, else the text itself. */
  function parseValue(text) { try { return JSON.parse(text); } catch { return text; } }
  /** The text that parseValue turns back into the value: text that would parse is quoted. */
  function formatValue(value) {
    if (typeof value === "string") return parses(value) ? JSON.stringify(value) : value;
    return JSON.stringify(value === undefined ? null : value);
  }
  function headersText(headers) {
    const source = object(headers);
    return Object.keys(source).map((name) => name + ": " + string(source[name])).join("\n");
  }
  /** "Name: value" lines; in rewrite mode a name must be a header token, as the bridge checks. */
  function parseHeaders(text, strict) {
    const headers = {};
    let count = 0;
    for (const line of string(text).split(/\r?\n/)) {
      if (!line.trim()) continue;
      const colon = line.indexOf(":");
      if (colon < 1 || !line.slice(0, colon).trim()) return { error: "Each header line needs a name, a colon and a value." };
      const name = line.slice(0, colon).trim(), value = line.slice(colon + 1).trim();
      if (strict && !TOKEN.test(name)) return { error: HEADER_NAME };
      if (name.length > MAX_HEADER_NAME || value.length > MAX_HEADER_VALUE) return { error: "A header name is at most 256 characters and a value at most 8192." };
      if (!Object.prototype.hasOwnProperty.call(headers, name)) count += 1;
      headers[name] = value;
    }
    if (count > MAX_HEADERS) return { error: "At most " + MAX_HEADERS + " headers." };
    return { headers };
  }
  function pathProblem(path) {
    if (!path) return PATH_HINT;
    if (path.split(".").some((segment) => segment === "")) return "This path has an empty part. " + PATH_HINT;
    if (path.length > MAX_VALUE) return TOO_LONG;
    return null;
  }
  function emptyRows() {
    const rows = {};
    for (const key of LISTS) rows[key] = [];
    return rows;
  }

  /** A new form: a fake 200 response with no conditions, edits or timing. */
  function empty() {
    return {
      id: null, mode: "respond", url_glob: "", method: "", host: "", ignore_query: false, note: "",
      status: "200", headers: "", body: "", timing: "none", delay: "", body_mode: "keep", rows: emptyRows(),
      has_body: false, stored_status: null, stored_hang: false, stored_delay: null,
    };
  }

  /**
   * A stored rule (as mocks.list returns it, masked values included) as form values. The
   * stored_* fields and has_body describe what is stored, so payload() can tell a value the
   * person changed from one they left alone.
   */
  function fill(rule) {
    const source = object(rule), match = object(source.match), response = object(source.response);
    const body = object(match.body), rewrite = object(source.rewrite);
    const mode = source.mode === "rewrite" ? "rewrite" : "respond";
    const status = typeof response.status === "number" ? response.status : null;
    const delay = typeof source.delay_s === "number" && isFinite(source.delay_s) ? source.delay_s : null;
    const hasBody = Boolean(response.body_path);
    // A stored empty value ("name=" on the CLI) is not the same as no value (any value): the row
    // keeps a mark so it is sent back as "" and not as null.
    const named = (items) => list(items).map((item) => (object(item).value === "" ? { a: string(object(item).name), b: "", empty: true }
      : { a: string(object(item).name), b: string(object(item).value) }));
    const pathed = (items) => list(items).map((item) => ({ a: string(object(item).path), b: formatValue(object(item).value) }));
    const single = (items) => list(items).map((item) => ({ a: string(item), b: "" }));
    const rows = {
      query: named(match.query), request_headers: named(match.headers),
      body_contains: single(body.contains), body_json: pathed(body.json),
      remove_headers: single(rewrite.remove_headers), json_set: pathed(rewrite.json_set),
      json_remove: single(rewrite.json_remove),
      replace: list(rewrite.replace).map((item) => ({ a: string(object(item).find), b: string(object(item).with) })),
    };
    const bodyEdits = BODY_EDITS.some((key) => rows[key].length > 0);
    return {
      id: source.id === undefined || source.id === null ? null : String(source.id),
      mode, url_glob: string(match.url_glob), method: string(match.method).toUpperCase(), host: string(match.host),
      ignore_query: match.ignore_query === true, note: string(source.note),
      status: status === null ? (mode === "respond" ? "200" : "") : String(status),
      headers: headersText(response.headers), body: "",
      timing: source.hang === true ? "hang" : delay !== null ? "delay" : "none", delay: delay === null ? "" : String(delay),
      body_mode: hasBody ? "replace" : bodyEdits ? "edit" : "keep", rows,
      has_body: hasBody, stored_status: status, stored_hang: source.hang === true, stored_delay: delay,
    };
  }

  /** The rows that apply in the form's mode and body choice; empty rows are left out. */
  function effective(form) {
    const rewrite = form.mode === "rewrite", rows = object(form.rows), out = {};
    for (const key of LISTS) {
      let items = list(rows[key]).map((row, index) => ({ a: string(object(row).a), b: string(object(row).b), empty: object(row).empty === true, index }))
        .filter((row) => row.a !== "" || row.b !== "");
      if (!rewrite && EDITS.includes(key)) items = [];
      if (rewrite && BODY_EDITS.includes(key) && form.body_mode !== "edit") items = [];
      out[key] = items;
    }
    return out;
  }
  const sameRows = (left, right) => JSON.stringify(left.map((row) => [row.a, row.b, row.empty])) === JSON.stringify(right.map((row) => [row.a, row.b, row.empty]));

  /** One list as the bridge takes it, each row checked like mocks.validate_conditions / validate_rewrite. */
  function buildList(key, rows, errors) {
    const kind = KINDS[key], out = [];
    if (rows.length > MAX_ITEMS) errors.push({ field: key, index: null, part: null, message: "At most " + MAX_ITEMS + " " + kind.many + "." });
    for (const row of rows) {
      const fail = (part, message) => { errors.push({ field: key, index: row.index, part, message }); };
      const name = row.a.trim();
      if (key === "query" || key === "request_headers") {
        const value = key === "request_headers" ? row.b.trim() : row.b;
        if (!name) fail("a", "Enter a name.");
        else if (key === "request_headers" && !TOKEN.test(name)) fail("a", HEADER_NAME);
        else if (name.length > MAX_VALUE) fail("a", TOO_LONG);
        else if (value.length > MAX_VALUE) fail("b", TOO_LONG);
        else out.push({ name, value: value === "" ? (row.empty ? "" : null) : value });
      } else if (key === "body_contains") {
        if (row.a.length > MAX_VALUE) fail("a", TOO_LONG);
        else out.push(row.a);
      } else if (key === "body_json" || key === "json_set") {
        const problem = pathProblem(name), value = parseValue(row.b);
        if (problem) fail("a", problem);
        else if (typeof value === "number" && !isFinite(value)) fail("b", "This number is too large. Quote it to use it as text.");
        else if (JSON.stringify(value).length > MAX_VALUE) fail("b", TOO_LONG);
        else out.push({ path: name, value });
      } else if (key === "json_remove") {
        const problem = pathProblem(name);
        if (problem) fail("a", problem);
        else out.push(name);
      } else if (key === "remove_headers") {
        if (!TOKEN.test(name)) fail("a", HEADER_NAME);
        else if (name.length > MAX_VALUE) fail("a", TOO_LONG);
        else out.push(name);
      } else if (key === "replace") {
        if (row.a === "") fail("a", "Enter the text to find.");
        else if (row.a.length > MAX_VALUE) fail("a", TOO_LONG);
        else if (row.b.length > MAX_VALUE) fail("b", TOO_LONG);
        else out.push({ find: row.a, with: row.b });
      }
    }
    return out;
  }

  function bodyProblem(text) {
    if (utf8(text) > MAX_BODY) return "The body is larger than 32 KB.";
    if (/^\s*[[{]/.test(text) && !parses(text)) return "The body starts like JSON but is not valid JSON.";
    return null;
  }

  /**
   * The checked mocks.add payload (original null) or mocks.update payload (original = the
   * fill() of the stored rule). On update a list, the headers or the body are sent only when
   * they differ from what is stored, so saving without changes stores the same rule (masked
   * values included); url, method, host, query flag, note and status are always sent.
   * Returns {payload, errors}; payload is null when any field is invalid.
   */
  function payload(form, original) {
    const errors = [], editing = Boolean(original), rewrite = form.mode === "rewrite", out = {};
    const fail = (field, message, code) => { errors.push({ field, index: null, part: null, message, code: code || "invalid_value" }); };
    const glob = string(form.url_glob).trim();
    if (!glob) fail("url_glob", "Enter a URL glob, for example */api/items*.", "selector_required");
    else if (glob.length > MAX_GLOB) fail("url_glob", "The URL glob is longer than " + MAX_GLOB + " characters.");
    out.url_glob = glob;
    if (editing ? form.mode !== original.mode : rewrite) out.mode = rewrite ? "rewrite" : "respond";

    const statusText = string(form.status).trim(), status = Number(statusText);
    if (rewrite && statusText === "") {
      if (editing && original.stored_status !== null) out.clear_status = true;
    } else if (!/^[0-9]+$/.test(statusText) || status < 100 || status > 599) {
      fail("status", rewrite ? "Leave the status empty to keep the real one, or enter a whole number from 100 to 599."
        : "The status must be a whole number from 100 to 599.");
    } else out.status = status;
    out.ignore_query = form.ignore_query === true;

    const headers = parseHeaders(form.headers, rewrite);
    if (headers.error) fail("headers", headers.error);
    else if (!editing || JSON.stringify(headers.headers) !== JSON.stringify(parseHeaders(original.headers, false).headers)) out.headers = headers.headers;

    const method = string(form.method).trim().toUpperCase(), host = string(form.host).trim(), note = string(form.note).trim();
    if (host.length > MAX_HOST) fail("host", "The host is longer than " + MAX_HOST + " characters.");
    if (note.length > MAX_NOTE) fail("note", "The note is longer than " + MAX_NOTE + " characters.");
    if (method || editing) out.method = method;
    if (host || editing) out.host = host;
    if (note || editing) out.note = note;

    const body = string(form.body);
    if (!rewrite) {
      if (body || !editing) {
        const problem = bodyProblem(body);
        if (problem) fail("body", problem); else out.body = body;
      }
    } else if (form.body_mode === "replace") {
      if (body) {
        const problem = bodyProblem(body);
        if (problem) fail("body", problem); else out.body = body;
      } else if (!editing || !original.has_body) fail("body", "Enter the new body, or keep the real one.");
    } else if (editing && original.has_body) out.clear_body = true;

    const timing = form.timing === "delay" || form.timing === "hang" ? form.timing : "none";
    if (timing === "hang") {
      if (rewrite) fail("timing", "An edited real response cannot hang. Choose a delay or none.");
      else out.hang = true;
    } else if (timing === "delay") {
      const text = string(form.delay).trim(), seconds = Number(text);
      if (!DECIMAL.test(text) || !(seconds > 0) || seconds > MAX_DELAY) fail("delay", "Enter seconds above 0 and up to 120, for example 2.5.");
      else out.delay_s = seconds;
    } else if (editing && original.stored_delay !== null) {
      fail("timing", "A saved delay cannot be removed here. Enter another delay, or remove the mock and add it again.");
    } else if (editing && original.stored_hang) out.hang = false;

    const now = effective(form), before = editing ? effective(original) : null, lists = {};
    for (const key of LISTS) {
      if (editing ? sameRows(now[key], before[key]) : now[key].length === 0) continue;
      lists[key] = buildList(key, now[key], errors);
    }
    if (utf8(JSON.stringify(lists)) > MAX_LISTS) fail("lists", "The conditions and edits are larger than 32 KB together. Keep fewer or shorter ones.");
    Object.assign(out, lists);
    if (editing && original.id !== null) out.id = original.id;
    return { payload: errors.length ? null : out, errors };
  }

  function plural(count, word) { return "+" + count + " " + word + (count === 1 ? "" : "s"); }
  /**
   * The list row: title (method and glob), rewrite (the Edit badge), detail (status, the
   * condition summary such as "page=2 · +1 header", edits and hits) and a tooltip. Values
   * arrive masked from mocks.list; nothing here unmasks them.
   */
  function summary(rule) {
    const source = object(rule), match = object(source.match), response = object(source.response);
    const body = object(match.body), rewrite = source.mode === "rewrite";
    const parts = [], query = list(match.query).filter(isObject), shown = query.slice(0, 2);
    for (const item of shown) parts.push(item.value === null || item.value === undefined ? string(item.name) : string(item.name) + "=" + string(item.value));
    if (query.length > shown.length) parts.push(plural(query.length - shown.length, "query param"));
    const headerCount = list(match.headers).length, textCount = list(body.contains).length, fieldCount = list(body.json).length;
    if (headerCount) parts.push(plural(headerCount, "header"));
    if (textCount) parts.push(plural(textCount, "body text"));
    if (fieldCount) parts.push(plural(fieldCount, "body field"));
    const conditions = parts.join(" · ");
    const statusText = rewrite ? (typeof response.status === "number" ? String(response.status) : "Real status") : string(response.status || "—");
    let edits = 0;
    if (rewrite) {
      const stored = object(source.rewrite);
      for (const key of EDITS) edits += list(stored[key]).length;
      edits += Object.keys(object(response.headers)).length + (response.body_path ? 1 : 0);
    }
    const hits = Number(source.hits) || 0;
    const detail = [statusText, conditions, rewrite && edits ? edits + (edits === 1 ? " edit" : " edits") : "", hits + " hits"].filter(Boolean).join(" · ");
    const method = string(match.method);
    return {
      rewrite, title: (method ? method + " " : "") + string(match.url_glob), conditions, detail,
      tooltip: string(match.url_glob) + " · #" + string(source.id) + (match.host ? " · " + string(match.host) : "") + (source.note ? " · " + string(source.note) : ""),
    };
  }

  return Object.freeze({ KINDS, LISTS, CONDITIONS, EDITS, BODY_EDITS, empty, fill, payload, summary, parseValue, formatValue, parseHeaders });
}

/**
 * The DOM half: `env` brings the page's $, el, makeButton and text helpers and the core. It
 * owns the form's rows and visibility; the page owns loading, saving and the list.
 */
export function mockFormController(env) {
  const $ = env.$, el = env.el, makeButton = env.makeButton, text = env.text, core = env.core;
  const form = $("tools-mock-form");
  const CONTAINERS = { conditions: "tools-mock-conditions", headers: "tools-mock-remove-headers", body: "tools-mock-edits" };
  const SECTION_ERRORS = { conditions: "tools-mock-conditions-error", headers: "tools-mock-remove-headers-error", body: "tools-mock-edits-error" };
  // A field's input and the message slot beside it.
  const SLOTS = {
    url_glob: ["tools-mock-glob", "tools-mock-glob-error"], host: ["tools-mock-host", "tools-mock-match-error"],
    status: ["tools-mock-status", "tools-mock-response-error"], timing: ["tools-mock-timing", "tools-mock-response-error"],
    delay: ["tools-mock-delay", "tools-mock-response-error"], headers: ["tools-mock-headers", "tools-mock-headers-error"],
    body: ["tools-mock-body", "tools-mock-body-error"], note: ["tools-mock-note", "tools-mock-note-error"],
  };
  const records = [];
  let mode = "respond", original = null, seq = 0;
  // Each mode keeps its own status text: a fake response needs one, an edit keeps the real one.
  const statusFor = { respond: "200", rewrite: "" };
  let invalid = [];

  function field(label, placeholder, value) {
    const node = el("input", "tools-mock-field");
    node.setAttribute("type", "text");
    node.setAttribute("autocomplete", "off");
    node.setAttribute("autocapitalize", "off");
    node.setAttribute("spellcheck", "false");
    node.setAttribute("aria-label", label);
    if (placeholder) node.setAttribute("placeholder", placeholder);
    node.value = value || "";
    return node;
  }
  function addButtonFor(key) {
    for (const button of form.querySelectorAll("[data-mock-add]")) if (button.getAttribute("data-mock-add") === key) return button;
    return null;
  }
  function addRow(key, values, focus) {
    const kind = core.KINDS[key];
    if (!kind) return null;
    seq += 1;
    const row = el("div", "tools-mock-row" + (kind.pair ? "" : " tools-mock-row-single"));
    row.setAttribute("data-kind", key);
    const tag = el("span", "tools-mock-tag", kind.tag);
    tag.setAttribute("aria-hidden", "true");
    row.appendChild(tag);
    const a = field(kind.a, kind.hintA, values && values.a);
    row.appendChild(a);
    const empty = Boolean(values && values.empty === true);
    const b = kind.pair ? field(kind.b, empty ? "Empty value" : kind.hintB, values && values.b) : null;
    if (b) row.appendChild(b);
    const remove = makeButton("", "btn small tools-mock-icon tools-mock-row-remove", { "aria-label": "Remove " + kind.noun, title: "Remove " + kind.noun });
    row.appendChild(remove);
    const error = el("p", "tools-field-error");
    error.setAttribute("id", "tools-mock-row-" + seq + "-error");
    error.setAttribute("role", "status");
    error.hidden = true;
    row.appendChild(error);
    const record = { key, row, a, b, error, empty };
    records.push(record);
    remove.addEventListener("click", () => removeRow(record));
    $(CONTAINERS[kind.section]).appendChild(row);
    if (focus) a.focus();
    return record;
  }
  function removeRow(record) {
    const index = records.indexOf(record);
    if (index < 0) return;
    records.splice(index, 1);
    record.row.remove();
    const button = addButtonFor(record.key);
    if (button) button.focus();
  }
  function clearRows() { for (const record of records.splice(0)) record.row.remove(); }
  function ensureOption(select, value) {
    if (!value) return;
    if (Array.from(select.querySelectorAll("option")).some((option) => option.getAttribute("value") === value)) return;
    const option = el("option", null, value);
    option.setAttribute("value", value);
    select.appendChild(option);
  }

  /** Show what the mode and the timing and body choices need; hide the rest. */
  function sync() {
    const rewrite = mode === "rewrite";
    const timing = $("tools-mock-timing"), hang = $("tools-mock-timing-hang");
    if (hang) { hang.hidden = rewrite; hang.disabled = rewrite; }
    if (rewrite && timing.value === "hang") timing.value = "none";
    $("tools-mock-delay-field").hidden = timing.value !== "delay";
    const bodyMode = $("tools-mock-body-mode").value;
    $("tools-mock-rewrite-headers").hidden = !rewrite;
    $("tools-mock-body-choice").hidden = !rewrite;
    $("tools-mock-body-field").hidden = rewrite && bodyMode !== "replace";
    $("tools-mock-body-edits").hidden = !(rewrite && bodyMode === "edit");
    text("tools-mock-response-title", rewrite ? "Edit response" : "Response");
    text("tools-mock-headers-label", rewrite ? "Set headers" : "Headers");
    text("tools-mock-body-label", rewrite ? "New body" : "Response body");
    text("tools-mock-mode-hint", rewrite ? "The request reaches the server; these edits change its answer before the app sees it."
      : "The app gets this response; the server is not asked.");
    const status = $("tools-mock-status");
    status.placeholder = rewrite ? "Keep" : "";
    status.title = rewrite ? "Leave empty to keep the real status" : "HTTP status of the fake response";
    $("tools-mock-body").placeholder = original && original.has_body ? "Leave empty to keep the current body" : "";
  }
  function setMode(next) {
    const value = next === "rewrite" ? "rewrite" : "respond";
    const status = $("tools-mock-status");
    if (value !== mode) { statusFor[mode] = status.value; status.value = statusFor[value]; }
    mode = value;
    $("tools-mock-mode-respond").checked = value === "respond";
    $("tools-mock-mode-rewrite").checked = value === "rewrite";
    sync();
  }

  function load(values) {
    clearErrors();
    clearRows();
    mode = values.mode === "rewrite" ? "rewrite" : "respond";
    statusFor.respond = mode === "respond" ? values.status : values.status || "200";
    statusFor.rewrite = mode === "rewrite" ? values.status : "";
    $("tools-mock-mode-respond").checked = mode === "respond";
    $("tools-mock-mode-rewrite").checked = mode === "rewrite";
    $("tools-mock-glob").value = values.url_glob;
    ensureOption($("tools-mock-method"), values.method);
    $("tools-mock-method").value = values.method;
    $("tools-mock-host").value = values.host;
    $("tools-mock-ignore-query").checked = values.ignore_query === true;
    $("tools-mock-status").value = values.status;
    $("tools-mock-headers").value = values.headers;
    $("tools-mock-body").value = values.body;
    $("tools-mock-timing").value = values.timing;
    $("tools-mock-delay").value = values.delay;
    $("tools-mock-body-mode").value = values.body_mode;
    $("tools-mock-note").value = values.note;
    for (const key of core.LISTS) for (const row of values.rows[key] || []) addRow(key, row, false);
    sync();
  }
  function read() {
    const rows = {};
    for (const key of core.LISTS) rows[key] = records.filter((record) => record.key === key).map((record) => (record.empty ? { a: record.a.value, b: record.b ? record.b.value : "", empty: true } : { a: record.a.value, b: record.b ? record.b.value : "" }));
    return {
      mode, url_glob: $("tools-mock-glob").value, method: $("tools-mock-method").value, host: $("tools-mock-host").value,
      ignore_query: $("tools-mock-ignore-query").checked === true, note: $("tools-mock-note").value,
      status: $("tools-mock-status").value, headers: $("tools-mock-headers").value, body: $("tools-mock-body").value,
      timing: $("tools-mock-timing").value, delay: $("tools-mock-delay").value, body_mode: $("tools-mock-body-mode").value, rows,
    };
  }

  function clearErrors() {
    for (const slot of form.querySelectorAll(".tools-field-error")) { slot.hidden = true; slot.textContent = ""; }
    for (const node of invalid) { node.removeAttribute("aria-invalid"); node.removeAttribute("aria-describedby"); }
    invalid = [];
  }
  function target(error) {
    const kind = core.KINDS[error.field];
    if (kind) {
      const rows = records.filter((record) => record.key === error.field);
      if (error.index === null || error.index === undefined) return { input: rows.length ? rows[0].a : addButtonFor(error.field), slot: $(SECTION_ERRORS[kind.section]) };
      const record = rows[error.index];
      if (!record) return null;
      return { input: error.part === "b" && record.b ? record.b : record.a, slot: record.error };
    }
    const pair = SLOTS[error.field];
    return pair ? { input: $(pair[0]), slot: $(pair[1]) } : null;
  }
  /** Each message beside its field (the first per slot), the field marked invalid; focus the first. */
  function showErrors(errors) {
    clearErrors();
    let first = null;
    for (const error of errors) {
      const place = target(error);
      if (!place) continue;
      if (place.slot && place.slot.hidden) { place.slot.textContent = error.message; place.slot.hidden = false; }
      if (place.input && place.input.localName !== "button") {
        place.input.setAttribute("aria-invalid", "true");
        if (place.slot) place.input.setAttribute("aria-describedby", place.slot.getAttribute("id"));
        invalid.push(place.input);
      }
      if (!first && place.input) first = place.input;
    }
    if (first) first.focus();
  }

  function setTitle(value, save, editing) {
    text("tools-mock-form-title", value);
    text("tools-mock-save", save);
    $("tools-mock-cancel-edit").hidden = !editing;
  }
  function reset() { original = null; load(core.empty()); setTitle("New mock", "Add mock", false); }
  function fill(rule) {
    original = core.fill(rule);
    load(original);
    setTitle("Edit mock " + (original.id || ""), "Save changes", true);
  }
  /** A captured request as a new fake response for the same URL, method and host. */
  function fillFromRequest(item) {
    reset();
    const address = string(item.url), query = address.indexOf("?"), method = string(item.method).toUpperCase();
    $("tools-mock-glob").value = query >= 0 ? address.slice(0, query) : address;
    $("tools-mock-ignore-query").checked = query >= 0;
    if (["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"].includes(method)) $("tools-mock-method").value = method;
    $("tools-mock-host").value = item.host ? String(item.host) : "";
    if (item.status) $("tools-mock-status").value = String(item.status);
  }
  function string(value) { return value === undefined || value === null ? "" : String(value); }
  /** {payload, errors}; errors are shown beside their fields before anything is sent. */
  function build() {
    const result = core.payload(read(), original);
    if (result.errors.length) showErrors(result.errors); else clearErrors();
    return result;
  }

  for (const radio of [$("tools-mock-mode-respond"), $("tools-mock-mode-rewrite")]) {
    radio.addEventListener("change", (event) => { if (event.target.checked !== false) setMode(event.target.getAttribute("value")); });
  }
  $("tools-mock-timing").addEventListener("change", sync);
  $("tools-mock-body-mode").addEventListener("change", sync);
  form.addEventListener("click", (event) => {
    const button = event.target && event.target.closest ? event.target.closest("[data-mock-add]") : null;
    if (button && !button.disabled) addRow(button.getAttribute("data-mock-add"), null, true);
  });
  // Typing in a marked field clears its mark and its message.
  form.addEventListener("input", (event) => {
    const node = event.target;
    if (!node || !node.getAttribute || node.getAttribute("aria-invalid") !== "true") return;
    const describedBy = node.getAttribute("aria-describedby"), slot = describedBy ? $(describedBy) : null;
    node.removeAttribute("aria-invalid");
    node.removeAttribute("aria-describedby");
    invalid = invalid.filter((item) => item !== node);
    if (slot && !invalid.some((item) => item.getAttribute("aria-describedby") === slot.getAttribute("id"))) { slot.hidden = true; slot.textContent = ""; }
  });
  sync();

  return Object.freeze({ reset, fill, fillFromRequest, build, read, setMode, addRow, showErrors, clearErrors, mode: () => mode, original: () => original });
}

/** The page code: the core as MOCK_FORM and the controller factory, inside the drawer's scope. */
export function mockFormScript() {
  return "const MOCK_FORM=(" + mockFormCore.toString() + ")();\nconst mockFormController=" + mockFormController.toString() + ";";
}

const ADD_CONDITION = [["query", "Query"], ["request_headers", "Header"], ["body_contains", "Body text"], ["body_json", "JSON field"]];
const ADD_EDIT = [["json_set", "Set field"], ["json_remove", "Remove field"], ["replace", "Replace text"]];

function addButtons(items, label) {
  return `<div class="tools-mock-add" role="group" aria-label="${label}">` +
    items.map(([key, caption]) => `<button type="button" class="btn small" data-mock-add="${key}">+ ${caption}</button>`).join("") + `</div>`;
}
function errorSlot(id) { return `<p class="tools-field-error" id="${id}" role="status" hidden></p>`; }

/** The form markup; `methodOptions` is the page's option list for the method picker. */
export function mockFormMarkup(methodOptions) {
  return `<form class="tools-group" id="tools-mock-form" novalidate aria-labelledby="tools-mock-form-title">` +
    `<h3 id="tools-mock-form-title">New mock</h3>` +
    `<fieldset class="tools-mock-mode" id="tools-mock-mode"><legend class="sr">Response source</legend>` +
      `<label class="tools-mock-mode-option"><input type="radio" name="tools-mock-mode" id="tools-mock-mode-respond" value="respond" checked><span>Fake response</span></label>` +
      `<label class="tools-mock-mode-option"><input type="radio" name="tools-mock-mode" id="tools-mock-mode-rewrite" value="rewrite"><span>Edit real response</span></label>` +
    `</fieldset>` +
    `<p class="tools-hint tools-mock-mode-hint" id="tools-mock-mode-hint">The app gets this response; the server is not asked.</p>` +
    `<h4 class="tools-mock-head" id="tools-mock-match-title">Match request</h4>` +
    `<input id="tools-mock-glob" aria-label="URL glob" title="URL pattern to match" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="URL pattern · */api/items*">` +
    errorSlot("tools-mock-glob-error") +
    `<div class="tools-pair tools-mock-match">` +
      `<select id="tools-mock-method" aria-label="Method" title="HTTP method">${methodOptions}</select>` +
      `<input id="tools-mock-host" aria-label="Host" title="Host to match" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Any host">` +
    `</div>` + errorSlot("tools-mock-match-error") +
    `<label class="tools-check"><input id="tools-mock-ignore-query" type="checkbox"> Ignore query string</label>` +
    `<div class="tools-mock-rows" id="tools-mock-conditions" role="group" aria-label="Request conditions"></div>` +
    addButtons(ADD_CONDITION, "Add a request condition") + errorSlot("tools-mock-conditions-error") +
    `<h4 class="tools-mock-head" id="tools-mock-response-title">Response</h4>` +
    `<div class="tools-mock-response">` +
      `<div><label class="tools-label" for="tools-mock-status">Status</label><input id="tools-mock-status" type="text" inputmode="numeric" autocomplete="off" value="200" title="HTTP status of the fake response"></div>` +
      `<div><label class="tools-label" for="tools-mock-timing">Timing</label><select id="tools-mock-timing" title="When the answer arrives"><option value="none">None</option><option value="delay">Delay</option><option value="hang" id="tools-mock-timing-hang">Hang</option></select></div>` +
      `<div id="tools-mock-delay-field" hidden><label class="tools-label" for="tools-mock-delay">Seconds</label><input id="tools-mock-delay" type="text" inputmode="decimal" autocomplete="off" placeholder="2.5" title="Seconds, above 0 and up to 120"></div>` +
    `</div>` + errorSlot("tools-mock-response-error") +
    `<label class="tools-label" for="tools-mock-headers" id="tools-mock-headers-label">Headers</label>` +
    `<textarea id="tools-mock-headers" rows="2" spellcheck="false" title="One Name: value header per line" placeholder="Content-Type: application/json"></textarea>` +
    errorSlot("tools-mock-headers-error") +
    `<div id="tools-mock-rewrite-headers" hidden>` +
      `<div class="tools-mock-rows" id="tools-mock-remove-headers" role="group" aria-label="Headers to remove"></div>` +
      addButtons([["remove_headers", "Remove header"]], "Remove a response header") + errorSlot("tools-mock-remove-headers-error") +
    `</div>` +
    `<div id="tools-mock-body-choice" hidden><label class="tools-label" for="tools-mock-body-mode">Body</label>` +
      `<select id="tools-mock-body-mode" title="What happens to the real body"><option value="keep">Keep the real body</option><option value="replace">Replace it whole</option><option value="edit">Edit fields or text</option></select></div>` +
    `<div id="tools-mock-body-field"><label class="tools-label" for="tools-mock-body" id="tools-mock-body-label">Response body</label>` +
      `<textarea id="tools-mock-body" rows="3" spellcheck="false" title="Response body, up to 32 KB"></textarea>` + errorSlot("tools-mock-body-error") + `</div>` +
    `<div id="tools-mock-body-edits" hidden>` +
      `<div class="tools-mock-rows" id="tools-mock-edits" role="group" aria-label="Body edits"></div>` +
      addButtons(ADD_EDIT, "Add a body edit") + errorSlot("tools-mock-edits-error") +
    `</div>` +
    `<label class="tools-label" for="tools-mock-note">Note</label>` +
    `<input id="tools-mock-note" type="text" autocomplete="off" placeholder="Optional">` + errorSlot("tools-mock-note-error") +
    `<div class="tools-actions"><button type="submit" class="btn primary" id="tools-mock-save" data-mutate>Add mock</button>` +
    `<button type="button" class="btn" id="tools-mock-cancel-edit" hidden>Cancel editing</button></div>` +
  `</form>`;
}

/** Styles on the drawer's own tokens; no new colors. */
export function mockFormStyles() {
  return `
#tools-mock-form .tools-mock-match{margin-top:8px;grid-template-columns:repeat(2,minmax(0,1fr))}
#tools-mock-form .tools-mock-mode{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px;min-width:0;margin:0;padding:2px;border:0;border-radius:var(--r-sm);background:var(--fill)}
#tools-mock-form .tools-mock-mode-option{position:relative;display:flex;align-items:center;justify-content:center;min-width:0;height:var(--control-small);padding:0 8px;border-radius:calc(var(--r-sm) - 2px);color:var(--text-2);font-size:12px;font-weight:500;cursor:pointer;transition:background-color var(--motion-duration) var(--motion-easing),color var(--motion-duration) var(--motion-easing)}
#tools-mock-form .tools-mock-mode-option span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#tools-mock-form .tools-mock-mode-option input{position:absolute;inset:0;display:block;width:100%;height:100%;margin:0;opacity:0;cursor:pointer}
#tools-mock-form .tools-mock-mode-option:has(input:checked){background:var(--tab-selected-bg);color:var(--text);box-shadow:var(--tab-shadow)}
#tools-mock-form .tools-mock-mode-option:has(input:focus-visible){outline:2px solid var(--accent);outline-offset:-2px}
#tools-mock-form .tools-mock-mode-hint{margin:6px 0 0}
#tools-mock-form .tools-mock-head{margin:14px 0 6px;font-size:12px;font-weight:600;color:var(--text-2)}
#tools-mock-form .tools-mock-rows{display:grid;gap:6px;margin-top:8px}
#tools-mock-form .tools-mock-rows:empty{display:none}
#tools-mock-form .tools-mock-row{display:grid;grid-template-columns:46px minmax(0,1fr) minmax(0,1fr) var(--control-small);align-items:center;gap:6px;min-width:0}
#tools-mock-form .tools-mock-row-single .tools-mock-field{grid-column:span 2}
#tools-mock-form .tools-mock-tag{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-2);font-size:11.5px}
#tools-mock-form .tools-mock-row .tools-field-error{grid-column:2/-1;margin:0}
#tools-mock-form .tools-mock-add{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
#tools-mock-form .tools-mock-add .btn{padding:0 9px;color:var(--text-2)}
#tools-mock-form .tools-mock-icon{width:var(--control-small);padding:0;color:var(--text-2)}
#tools-mock-form .tools-mock-icon::before{content:"";display:block;width:14px;height:14px;margin:auto;background:currentColor;mask:url("${canvasIconData("close")}") center/contain no-repeat;-webkit-mask:url("${canvasIconData("close")}") center/contain no-repeat}
#tools-mock-form .tools-mock-response{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
#tools-mock-form .tools-mock-response .tools-label{margin-top:0}
#tools-mock-form .tools-field-error{margin:4px 0 0;color:var(--t-danger);font-size:12px;line-height:1.4;overflow-wrap:anywhere}
#tools-mock-form [aria-invalid=true]{border-color:var(--t-danger)}
#tools-mock-form .tools-check{margin-top:8px}
#tools-mocks .tools-mock-line{display:flex;align-items:center;gap:6px;min-width:0}
#tools-mocks .tools-mock-line b{min-width:0}
#tools-mocks .tools-mock-badge{flex:none;padding:1px 7px;font-size:11px}
`;
}
