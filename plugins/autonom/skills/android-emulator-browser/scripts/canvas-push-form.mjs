import { canvasIconData } from "./canvas-ui.mjs";
// The push form of the Tools drawer's Simulate tab: set up a push message (autonom.push/v1) in
// plain fields or as JSON, preview what this platform gets, and send it. Two parts, as in
// canvas-mock-form.mjs:
//   pushFormCore()        pure functions: the default message, form values turned into the
//                         checked `simulate` values Send takes, fields <-> JSON, the summary
//                         line, the status line of a reply, the preview text and the state
//                         remembered in this browser.
//   pushFormController()  the DOM wiring: data rows that can be added and removed, Silent,
//                         Edit as JSON, the Android delivery choice, Reset and the preview box.
// Both are plain functions that toolsScript() inlines with Function.prototype.toString, so they
// avoid backticks and never write HTML; node tests import the same code.

/**
 * The pure half. Self-contained (no outer names), so it runs the same in node and inlined in
 * the page. A form is {title, body, subtitle, badge, sound, silent, rows: [{key, value}], json,
 * json_text}, every text as typed.
 */
export function pushFormCore() {
  const LIMIT = 4096, MAX_ROWS = 50, MAX_TEXT = 4096, MAX_JSON_TEXT = 16384;
  const SCHEMA = "autonom.push/v1";
  // The data keys the message model refuses on both platforms (push_message.RESERVED_DATA_*).
  const RESERVED = ["aps", "from", "message_type", "collapse_key"], PREFIXES = ["google.", "gcm."];
  const DEFAULT_TITLE = "Autonom", DEFAULT_BODY = "Hello from the Canvas";
  const SHADE = "“Notification shade only”";
  const APP_NOTE = "Runs the app’s own push code. Needs an emulator image without Play Store.";
  const SHADE_NOTE = "Shows a plain notification from the system. The app’s code does not run.";

  const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const string = (value) => (value === undefined || value === null ? "" : String(value));
  const has = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
  const filled = (value) => typeof value === "string" && value.trim() !== "";
  function utf8(text) { return new TextEncoder().encode(text).length; }
  function plural(count, word) { return count + " " + word + (count === 1 ? "" : "s"); }
  function reserved(key) { return RESERVED.includes(key) || PREFIXES.some((prefix) => key.startsWith(prefix)); }

  /** The message Send starts with on both platforms. */
  function defaults() {
    return { title: DEFAULT_TITLE, body: DEFAULT_BODY, subtitle: "", badge: "", sound: "", silent: false, rows: [], json: false, json_text: "" };
  }
  function empty() {
    return { title: "", body: "", subtitle: "", badge: "", sound: "", silent: false, rows: [], json: false, json_text: "" };
  }

  /** Badge text: empty is none, else a whole number 0 or more. */
  function badge(text) {
    const value = string(text).trim();
    if (value === "") return { value: null };
    if (!/^[0-9]+$/.test(value) || !Number.isSafeInteger(Number(value))) {
      return { error: "Badge must be a whole number, 0 or more. Leave it empty for no badge." };
    }
    return { value: Number(value) };
  }

  /**
   * The message the fields describe, in the field order of a saved message. strict adds the
   * rule that a message says something (a title, a body, data or Silent); the JSON pre-fill
   * leaves it out. Errors: {field, index, part, message}; data errors name the row.
   */
  function fromFields(form, platform, strict) {
    const errors = [], message = {}, source = isObject(form) ? form : {};
    const fail = (field, text, index) => { errors.push({ field, message: text, index: index === undefined ? null : index, part: index === undefined ? null : "key" }); };
    const silent = source.silent === true;
    if (!silent) {
      if (filled(source.title)) message.title = source.title.trim();
      if (platform === "ios" && filled(source.subtitle)) message.subtitle = source.subtitle.trim();
      if (filled(source.body)) message.body = source.body.trim();
    }
    const count = badge(source.badge);
    if (count.error) fail("badge", count.error);
    else if (count.value !== null) message.badge = count.value;
    if (filled(source.sound)) message.sound = source.sound.trim();
    if (silent) message.silent = true;
    const data = {}, seen = new Set();
    (Array.isArray(source.rows) ? source.rows : []).forEach((row, index) => {
      const key = string(row && row.key).trim();
      if (!key) return;
      if (seen.has(key)) { fail("data", "The data key “" + key + "” is used twice. Each key can appear once.", index); return; }
      seen.add(key);
      if (reserved(key)) {
        fail("data", "The data key “" + key + "” is reserved: APNs and FCM use aps, from, message_type, collapse_key and every key starting with google. or gcm. Rename it.", index);
        return;
      }
      data[key] = string(row && row.value);
    });
    if (Object.keys(data).length) message.data = data;
    if (strict && !says(message)) fail("content", "Add a title, a body or a data field, or tick Silent.");
    return { message, errors };
  }
  /** A message says something: a title, a body, data or silent. */
  function says(message) {
    return filled(message.title) || filled(message.body) || (isObject(message.data) && Object.keys(message.data).length > 0) || message.silent === true;
  }

  function parseJson(text) {
    let value;
    try { value = JSON.parse(string(text)); } catch { return { error: "The JSON is not valid." }; }
    if (!isObject(value)) return { error: "The message must be a JSON object." };
    return { value };
  }

  /**
   * What the form sends, before the app and the delivery: {key, body, bytes, errors}. key is
   * "message", or "payload" for a raw APNs object (top-level aps) in JSON mode on iOS.
   */
  function content(form, platform) {
    const ios = platform === "ios", errors = [], source = isObject(form) ? form : {};
    const fail = (field, text) => { errors.push({ field, message: text, index: null, part: null }); };
    let key = "message", body = null;
    if (source.json === true) {
      const parsed = parseJson(source.json_text);
      if (parsed.error) fail("json", parsed.error);
      else if (has(parsed.value, "aps")) {
        if (ios) { key = "payload"; body = parsed.value; }
        else fail("json", "A raw APNs payload (an object with an aps key) works on iOS only. On Android write the message fields instead: title, body, data.");
      } else if (!says(parsed.value)) fail("json", "Add a title, a body or data, or set \"silent\": true.");
      else body = parsed.value;
    } else {
      const built = fromFields(source, platform, true);
      for (const error of built.errors) errors.push(error);
      body = built.message;
    }
    let bytes = 0;
    if (!errors.length) {
      bytes = utf8(JSON.stringify(body));
      if (bytes > LIMIT) fail(source.json === true ? "json" : "size", (key === "payload" ? "The payload" : "The message") + " is larger than 4 KB (" + bytes + " bytes, at most " + LIMIT + "). Shorten the body or the data.");
    }
    return { key, body: errors.length ? null : body, bytes, errors };
  }

  /**
   * The checked simulate values of Send: {values, errors}; values is null when anything is
   * wrong. ctx: {platform, app_id (the form's App field, else the App tab), delivery, include_stopped}.
   */
  function build(form, ctx) {
    const options = isObject(ctx) ? ctx : {}, platform = options.platform === "ios" ? "ios" : "android";
    const shade = platform === "android" && options.delivery === "shade";
    const made = content(form, platform), errors = made.errors.slice();
    if (!errors.length && shade && made.key === "message" && !filled(made.body.title) && !filled(made.body.body)) {
      errors.push({ field: isObject(form) && form.json === true ? "json" : "content", message: SHADE + " needs a title or a body: a data-only push has nothing to show.", index: null, part: null });
    }
    const app = string(options.app_id).trim();
    // Shade delivery posts the notification as the shell: no app is needed.
    if (!app && !shade) {
      errors.push({ field: "app", message: platform === "ios" ? "Enter a bundle id here or on the App tab." : "Enter a package here or on the App tab.", index: null, part: null });
    }
    if (errors.length) return { values: null, errors };
    const values = {};
    if (app) values.app_id = app;
    values[made.key] = made.body;
    if (platform === "android") {
      values.delivery = shade ? "shade" : "app";
      if (!shade) values.include_stopped = options.include_stopped === true;
    }
    return { values, errors };
  }

  /** What Preview asks for: {message} for push.compose, {raw, bytes} for a raw APNs object, or {errors}. */
  function preview(form, platform) {
    const made = content(form, platform);
    if (made.errors.length) return { errors: made.errors };
    return made.key === "payload" ? { raw: made.body, bytes: made.bytes } : { message: made.body };
  }

  /** The JSON text Edit as JSON starts from: {text} or {error, errors}. */
  function toJson(form, platform) {
    const built = fromFields(form, platform, false);
    if (built.errors.length) return { error: built.errors[0].message, errors: built.errors };
    return { text: JSON.stringify(built.message, null, 2) };
  }

  /**
   * JSON back into the fields: {form, unshown} where unshown names what the form has no field
   * for (a value of another type counts: data values are text in the form), or {error}.
   */
  function fromJson(text, platform) {
    const parsed = parseJson(text);
    if (parsed.error) return { error: parsed.error + " Fix it to go back to the fields.", unshown: [] };
    const value = parsed.value, form = empty(), unshown = [];
    for (const key of Object.keys(value)) {
      const item = value[key];
      if (key === "title" || key === "body" || key === "sound" || (key === "subtitle" && platform === "ios")) {
        if (typeof item === "string") form[key] = item; else unshown.push(key);
      } else if (key === "badge") {
        if (Number.isSafeInteger(item) && item >= 0) form.badge = String(item); else unshown.push(key);
      } else if (key === "silent") {
        if (typeof item === "boolean") form.silent = item; else unshown.push(key);
      } else if (key === "data") {
        if (!isObject(item)) { unshown.push(key); continue; }
        for (const name of Object.keys(item)) {
          if (typeof item[name] === "string") form.rows.push({ key: name, value: item[name] });
          else unshown.push("data." + name);
        }
      } else if (key === "schema" && item === SCHEMA) {
        // the form adds nothing for it: every message it sends is this schema
      } else if (key === "aps") unshown.push("aps (a raw APNs payload)");
      else if (key === "subtitle") unshown.push("subtitle (iOS only)");
      else unshown.push(key);
    }
    if (form.rows.length > MAX_ROWS) unshown.push("more than " + MAX_ROWS + " data fields");
    return { form, unshown };
  }

  /** The one-line preview in the block's summary. */
  function summary(form, platform) {
    const source = isObject(form) ? form : {};
    if (source.json === true) return "Custom JSON";
    const rows = (Array.isArray(source.rows) ? source.rows : []).filter((row) => filled(string(row && row.key))).length;
    const data = rows ? plural(rows, "data field") : "";
    const line = (value) => string(value).replace(/\s+/g, " ").trim();
    if (source.silent === true) return "Silent push" + (data ? " · " + data : "");
    const title = line(source.title), body = line(source.body), subtitle = platform === "ios" ? line(source.subtitle) : "";
    const head = title && body ? title + " — " + body : title || body || subtitle;
    if (head) return head + (data ? " · " + data : "");
    return data ? "Data push · " + data : "Nothing to send yet";
  }

  const WARNINGS = {
    push_notifications_denied: "Notifications are not allowed for this app: grant them on the App tab.",
    push_payload_large: "The payload is larger than 4 KB: the Simulator may take it, a real APNs send would not.",
  };
  /** The status line after Send: {text, hint}; hint holds every warning in plain words. */
  function status(reply, platform) {
    const result = isObject(reply) ? reply : {};
    let text;
    if (platform === "ios") text = result.verified === true ? "Push sent and confirmed on the device." : "Push sent (the device cannot confirm it).";
    else if (result.delivery === "shade") {
      text = result.verified === true ? "Notification shown in the shade. The app’s code did not run."
        : "Notification posted, but it was not found in the shade. The app’s code did not run.";
    } else if (result.verified === true) text = "Push delivered. It is in the notification shade.";
    else if (result.verification === "not_in_shade") text = "Push delivered to the app. Nothing in the shade: an app that is open handles it itself.";
    else if (result.verification === "receiver_accepted") text = "Data push delivered to the app.";
    else text = "Push sent.";
    const notes = [];
    for (const item of Array.isArray(result.warnings) ? result.warnings : []) {
      if (!isObject(item) || item.code === "push_shade_only") continue; // the line already says it
      let words = WARNINGS[item.code];
      if (item.code === "slim_push_off") words = "Push is switched off on this Simulator (simulator slim), so it may not show." + (filled(item.hint) ? " " + item.hint : "");
      if (!words) words = string(item.error) || string(item.code);
      if (words && !notes.includes(words)) notes.push(words);
    }
    return { text, hint: notes.join(" ") };
  }

  /** What to do when the emulator did not deliver a push, in the page's own words; null otherwise. */
  function failureHint(error) {
    if (!error || error.code !== "push_not_delivered") return null;
    if (error.reason === "no_root") return "This emulator image has no root, as Play Store images do not. Use an emulator image without Play Store, or choose " + SHADE + " under Delivery.";
    if (error.reason === "not_received") return "The app is stopped or was never started. Open the app once, or tick “Reach a stopped app”.";
    if (error.reason === "no_receiver") return "The app has no push receiver (no Firebase Messaging?). Choose " + SHADE + " under Delivery to show a plain notification.";
    return null;
  }

  /** The preview box: {head, body, notes} from a push.compose reply. */
  function previewText(reply, platform, delivery) {
    const result = isObject(reply) ? reply : {};
    const notes = (Array.isArray(result.notes) ? result.notes : []).map((note) => string(note).replace(/^(ios|android): /, ""));
    if (platform === "ios") {
      const ios = isObject(result.ios) ? result.ios : {};
      const bytes = Number(ios.bytes) || 0;
      return { head: "The Simulator gets this APNs payload (" + bytes + " bytes" + (ios.over_limit === true ? ", above the " + LIMIT + "-byte APNs limit" : "") + "):",
        body: JSON.stringify(isObject(ios.payload) ? ios.payload : {}, null, 2), notes: notes.join(" · ") };
    }
    const android = isObject(result.android) ? result.android : {}, extras = isObject(android.extras) ? android.extras : {};
    if (delivery === "shade") notes.unshift("With " + SHADE + " the shade shows the title and body and the app gets nothing; this is what “To the app” delivers.");
    return { head: "The app gets these FCM extras (" + (android.kind === "data" ? "a data message" : "a notification message") + "):",
      body: Object.keys(extras).map((key) => key + " = " + string(extras[key])).join("\n"), notes: notes.join(" · ") };
  }
  function rawPreviewText(payload, bytes) {
    return { head: "The Simulator gets this APNs payload as it is (" + (Number(bytes) || 0) + " bytes):", body: JSON.stringify(payload, null, 2), notes: "" };
  }

  /** The state kept in this browser (never the app id). */
  function snapshot(form, options, open) {
    const source = isObject(form) ? form : defaults(), choice = isObject(options) ? options : {};
    return JSON.stringify({
      v: 1,
      form: { title: string(source.title), body: string(source.body), subtitle: string(source.subtitle), badge: string(source.badge), sound: string(source.sound),
        silent: source.silent === true, rows: (Array.isArray(source.rows) ? source.rows : []).map((row) => [string(row && row.key), string(row && row.value)]),
        json: source.json === true, json_text: string(source.json_text) },
      delivery: choice.delivery === "shade" ? "shade" : "app", include_stopped: choice.include_stopped === true, open: open === true,
    });
  }
  function validForm(value) {
    if (!isObject(value)) return null;
    for (const name of ["title", "body", "subtitle", "badge", "sound"]) if (typeof value[name] !== "string" || value[name].length > MAX_TEXT) return null;
    if (typeof value.silent !== "boolean" || typeof value.json !== "boolean") return null;
    if (typeof value.json_text !== "string" || value.json_text.length > MAX_JSON_TEXT) return null;
    if (!Array.isArray(value.rows) || value.rows.length > MAX_ROWS) return null;
    const rows = [];
    for (const row of value.rows) {
      if (!Array.isArray(row) || row.length !== 2 || typeof row[0] !== "string" || typeof row[1] !== "string" || row[0].length > MAX_TEXT || row[1].length > MAX_TEXT) return null;
      rows.push({ key: row[0], value: row[1] });
    }
    return { title: value.title, body: value.body, subtitle: value.subtitle, badge: value.badge, sound: value.sound, silent: value.silent, rows, json: value.json, json_text: value.json_text };
  }
  /** The remembered state, or the defaults for anything missing or broken. */
  function restore(text, platform) {
    const fallback = { form: defaults(), delivery: "app", include_stopped: false, open: false };
    let saved;
    try { saved = JSON.parse(string(text) || "null"); } catch { return fallback; }
    if (!isObject(saved) || saved.v !== 1) return fallback;
    return {
      form: validForm(saved.form) || defaults(),
      delivery: platform === "android" && saved.delivery === "shade" ? "shade" : "app",
      include_stopped: saved.include_stopped === true, open: saved.open === true,
    };
  }

  return Object.freeze({ LIMIT, MAX_ROWS, SCHEMA, APP_NOTE, SHADE_NOTE, defaults, badge, reserved, fromFields, content, build, preview, toJson, fromJson,
    summary, status, failureHint, previewText, rawPreviewText, snapshot, restore });
}

/**
 * The DOM half: env brings the page's $, el, makeButton and text helpers, the core, the
 * platform, and read(key) / write(key, value) for this browser's storage (both never throw).
 * It owns the fields, the rows, the JSON switch and the preview box; the page owns Send.
 */
export function pushFormController(env) {
  const $ = env.$, el = env.el, makeButton = env.makeButton, text = env.text, core = env.core;
  const platform = env.platform === "ios" ? "ios" : "android", ios = platform === "ios";
  const KEY = "autonom.canvas.push.v1:" + platform;
  const box = $("tools-push-form"), details = $("tools-push-details"), list = $("tools-push-data");
  const delivery = $("tools-push-delivery"), stopped = $("tools-push-stopped"), jsonSwitch = $("tools-push-json-mode");
  const records = [];
  let jsonMode = false, invalid = [], previewSeq = 0;

  function read() {
    return {
      title: $("tools-push-title").value, body: $("tools-push-body").value, subtitle: ios ? $("tools-push-subtitle").value : "",
      badge: $("tools-push-badge").value, sound: $("tools-push-sound").value, silent: $("tools-push-silent").checked === true,
      rows: records.map((record) => ({ key: record.key.value, value: record.value.value })),
      json: jsonMode, json_text: $("tools-push-payload").value,
    };
  }
  function options() {
    return { delivery: delivery && delivery.value === "shade" ? "shade" : "app", include_stopped: Boolean(stopped && stopped.checked === true) };
  }

  function field(placeholder, value) {
    const node = el("input", "tools-push-field");
    node.setAttribute("type", "text");
    node.setAttribute("autocomplete", "off");
    node.setAttribute("autocapitalize", "off");
    node.setAttribute("spellcheck", "false");
    if (placeholder) node.setAttribute("placeholder", placeholder);
    node.value = value || "";
    return node;
  }
  function syncRows() {
    $("tools-push-data-columns").hidden = records.length === 0;
    $("tools-push-data-add").disabled = records.length >= core.MAX_ROWS;
    records.forEach((record, index) => {
      record.key.setAttribute("aria-label", "Data key " + (index + 1));
      record.value.setAttribute("aria-label", "Data value " + (index + 1));
      record.remove.setAttribute("aria-label", "Remove data field " + (index + 1));
      record.remove.setAttribute("title", "Remove data field " + (index + 1));
    });
  }
  function addRow(values, focus) {
    if (records.length >= core.MAX_ROWS) return null;
    const row = el("div", "tools-push-row");
    const key = field("url", values && values.key), value = field("Value", values && values.value);
    const remove = makeButton("", "btn small tools-push-remove");
    row.appendChild(key);
    row.appendChild(value);
    row.appendChild(remove);
    const record = { row, key, value, remove };
    records.push(record);
    remove.addEventListener("click", () => removeRow(record));
    list.appendChild(row);
    syncRows();
    if (focus) key.focus();
    return record;
  }
  function removeRow(record) {
    const index = records.indexOf(record);
    if (index < 0) return;
    records.splice(index, 1);
    invalid = invalid.filter((node) => node !== record.key && node !== record.value);
    record.row.remove();
    syncRows();
    $("tools-push-data-add").focus();
    changed();
  }

  /** Show what the choices need: Silent turns the alert fields off; JSON mode replaces the fields. */
  function sync() {
    const form = read();
    $("tools-push-fields").hidden = jsonMode;
    $("tools-push-json").hidden = !jsonMode;
    jsonSwitch.checked = jsonMode;
    for (const id of ["tools-push-title", "tools-push-body", "tools-push-subtitle"]) {
      const node = $(id);
      if (node) node.disabled = form.silent;
    }
    if (delivery) {
      const shade = delivery.value === "shade";
      $("tools-push-stopped-field").hidden = shade;
      text("tools-push-delivery-note", shade ? core.SHADE_NOTE : core.APP_NOTE);
    }
    text("tools-push-summary", core.summary(form, platform));
  }
  function save() { env.write(KEY, core.snapshot(read(), options(), details.open === true)); }
  function clearPreview() { previewSeq += 1; $("tools-push-preview").hidden = true; }
  function changed() { sync(); clearPreview(); save(); }
  function setProblem(value) { text("tools-push-json-problem", value); $("tools-push-json-problem").hidden = !value; }

  /** The fields of form (rows rebuilt); the JSON text and mode are set apart. */
  function loadFields(form) {
    $("tools-push-title").value = form.title;
    $("tools-push-body").value = form.body;
    if (ios) $("tools-push-subtitle").value = form.subtitle;
    $("tools-push-badge").value = form.badge;
    $("tools-push-sound").value = form.sound;
    $("tools-push-silent").checked = form.silent === true;
    for (const record of records.splice(0)) record.row.remove();
    for (const row of form.rows) addRow(row, false);
    syncRows();
  }
  function load(form) {
    loadFields(form);
    jsonMode = form.json === true;
    $("tools-push-payload").value = form.json_text;
    setProblem("");
    sync();
  }

  function clearMarks() {
    for (const node of invalid) node.removeAttribute("aria-invalid");
    invalid = [];
  }
  function target(error) {
    if (error.field === "data" && error.index !== null && error.index !== undefined) return records[error.index] ? records[error.index].key : null;
    const ids = { badge: "tools-push-badge", json: "tools-push-payload", app: "tools-push-app", content: jsonMode ? "tools-push-payload" : "tools-push-title", size: jsonMode ? "tools-push-payload" : "tools-push-body" };
    return ids[error.field] ? $(ids[error.field]) : null;
  }
  /** Mark the field of the first error and bring it into view (the block opens if it was closed). */
  function mark(errors) {
    clearMarks();
    const node = errors && errors.length ? target(errors[0]) : null;
    if (!node) return;
    node.setAttribute("aria-invalid", "true");
    invalid.push(node);
    if (details.open !== true) { details.open = true; save(); }
    if (!node.disabled) node.focus();
  }

  /** Edit as JSON switched: on fills the JSON from the fields; off reads it back when the form can show it all. */
  function switchJson() {
    const want = jsonSwitch.checked === true;
    clearMarks();
    if (want === jsonMode) { sync(); return; }
    if (want) {
      const made = core.toJson(read(), platform);
      if (made.error) {
        jsonSwitch.checked = false;
        mark(made.errors);
        env.problem(made.error);
        return;
      }
      $("tools-push-payload").value = made.text;
      jsonMode = true;
      setProblem("");
    } else {
      const back = core.fromJson($("tools-push-payload").value, platform);
      if (back.error || back.unshown.length) {
        jsonSwitch.checked = true;
        setProblem(back.error || "The form cannot show " + back.unshown.join(", ") + ". Edit them here, or remove them to go back to the fields.");
        return;
      }
      loadFields(back.form);
      jsonMode = false;
      setProblem("");
    }
    changed();
  }

  function reset() {
    clearMarks();
    load(core.defaults());
    clearPreview();
    save();
  }

  /** Send's values for appId (the App field, else the App tab): {values, errors}, the first error marked. */
  function build(appId) {
    const app = $("tools-push-app").value.trim() || String(appId || "").trim();
    const choice = options();
    const result = core.build(read(), { platform, app_id: app, delivery: choice.delivery, include_stopped: choice.include_stopped });
    if (result.errors.length) mark(result.errors); else clearMarks();
    return result;
  }
  /** Preview's request: {message, seq} for push.compose, {raw, bytes} for a raw payload, or {error}. */
  function previewRequest() {
    const request = core.preview(read(), platform);
    if (request.errors) { mark(request.errors); return { error: request.errors[0].message }; }
    clearMarks();
    clearPreview();
    return Object.assign({ seq: previewSeq }, request);
  }
  function showText(shown) {
    text("tools-push-preview-head", shown.head);
    text("tools-push-preview-body", shown.body);
    text("tools-push-preview-notes", shown.notes);
    $("tools-push-preview-notes").hidden = !shown.notes;
    $("tools-push-preview").hidden = false;
  }
  /** A compose reply for the request seq; a reply for a form changed since is dropped. */
  function showPreview(result, seq) {
    if (seq !== previewSeq) return false;
    showText(core.previewText(result, platform, options().delivery));
    return true;
  }
  function showRaw(payload, bytes) { showText(core.rawPreviewText(payload, bytes)); }

  box.addEventListener("input", (event) => {
    const node = event.target;
    if (node === jsonSwitch) return;
    if (node && node.getAttribute && node.getAttribute("aria-invalid") === "true") {
      node.removeAttribute("aria-invalid");
      invalid = invalid.filter((item) => item !== node);
    }
    changed();
  });
  box.addEventListener("change", (event) => {
    if (event.target === jsonSwitch) switchJson(); else changed();
  });
  // A single-line field never sends: Send is the button.
  box.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && event.target && event.target.localName === "input") event.preventDefault();
  });
  details.addEventListener("toggle", save);
  $("tools-push-data-add").addEventListener("click", () => { if (addRow(null, true)) changed(); });
  $("tools-push-reset").addEventListener("click", reset);

  const saved = core.restore(env.read(KEY), platform);
  if (delivery) delivery.value = saved.delivery;
  if (stopped) stopped.checked = saved.include_stopped;
  load(saved.form);
  details.open = saved.open;

  return Object.freeze({ build, previewRequest, showPreview, showRaw, reset, read, options, addRow, mode: () => (jsonMode ? "json" : "fields") });
}

/** The page code: the core as PUSH_FORM and the controller factory, inside the drawer's scope. */
export function pushFormScript() {
  return "const PUSH_FORM=(" + pushFormCore.toString() + ")();\nconst pushFormController=" + pushFormController.toString() + ";";
}

/** The block under the Push notification row: static markup only; the script fills the values. */
export function pushFormMarkup(platform) {
  const ios = platform === "ios";
  const label = (id, caption) => `<label class="tools-label" for="${id}">${caption}</label>`;
  const text = (id, extra = "") => `<input id="${id}" type="text" autocomplete="off"${extra}>`;
  const code = (id, extra = "") => text(id, ` autocapitalize="off" spellcheck="false"${extra}`);
  return `<details class="tools-sim-details tools-push-details" id="tools-push-details">` +
    `<summary><span class="sr">Message: </span><span class="tools-push-summary" id="tools-push-summary">Autonom — Hello from the Canvas</span></summary>` +
    `<div class="tools-push-form" id="tools-push-form" role="group" aria-label="Push message">` +
      label("tools-push-app", "App") + code("tools-push-app", ` placeholder="Same as the App tab" title="${ios ? "Bundle id" : "Package"}; empty uses the App tab"`) +
      `<div id="tools-push-fields">` +
        label("tools-push-title", "Title") + text("tools-push-title", ` value="Autonom"`) +
        label("tools-push-body", "Body") + `<textarea id="tools-push-body" rows="2">Hello from the Canvas</textarea>` +
        (ios ? label("tools-push-subtitle", "Subtitle") + text("tools-push-subtitle") : "") +
        `<div class="tools-pair">` +
          `<div>${label("tools-push-badge", "Badge")}${code("tools-push-badge", ` inputmode="numeric" placeholder="None" title="A whole number, 0 or more"`)}</div>` +
          `<div>${label("tools-push-sound", "Sound")}${code("tools-push-sound", ` placeholder="default"`)}</div>` +
        `</div>` +
        `<label class="tools-check"><input id="tools-push-silent" type="checkbox"> Silent (data only, no banner)</label>` +
        `<h4 id="tools-push-data-title">Data</h4>` +
        `<div class="tools-push-row tools-push-row-head" id="tools-push-data-columns" aria-hidden="true" hidden><span>Key</span><span>Value</span></div>` +
        `<div class="tools-push-rows" id="tools-push-data" role="group" aria-labelledby="tools-push-data-title"></div>` +
        `<button type="button" class="btn small tools-push-add" id="tools-push-data-add">+ Add field</button>` +
        `<p class="tools-note">The key your app reads a link from (for example <code>url</code>) goes here.</p>` +
      `</div>` +
      `<div id="tools-push-json" hidden>` +
        label("tools-push-payload", "Message as JSON") +
        `<textarea id="tools-push-payload" rows="8" autocapitalize="off" spellcheck="false" title="JSON, up to 4 KB"></textarea>` +
        `<p class="tools-note">${ios ? "A push message, or a raw APNs payload (an object with an aps key) sent as it is." : "A push message: title, body, data and the other message fields."}</p>` +
        `<p class="tools-note tools-push-problem" id="tools-push-json-problem" role="status" hidden></p>` +
      `</div>` +
      (ios ? "" :
        label("tools-push-delivery", "Delivery") +
        `<select id="tools-push-delivery"><option value="app">To the app</option><option value="shade">Notification shade only</option></select>` +
        `<p class="tools-note" id="tools-push-delivery-note">Runs the app’s own push code. Needs an emulator image without Play Store.</p>` +
        `<div id="tools-push-stopped-field"><label class="tools-check"><input id="tools-push-stopped" type="checkbox"> Reach a stopped app</label></div>`) +
      `<label class="tools-check"><input id="tools-push-json-mode" type="checkbox"> Edit as JSON</label>` +
      `<div class="tools-actions"><button type="button" class="btn" id="tools-push-preview-run">Preview</button>` +
        `<button type="button" class="btn small tools-push-reset" id="tools-push-reset">Reset</button></div>` +
      `<div class="tools-push-preview" id="tools-push-preview" aria-live="polite" hidden><p class="tools-note" id="tools-push-preview-head"></p>` +
        `<pre class="tools-pre" id="tools-push-preview-body"></pre><p class="tools-note" id="tools-push-preview-notes" hidden></p></div>` +
    `</div>` +
  `</details>`;
}

/** Styles on the drawer's own tokens and classes; no new colors. */
export function pushFormStyles() {
  return `
#tools-push-details>summary{padding:5px 0}
.tools-push-summary{display:inline-block;max-width:calc(100% - 18px);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:bottom;color:var(--text)}
#tools-push-form{margin-top:2px}
#tools-push-form>.tools-label:first-child{margin-top:4px}
#tools-push-form .tools-pair{margin-top:0}
#tools-push-form .tools-pair .tools-label{margin-top:8px}
#tools-push-form #tools-push-body{min-height:52px;font:inherit;line-height:1.45}
#tools-push-form #tools-push-payload{min-height:140px}
#tools-push-form h4{margin:12px 0 0;font-size:12px;font-weight:500;color:var(--text-2)}
#tools-push-form .tools-check{margin-top:10px}
#tools-push-form .tools-note code{font:11.5px var(--mono)}
#tools-push-form .tools-push-rows{display:grid;gap:6px;margin-top:6px}
#tools-push-form .tools-push-rows:empty{display:none}
#tools-push-form .tools-push-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr) var(--control-small);align-items:center;gap:6px;min-width:0}
#tools-push-form .tools-push-row-head{margin-top:6px;font-size:11.5px;color:var(--text-2)}
#tools-push-form .tools-push-add{margin-top:6px;color:var(--text-2)}
#tools-push-form .tools-push-remove{width:var(--control-small);padding:0;color:var(--text-2)}
#tools-push-form .tools-push-remove::before{content:"";display:block;width:14px;height:14px;margin:auto;background:currentColor;mask:url("${canvasIconData("close")}") center/contain no-repeat;-webkit-mask:url("${canvasIconData("close")}") center/contain no-repeat}
#tools-push-form .tools-push-reset{border-color:transparent;background:transparent;color:var(--text-2)}
#tools-push-form .tools-push-problem{color:var(--t-danger)}
#tools-push-form [aria-invalid=true]{border-color:var(--t-danger)}
#tools-push-form .tools-push-preview .tools-pre{margin-top:6px}
`;
}
