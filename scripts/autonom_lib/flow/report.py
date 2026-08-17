"""Evidence renderers: run manifest → self-contained HTML and JUnit XML.

The manifest is the protocol; these are only renderers (§9). Hard rules:

- the HTML is fully self-contained — screenshots ride inline as ``data:``
  URIs, styles are inline, and a restrictive CSP meta tag refuses every
  external fetch, so opening a report can never phone anywhere;
- every dynamic string is escaped — UI text and log lines are attacker-ish
  input (an app can name a button ``<script>``);
- rendering never mutates: a report can be rebuilt from the same run
  forever.
"""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape, quoteattr

_MAX_INLINE_IMAGE = 2_000_000  # bytes of PNG we are willing to inline


def load_manifest(run_dir: Path) -> dict[str, Any]:
    from .. import errors
    path = run_dir / "manifest.json"
    if not path.is_file():
        raise errors.AutonomError(
            errors.FLOW_FILE_NOT_FOUND,
            f"no manifest at {path}",
            hint="Reports need a flow run recorded by 0.24.0 or later.",
            file=str(path),
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _inline_image(path: Path) -> str | None:
    try:
        blob = path.read_bytes()
    except OSError:
        return None
    if len(blob) > _MAX_INLINE_IMAGE:
        return None
    return "data:image/png;base64," + base64.b64encode(blob).decode("ascii")


def render_html(manifest: dict[str, Any], artifacts_dir: Path) -> str:
    e = html.escape
    status = manifest.get("status", "unknown")
    color = {"passed": "#1a7f37", "failed": "#b42318"}.get(status, "#555")
    rows = []
    for step in manifest.get("steps", []):
        selector = step.get("selector")
        detail_bits = []
        if step.get("label"):
            detail_bits.append(e(str(step["label"])))
        if selector:
            detail_bits.append(f"<code>{e(json.dumps(selector, ensure_ascii=False))}</code>")
        if step.get("skip_reason"):
            detail_bits.append(f"skipped: {e(str(step['skip_reason']))}")
        if step.get("error"):
            detail_bits.append(
                f"<b>{e(str(step.get('error_code', '')))}"
                f"</b> ({e(str(step.get('failure_class', '')))}) — "
                f"{e(str(step['error']))}")
        badge = {"passed": "✓", "failed": "✗", "skipped": "○"}.get(
            step.get("status", ""), "·")
        rows.append(
            "<tr>"
            f"<td>{step.get('index', '')}</td>"
            f"<td><code>{e(str(step.get('command', '')))}</code>"
            + (f" <small>({e(str(step.get('hook')))})</small>" if step.get("hook") else "")
            + "</td>"
            f"<td class='s-{e(str(step.get('status', '')))}'>{badge} {e(str(step.get('status', '')))}</td>"
            f"<td>{step.get('duration_ms', 0)}&nbsp;ms ×{step.get('attempts', 1)}</td>"
            f"<td>{'<br>'.join(detail_bits)}</td>"
            "</tr>")

    images = []
    for relative in manifest.get("artifacts", []):
        if not relative.endswith(".png"):
            continue
        uri = _inline_image(artifacts_dir / relative)
        if uri:
            images.append(
                f"<figure><img src='{uri}' alt='{e(relative)}'>"
                f"<figcaption>{e(relative)}</figcaption></figure>")

    failure = manifest.get("primary_error")
    failure_html = ""
    if failure:
        failure_html = (
            "<h2>Failure</h2><p>"
            f"step {failure.get('step_index')} "
            f"<code>{e(str(failure.get('command', '')))}</code> — "
            f"<b>{e(str(failure.get('error_code', '')))}</b> "
            f"({e(str(failure.get('failure_class', '')))})<br>"
            f"{e(str(failure.get('error', '')))}</p>")
    hooks = manifest.get("hook_failures") or []
    hooks_html = ""
    if hooks:
        items = "".join(
            f"<li><code>{e(str(h.get('command', '')))}</code> — "
            f"{e(str(h.get('error_code', '')))}: {e(str(h.get('error', '')))}</li>"
            for h in hooks)
        hooks_html = f"<h2>Cleanup failures (did not mask the outcome)</h2><ul>{items}</ul>"

    sensitive = ("<p class='sensitive'>⚠ This run used secrets or sensitive "
                 "input; screenshots may show private data. Local file — "
                 "share deliberately.</p>" if manifest.get("sensitive") else "")

    return f"""<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>{e(str(manifest.get('flow_name', 'flow run')))} — {e(status)}</title>
<style>
  body {{ font: 14px/1.5 -apple-system, system-ui, sans-serif; margin: 2rem auto;
         max-width: 60rem; padding: 0 1rem; color: #1f2328; }}
  table {{ border-collapse: collapse; width: 100%; }}
  td, th {{ border-bottom: 1px solid #d0d7de; padding: .4rem .6rem;
            text-align: left; vertical-align: top; }}
  code {{ background: #f6f8fa; padding: .1em .3em; border-radius: 4px; }}
  .status {{ color: {color}; font-weight: 700; }}
  .s-failed {{ color: #b42318; }} .s-passed {{ color: #1a7f37; }}
  .s-skipped {{ color: #9a6700; }}
  figure {{ margin: 1rem 0; }} img {{ max-width: 100%; border: 1px solid #d0d7de; }}
  figcaption {{ font-size: .85em; color: #57606a; }}
  .sensitive {{ background: #fff8c5; padding: .5rem .8rem; border-radius: 6px; }}
</style>
<h1>{e(str(manifest.get('flow_name', 'flow run')))}
    <span class="status">{e(status)}</span></h1>
<p><code>{e(str(manifest.get('flow_path', '')))}</code> ·
   {e(str(manifest.get('platform', '')))} {e(str(manifest.get('target_id', '')))} ·
   app <code>{e(str(manifest.get('app_id', '')))}</code> ·
   run <code>{e(str(manifest.get('run_id', '')))}</code> ·
   session <code>{e(str(manifest.get('session_id', '')))}</code></p>
{sensitive}
{failure_html}
{hooks_html}
<h2>Timeline</h2>
<table><tr><th>#</th><th>command</th><th>status</th><th>time</th><th>detail</th></tr>
{''.join(rows)}
</table>
<h2>Screenshots</h2>
{''.join(images) or '<p>none captured</p>'}
<h2>Reproduce</h2>
<p><code>{e(str(manifest.get('reproduction', '')))}</code></p>
"""


def render_suite_html(manifests: list[dict[str, Any]]) -> str:
    """One page for a whole suite run: totals, then every flow with its steps.

    Same containment rules as the single-run report (no external fetch,
    everything escaped). Screenshots stay in the per-run reports — a suite
    page inlining 46 runs' images would be hundreds of megabytes; each row
    links to its own report instead.
    """
    e = html.escape
    passed = [m for m in manifests if m.get("status") == "passed"]
    failed = [m for m in manifests if m.get("status") != "passed"]
    total_ms = sum(step.get("duration_ms", 0)
                   for m in manifests for step in m.get("steps", []))
    status = "failed" if failed else "passed"
    color = "#b42318" if failed else "#1a7f37"

    def step_rows(manifest: dict[str, Any]) -> str:
        rows = []
        for step in manifest.get("steps", []):
            badge = {"passed": "✓", "failed": "✗", "skipped": "○"}.get(
                step.get("status", ""), "·")
            detail = ""
            if step.get("label"):
                detail = e(str(step["label"]))
            elif step.get("selector"):
                detail = f"<code>{e(json.dumps(step['selector'], ensure_ascii=False))}</code>"
            if step.get("error"):
                detail += (f"<br><b>{e(str(step.get('error_code','')))}</b> "
                           f"{e(str(step['error']))}")
            if step.get("skip_reason"):
                detail += f"<br><i>skipped: {e(str(step['skip_reason']))}</i>"
            rows.append(
                f"<tr><td>{step.get('index','')}</td>"
                f"<td><code>{e(str(step.get('command','')))}</code></td>"
                f"<td class='s-{e(str(step.get('status','')))}'>{badge}</td>"
                f"<td>{step.get('duration_ms',0)}&nbsp;ms</td>"
                f"<td>{detail}</td></tr>")
        return "".join(rows)

    blocks = []
    for manifest in manifests:
        flow_status = manifest.get("status", "unknown")
        duration = sum(s.get("duration_ms", 0) for s in manifest.get("steps", []))
        open_attr = " open" if flow_status != "passed" else ""
        blocks.append(
            f"<details{open_attr}><summary class='s-{e(flow_status)}'>"
            f"<b>{e(str(manifest.get('flow_name','flow')))}</b> "
            f"<span class='muted'>{e(str(manifest.get('flow_id') or ''))} · "
            f"{duration/1000:.1f}s · {e(flow_status)}</span></summary>"
            f"<p class='muted'><code>{e(str(manifest.get('flow_path','')))}</code><br>"
            f"reproduce: <code>{e(str(manifest.get('reproduction','')))}</code></p>"
            "<table><tr><th>#</th><th>command</th><th></th><th>time</th>"
            f"<th>detail</th></tr>{step_rows(manifest)}</table></details>")

    failed_list = "".join(
        f"<li class='s-failed'>{e(str(m.get('flow_name','')))} — "
        f"{e(str((m.get('primary_error') or {}).get('error_code','')))}</li>"
        for m in failed) or "<li class='s-passed'>none</li>"

    return f"""<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>Flow suite — {len(passed)}/{len(manifests)} passed</title>
<style>
  body {{ font: 14px/1.5 -apple-system, system-ui, sans-serif; margin: 2rem auto;
         max-width: 64rem; padding: 0 1rem; color: #1f2328; }}
  table {{ border-collapse: collapse; width: 100%; margin: .5rem 0 1rem; }}
  td, th {{ border-bottom: 1px solid #d0d7de; padding: .35rem .5rem;
            text-align: left; vertical-align: top; }}
  code {{ background: #f6f8fa; padding: .1em .3em; border-radius: 4px; }}
  .status {{ color: {color}; font-weight: 700; }}
  .s-failed {{ color: #b42318; }} .s-passed {{ color: #1a7f37; }}
  .s-skipped {{ color: #9a6700; }}
  .muted {{ color: #57606a; font-weight: 400; }}
  summary {{ cursor: pointer; padding: .35rem 0; }}
  .totals {{ display: flex; gap: 2rem; margin: 1rem 0; }}
  .totals div {{ font-size: 1.6rem; font-weight: 700; }}
  .totals span {{ display: block; font-size: .8rem; font-weight: 400;
                  color: #57606a; }}
</style>
<h1>Flow suite <span class="status">{e(status)}</span></h1>
<div class="totals">
  <div>{len(manifests)}<span>flows</span></div>
  <div class="s-passed">{len(passed)}<span>passed</span></div>
  <div class="s-failed">{len(failed)}<span>failed</span></div>
  <div>{total_ms/1000:.0f}s<span>total step time</span></div>
</div>
<h2>Failures</h2>
<ul>{failed_list}</ul>
<h2>Flows</h2>
{''.join(blocks)}
"""


def render_suite_junit(manifests: list[dict[str, Any]]) -> str:
    """One JUnit document with a <testsuite> per flow — what CI expects."""
    suites = [render_junit(m).split("\n", 1)[1].strip() for m in manifests]
    tests = sum(len(m.get("steps", [])) for m in manifests)
    failures = sum(1 for m in manifests for s in m.get("steps", [])
                   if s.get("status") == "failed")
    total = sum(s.get("duration_ms", 0)
                for m in manifests for s in m.get("steps", [])) / 1000
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<testsuites tests="{tests}" failures="{failures}" '
            f'time="{total:.3f}">' + "".join(suites) + "</testsuites>\n")


def render_junit(manifest: dict[str, Any]) -> str:
    suite_name = manifest.get("flow_id") or manifest.get("flow_name") or "flow"
    steps = manifest.get("steps", [])
    failures = sum(1 for s in steps if s.get("status") == "failed")
    skipped = sum(1 for s in steps if s.get("status") == "skipped")
    total_time = sum(s.get("duration_ms", 0) for s in steps) / 1000
    cases = []
    for step in steps:
        name = f"{step.get('index', 0):03d} {step.get('command', '')}"
        if step.get("label"):
            name += f" — {step['label']}"
        time_s = step.get("duration_ms", 0) / 1000
        body = ""
        if step.get("status") == "failed":
            message = quoteattr(str(step.get("error", "")))
            code = xml_escape(str(step.get("error_code", "")))
            body = (f'<failure message={message} type="{code}">'
                    f'{xml_escape(str(step.get("failure_class", "")))}</failure>')
        elif step.get("status") == "skipped":
            body = (f'<skipped message='
                    f'{quoteattr(str(step.get("skip_reason", "")))}/>')
        cases.append(
            f'<testcase classname={quoteattr(str(suite_name))} '
            f'name={quoteattr(name)} time="{time_s:.3f}">{body}</testcase>')
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<testsuite name={quoteattr(str(suite_name))} tests="{len(steps)}" '
        f'failures="{failures}" skipped="{skipped}" errors="0" '
        f'time="{total_time:.3f}">'
        + "".join(cases) + "</testsuite>\n"
    )
