#!/usr/bin/env node
/**
 * Layout check of a pre-test pack's offline view (requirement tp-019, scenario tp-019-S02).
 *
 * Not part of `node --test tests/*.test.mjs`: it opens <pack>/index.html from disk in headless
 * Chromium through Playwright (from PLAYWRIGHT_ROOT, default /Users/gmh-basket/pr/defuddle-eval,
 * found the same way as canvas_tools_live.mjs) and touches no device:
 *
 *   node tests/live/pretest_view_check.mjs <pack> --out DIR
 *   node tests/live/pretest_view_check.mjs --self-test   (the pure helpers only; no browser)
 *
 * At 1440x900 and 390x844, each in the light and the dark color scheme, it asserts:
 *   - document.documentElement.scrollWidth <= innerWidth (no sideways scrolling);
 *   - every section is present: the verdict header (header.hero) and #findings, #checks,
 *     #health, #review, #coverage, #files, #timing, plus the embedded #pretest-data JSON,
 *     whose verdict equals summary.json's;
 *   - the page asked for nothing but file: URLs (every other request is aborted and
 *     counted), no image failed to load, and no script error was raised;
 *   - the page saw the requested color scheme.
 * One full-page screenshot per run: view-<width>x<height>-<scheme>.png (four in all).
 *
 * The pack must be a pre-test pack (manifest.json kind autonom.pretest-pack/v1) holding a
 * regular index.html and summary.json. Writes <out>/view-check.json (owner-only) with
 * per-run measurements; the widest overflowing elements are named by tag, id and class
 * only, never by their text. Prints one JSON line {ok, report, screenshots, runs}. Exits 0
 * when every run passed, 1 otherwise, 2 on bad arguments or a path that is not a pack.
 */
import assert from "node:assert/strict";
import { existsSync, lstatSync, realpathSync } from "node:fs";
import { chmod, mkdir, readFile, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { parseArgs } from "node:util";

const PACK_KIND = "autonom.pretest-pack/v1";
const DEFAULT_PLAYWRIGHT_ROOT = "/Users/gmh-basket/pr/defuddle-eval";
const VIEWPORTS = [{ width: 1440, height: 900 }, { width: 390, height: 844 }];
const SCHEMES = ["light", "dark"];
const SECTIONS = ["findings", "checks", "health", "review", "coverage", "files", "timing"];
const LOAD_TIMEOUT_MS = 30_000;
const USAGE = `usage: pretest_view_check.mjs <pack> --out DIR
       pretest_view_check.mjs --self-test`;

// --- pure helpers (checked by --self-test) --------------------------------------------------

/** A JSON error for stderr, shaped like the CLI's: ok false, error_code, error, hint. */
function errorPayload(code, message, hint) {
  return { ok: false, error_code: code, error: String(message), hint };
}

/** The screenshot file name of one run. */
function shotName(viewport, scheme) {
  return `view-${viewport.width}x${viewport.height}-${scheme}.png`;
}

/** Whether a request URL stays offline: only file: (and data:/blob: made by the page). */
function isOfflineUrl(url) {
  return /^(file|data|blob|about):/i.test(String(url ?? ""));
}

/**
 * The verdict of one run from the measurements the page reported:
 * the list of reasons it failed (empty when it passed).
 */
function runFailures(m, expected) {
  const reasons = [];
  if (!(m.scrollWidth <= m.innerWidth)) reasons.push("horizontal_overflow");
  const missing = SECTIONS.filter((id) => !m.sections?.[id]);
  if (missing.length) reasons.push(`missing_sections:${missing.join(",")}`);
  if (!m.hero) reasons.push("missing_verdict_header");
  if (!m.dataPresent) reasons.push("missing_pretest_data");
  else if (expected?.verdict !== undefined && m.dataVerdict !== expected.verdict) reasons.push("verdict_mismatch");
  if (m.externalRequests > 0) reasons.push("network_request");
  if (m.externalRefs > 0) reasons.push("network_reference");
  if (m.brokenImages > 0) reasons.push("broken_image");
  if (m.pageErrors > 0) reasons.push("script_error");
  if (m.schemeMatches === false) reasons.push("color_scheme_not_applied");
  return reasons;
}

/** A short, text-free name for an element: tag#id.class1.class2. */
function describeElement(tag, id, className) {
  const classes = String(className ?? "").trim().split(/\s+/).filter(Boolean).slice(0, 3);
  return `${String(tag).toLowerCase()}${id ? `#${id}` : ""}${classes.map((c) => `.${c}`).join("")}`.slice(0, 120);
}

/** An error message for the evidence: cut, with any absolute home path shortened. */
function errorText(error, max = 300) {
  return String(error?.message ?? error).replace(/\/(Users|home)\/[^/\s]+/g, "~").slice(0, max);
}

function selfTest() {
  assert.equal(shotName({ width: 390, height: 844 }, "dark"), "view-390x844-dark.png");
  assert.equal(isOfflineUrl("file:///tmp/pack/index.html"), true);
  assert.equal(isOfflineUrl("data:image/png;base64,AA"), true);
  assert.equal(isOfflineUrl("https://example.com/x.js"), false);
  assert.equal(isOfflineUrl("http://127.0.0.1/"), false);
  assert.equal(isOfflineUrl(undefined), false);

  const good = {
    scrollWidth: 390, innerWidth: 390, hero: true, dataPresent: true, dataVerdict: "fail",
    sections: Object.fromEntries(SECTIONS.map((id) => [id, true])),
    externalRequests: 0, externalRefs: 0, brokenImages: 0, pageErrors: 0, schemeMatches: true,
  };
  assert.deepEqual(runFailures(good, { verdict: "fail" }), []);
  assert.deepEqual(runFailures({ ...good, scrollWidth: 391 }, { verdict: "fail" }), ["horizontal_overflow"]);
  assert.deepEqual(runFailures({ ...good, sections: { ...good.sections, review: false, timing: false } }, {}),
    ["missing_sections:review,timing"]);
  assert.deepEqual(runFailures({ ...good, hero: false }, {}), ["missing_verdict_header"]);
  assert.deepEqual(runFailures({ ...good, dataPresent: false }, { verdict: "fail" }), ["missing_pretest_data"]);
  assert.deepEqual(runFailures(good, { verdict: "pass" }), ["verdict_mismatch"]);
  assert.deepEqual(runFailures({ ...good, externalRequests: 1 }, {}), ["network_request"]);
  assert.deepEqual(runFailures({ ...good, externalRefs: 1 }, {}), ["network_reference"]);
  assert.deepEqual(runFailures({ ...good, brokenImages: 2 }, {}), ["broken_image"]);
  assert.deepEqual(runFailures({ ...good, pageErrors: 1 }, {}), ["script_error"]);
  assert.deepEqual(runFailures({ ...good, schemeMatches: false }, {}), ["color_scheme_not_applied"]);
  // A measurement that never arrived is a failure, not a pass.
  assert.ok(runFailures({}, {}).includes("horizontal_overflow"));

  assert.equal(describeElement("DIV", "checks", "list  wide extra more"), "div#checks.list.wide.extra");
  assert.equal(describeElement("pre", "", undefined), "pre");
  assert.equal(errorText(new Error("no file /Users/someone/pack/x")), "no file ~/pack/x");
  assert.equal(errorText("x".repeat(400)).length, 300);

  const refused = errorPayload("not_a_pretest_pack", "nope", "hint");
  assert.deepEqual(Object.keys(refused), ["ok", "error_code", "error", "hint"]);
  assert.equal(refused.ok, false);
}

// --- arguments ------------------------------------------------------------------------------

function refuse(code, message, hint) {
  console.error(JSON.stringify(errorPayload(code, message, hint)));
  process.exit(2);
}

let parsed;
try {
  parsed = parseArgs({
    allowPositionals: true,
    options: {
      out: { type: "string" },
      "self-test": { type: "boolean", default: false },
      help: { type: "boolean", short: "h", default: false },
    },
  });
} catch (error) {
  refuse("usage_error", error.message, USAGE);
}
const { values: args, positionals } = parsed;

if (args.help) {
  console.log(USAGE);
  process.exit(0);
}
if (args["self-test"]) {
  selfTest();
  console.log("ok: pretest_view_check self-test");
  process.exit(0);
}
if (positionals.length !== 1) refuse("usage_error", "pass exactly one pack directory", USAGE);
if (!args.out) refuse("usage_error", "--out DIR is required", USAGE);

const outDir = resolve(args.out);

/** lstat that answers null for a missing path. */
function stat(path) {
  try {
    return lstatSync(path);
  } catch {
    return null;
  }
}

// The pack root may sit behind a symlinked parent (/tmp on macOS); files inside it may not.
let pack;
try {
  pack = realpathSync(resolve(positionals[0]));
} catch {
  refuse("not_a_pretest_pack", "the pack path does not exist", "Pass the directory 'autonom pretest pack' printed.");
}
const packStat = stat(pack);
if (!packStat?.isDirectory()) refuse("not_a_pretest_pack", "the pack path is not a directory", "Pass the directory 'autonom pretest pack' printed.");
for (const name of ["manifest.json", "index.html", "summary.json"]) {
  if (!stat(join(pack, name))?.isFile()) {
    refuse("not_a_pretest_pack", `the pack has no regular ${name}`, "Pass a finished pack; check it with 'autonom report verify <pack>'.");
  }
}
let manifest;
let summary;
try {
  manifest = JSON.parse(await readFile(join(pack, "manifest.json"), "utf8"));
  summary = JSON.parse(await readFile(join(pack, "summary.json"), "utf8"));
} catch (error) {
  refuse("not_a_pretest_pack", `cannot read the pack: ${errorText(error)}`, "Check it with 'autonom report verify <pack>'.");
}
if (manifest?.kind !== PACK_KIND) refuse("not_a_pretest_pack", `manifest kind is not ${PACK_KIND}`, "Pass a pre-test pack, not a flow report bundle.");
const outStat = stat(outDir);
if (outStat && !outStat.isDirectory()) refuse("output_not_writable", "--out is not a directory", "Pass a new or existing directory.");

// --- the browser run ------------------------------------------------------------------------

const playwrightRoot = process.env.PLAYWRIGHT_ROOT || DEFAULT_PLAYWRIGHT_ROOT;

async function loadPlaywright() {
  const entry = join(playwrightRoot, "node_modules/playwright/index.mjs");
  if (!existsSync(entry)) throw new Error(`Playwright not found at ${entry}; set PLAYWRIGHT_ROOT`);
  return await import(pathToFileURL(entry).href);
}

async function measure(page, scheme) {
  return await page.evaluate(({ sections, scheme }) => {
    const root = document.documentElement;
    const width = window.innerWidth;
    const wide = [];
    for (const element of document.body.querySelectorAll("*")) {
      const rect = element.getBoundingClientRect();
      if (rect.width > 0 && rect.right > width + 1) wide.push({ element, right: rect.right });
    }
    wide.sort((a, b) => b.right - a.right);
    const data = document.getElementById("pretest-data");
    let dataVerdict = null;
    let dataPresent = false;
    try {
      dataVerdict = JSON.parse(data?.textContent ?? "null")?.verdict ?? null;
      dataPresent = Boolean(data);
    } catch {
      dataPresent = false;
    }
    const images = [...document.images];
    // A Content-Security-Policy blocks a fetch before any request event, so links and sources
    // are also read from the document: none may resolve to a network URL.
    const urls = [];
    for (const element of document.querySelectorAll("[src],[href],[srcset],[poster],[action]")) {
      for (const name of ["src", "href", "poster", "action"]) {
        if (element.hasAttribute(name)) urls.push(element[name] || element.getAttribute(name));
      }
      if (element.hasAttribute("srcset")) urls.push(...element.getAttribute("srcset").split(",").map((part) => part.trim().split(/\s+/)[0]));
    }
    const externalRefs = urls.filter((url) => /^[a-z][a-z0-9+.-]*:/i.test(String(url)) && !/^(file|data|blob|about):/i.test(String(url))).length;
    return {
      scrollWidth: root.scrollWidth,
      innerWidth: width,
      scrollHeight: root.scrollHeight,
      sections: Object.fromEntries(sections.map((id) => [id, Boolean(document.getElementById(id))])),
      hero: Boolean(document.querySelector("header.hero h1")),
      dataPresent,
      dataVerdict,
      externalRefs,
      images: images.length,
      brokenImages: images.filter((image) => image.complete && image.naturalWidth === 0).length,
      schemeMatches: window.matchMedia(`(prefers-color-scheme: ${scheme})`).matches,
      overflowing: wide.slice(0, 5).map(({ element, right }) => ({
        element: [element.tagName, element.id, typeof element.className === "string" ? element.className : ""],
        right: Math.round(right),
      })),
    };
  }, { sections: SECTIONS, scheme });
}

const report = {
  pack, playwright_root: playwrightRoot, started_at: new Date().toISOString(),
  expected_verdict: summary?.verdict ?? null, runs: [], ok: false,
};

try {
  await mkdir(outDir, { recursive: true, mode: 0o700 });
  await chmod(outDir, 0o700).catch(() => null);
  const { chromium } = await loadPlaywright();
  const browser = await chromium.launch({ headless: true });
  try {
    for (const viewport of VIEWPORTS) {
      for (const scheme of SCHEMES) {
        const entry = { viewport: `${viewport.width}x${viewport.height}`, scheme, screenshot: null, ok: false };
        report.runs.push(entry);
        const context = await browser.newContext({ viewport, colorScheme: scheme });
        let externalRequests = 0;
        let pageErrors = 0;
        await context.route("**/*", (route) => {
          if (isOfflineUrl(route.request().url())) return route.continue();
          externalRequests += 1;
          return route.abort();
        });
        const page = await context.newPage();
        page.on("pageerror", () => { pageErrors += 1; });
        try {
          await page.goto(pathToFileURL(join(pack, "index.html")).href, { waitUntil: "load", timeout: LOAD_TIMEOUT_MS });
          await page.waitForTimeout(300);
          const m = await measure(page, scheme);
          Object.assign(m, { externalRequests, pageErrors });
          m.overflowing = m.overflowing.map(({ element, right }) => ({ element: describeElement(...element), right }));
          const file = shotName(viewport, scheme);
          await page.screenshot({ path: join(outDir, file), fullPage: true });
          await chmod(join(outDir, file), 0o600).catch(() => null);
          entry.screenshot = file;
          entry.failures = runFailures(m, { verdict: report.expected_verdict });
          Object.assign(entry, {
            scroll_width: m.scrollWidth, inner_width: m.innerWidth, scroll_height: m.scrollHeight,
            sections: m.sections, verdict_header: m.hero, data_verdict: m.dataVerdict,
            images: m.images, broken_images: m.brokenImages, external_requests: externalRequests,
            external_references: m.externalRefs,
            page_errors: pageErrors, scheme_matches: m.schemeMatches, overflowing: m.overflowing,
          });
          entry.ok = entry.failures.length === 0;
        } catch (error) {
          entry.error = errorText(error);
        } finally {
          await context.close();
        }
      }
    }
  } finally {
    await browser.close();
  }
} catch (error) {
  report.error = errorText(error);
}

report.ok = !report.error && report.runs.length === VIEWPORTS.length * SCHEMES.length && report.runs.every((run) => run.ok);
report.finished_at = new Date().toISOString();
const reportFile = join(outDir, "view-check.json");
try {
  await mkdir(outDir, { recursive: true, mode: 0o700 });
  await writeFile(reportFile, `${JSON.stringify(report, null, 2)}\n`, { mode: 0o600 });
  await chmod(reportFile, 0o600).catch(() => null);
} catch (error) {
  report.ok = false;
  report.error = report.error ?? errorText(error);
}
console.log(JSON.stringify({
  ok: report.ok, report: reportFile,
  screenshots: report.runs.map((run) => run.screenshot).filter(Boolean),
  runs: report.runs.map((run) => ({ viewport: run.viewport, scheme: run.scheme, ok: run.ok, failures: run.failures ?? null, error: run.error ?? null })),
  error: report.error ?? null,
}));
process.exitCode = report.ok ? 0 : 1;
