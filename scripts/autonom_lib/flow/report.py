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
import os
import re
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
    return render_run_page(
        manifest, _inline_step_assets(manifest, artifacts_dir), standalone=True)


_LEGACY_FRAME_RE = re.compile(
    r"^\d{4}_\d{6}_(?:failure-)?step-(\d+)(?:-(?:before|after))?\.(?:png|json|txt)$")
_LEGACY_SIDECAR_RE = re.compile(
    r"^(?:failure-)?step-(\d+)-(?:hierarchy\.json|logs\.txt)$")


def _legacy_step_index(name: str) -> int | None:
    """Step number for a v1 manifest, from the harness's own file naming only.

    Anchored on purpose: an arbitrary `takeScreenshot` label must never be
    read as a step number (that bug is exactly why the ledger exists).
    """
    for pattern in (_LEGACY_FRAME_RE, _LEGACY_SIDECAR_RE):
        match = pattern.match(name)
        if match:
            return int(match.group(1))
    return None


def _artifact_phase(entry: dict[str, Any] | None, name: str) -> str:
    kind = str((entry or {}).get("kind") or "")
    for phase in ("before", "after", "failure"):
        if phase in kind or f"-{phase}-" in name or name.startswith(f"{phase}-"):
            return phase
    return "captured"


def _looks_like_step_evidence(path: Path) -> bool:
    return (path.suffix == ".png" or path.name.endswith("hierarchy.json")
            or path.name.endswith("logs.txt"))


def _load_hierarchy(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    nodes = value.get("nodes") if isinstance(value, dict) else value
    return nodes if isinstance(nodes, list) else []


def _node_key(node: dict[str, Any], occurrence: int) -> str:
    identity = (node.get("resource_id") or node.get("desc") or
                node.get("text") or node.get("ref") or "node")
    return f"{identity}|{node.get('role') or node.get('class') or ''}|{occurrence}"


def _indexed_nodes(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    seen: dict[str, int] = {}
    indexed: dict[str, dict[str, Any]] = {}
    for node in nodes:
        base = str(node.get("resource_id") or node.get("desc") or
                   node.get("text") or node.get("ref") or "node")
        seen[base] = seen.get(base, 0) + 1
        indexed[_node_key(node, seen[base])] = node
    return indexed


def _hierarchy_diff(before: list[dict[str, Any]],
                    after: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not before or not after:
        return None
    left = _indexed_nodes(before)
    right = _indexed_nodes(after)
    added = [right[key] for key in sorted(right.keys() - left.keys())]
    removed = [left[key] for key in sorted(left.keys() - right.keys())]
    fields = ("text", "desc", "resource_id", "role", "bounds", "enabled",
              "focused", "selected", "checked")
    changed = []
    for key in sorted(left.keys() & right.keys()):
        changes = {field: {"before": left[key].get(field),
                           "after": right[key].get(field)}
                   for field in fields if left[key].get(field) != right[key].get(field)}
        if changes:
            changed.append({"node": key, "changes": changes})
    return {
        "added": added[:25], "removed": removed[:25], "changed": changed[:25],
        "added_count": len(added), "removed_count": len(removed),
        "changed_count": len(changed),
        "truncated": any(len(items) > 25 for items in (added, removed, changed)),
    }


def _evidence_slot(by_step: dict[int, dict[str, Any]], index: int) -> dict[str, Any]:
    slot = by_step.setdefault(index, {})
    slot.setdefault("shots", [])
    slot.setdefault("hierarchies", {})
    slot.setdefault("logs", {})
    return slot


def _finalize_evidence(by_step: dict[int, dict[str, Any]]) -> dict[int, dict[str, Any]]:
    for slot in by_step.values():
        before = (slot.get("hierarchies") or {}).get("before", {}).get("nodes", [])
        after = (slot.get("hierarchies") or {}).get("after", {}).get("nodes", [])
        diff = _hierarchy_diff(before, after)
        if diff:
            slot["hierarchy_diff"] = diff
    return by_step


def _inline_step_assets(manifest: dict[str, Any],
                        artifacts_dir: Path) -> dict[int, dict[str, Any]]:
    """Build the same step evidence model using data URIs for one-file HTML."""
    by_step: dict[int, dict[str, Any]] = {}
    ledger = {entry.get("path"): entry
              for entry in manifest.get("artifact_steps", [])
              if entry.get("path")}
    for relative in manifest.get("artifacts", []):
        source = artifacts_dir / relative
        if not source.is_file():
            continue
        entry = ledger.get(relative)
        index = (entry or {}).get("step_index")
        if not isinstance(index, int):
            index = _legacy_step_index(source.name) if not ledger else None
        if not isinstance(index, int):
            if _looks_like_step_evidence(source):
                by_step.setdefault(0, {}).setdefault("orphans", []).append(relative)
            continue
        slot = _evidence_slot(by_step, index)
        phase = _artifact_phase(entry, source.name)
        if source.suffix == ".png":
            uri = _inline_image(source)
            slot["shots"].append({
                "src": uri, "phase": phase, "name": source.name,
                "missing": None if uri else "too large or unreadable",
            })
        elif source.name.endswith("hierarchy.json"):
            slot["hierarchies"][phase] = {
                "href": None, "nodes": _load_hierarchy(source),
                "name": source.name,
            }
        elif source.name.endswith("logs.txt"):
            slot["logs"][phase] = source.read_text(
                encoding="utf-8", errors="replace")[-12000:]
    return _finalize_evidence(by_step)


def _step_assets(manifest: dict[str, Any], artifacts_dir: Path,
                 assets: Path, mode: str,
                 file_mode: int = 0o644,
                 url_prefix: str = "assets") -> dict[int, dict[str, Any]]:
    """Copy this run's evidence next to the report and index it by step.

    The step of each artifact comes from the manifest's ``artifact_steps``
    ledger (manifest v2+), which the executor writes at capture time. Filenames are
    never parsed for a step number: a ``takeScreenshot`` label is arbitrary
    user text and could impersonate one.

    ``mode``: ``all`` copies every screenshot, ``failed`` only the frames of
    a run that failed, ``none`` copies nothing. Files are copied rather than
    inlined — a suite's worth of base64 would be hundreds of megabytes.
    """
    run_id = str(manifest.get("run_id") or "run")
    by_step: dict[int, dict[str, Any]] = {}
    target = assets / run_id
    ledger = {entry.get("path"): entry
              for entry in manifest.get("artifact_steps", [])
              if entry.get("path")}
    for relative in manifest.get("artifacts", []):
        source = artifacts_dir / relative
        if not source.is_file():
            continue
        entry = ledger.get(relative)
        if entry is None:
            # v1 manifest (no ledger): fall back to the harness's own naming,
            # which is machine-generated for auto-captured frames. A label the
            # user chose is never trusted as a step number — those frames are
            # listed under "unattached evidence" instead of guessed at.
            index = _legacy_step_index(source.name) if not ledger else None
            if index is None:
                if _looks_like_step_evidence(source):
                    by_step.setdefault(0, {}).setdefault("orphans", []).append(relative)
                continue
        else:
            index = entry.get("step_index")
        if not isinstance(index, int):
            continue
        name = source.name
        slot = _evidence_slot(by_step, index)
        phase = _artifact_phase(entry, name)
        if source.suffix == ".png":
            if mode == "none" or (mode == "failed"
                                  and manifest.get("status") == "passed"):
                continue
            target.mkdir(parents=True, exist_ok=True)
            copy = target / name
            copy.write_bytes(source.read_bytes())
            os.chmod(copy, file_mode)
            slot["shots"].append({
                "src": f"{url_prefix}/{run_id}/{name}",
                "phase": phase, "name": name,
            })
        elif name.endswith("logs.txt"):
            text = source.read_text(encoding="utf-8", errors="replace")
            slot["logs"][phase] = text[-12000:]
        elif name.endswith("hierarchy.json"):
            target.mkdir(parents=True, exist_ok=True)
            copy = target / name
            copy.write_bytes(source.read_bytes())
            os.chmod(copy, file_mode)
            slot["hierarchies"][phase] = {
                "href": f"{url_prefix}/{run_id}/{name}",
                "nodes": _load_hierarchy(source), "name": name,
            }
    return _finalize_evidence(by_step)


def _target_overlay(step: dict[str, Any]) -> str:
    target = step.get("target") or {}
    bounds = target.get("bounds")
    viewport = target.get("viewport")
    if (not isinstance(bounds, list) or len(bounds) != 4
            or not isinstance(viewport, list) or len(viewport) != 2
            or not all(isinstance(value, (int, float)) for value in bounds + viewport)
            or viewport[0] <= 0 or viewport[1] <= 0):
        return ""
    left, top, right, bottom = bounds
    width, height = viewport
    style = (f"left:{left / width * 100:.4f}%;top:{top / height * 100:.4f}%;"
             f"width:{max(0, right-left) / width * 100:.4f}%;"
             f"height:{max(0, bottom-top) / height * 100:.4f}%")
    return f"<span class='target-box' style='{style}' aria-label='matched target'></span>"


def _render_shots(step: dict[str, Any], found: dict[str, Any]) -> str:
    e = html.escape
    figures = []
    overlay = _target_overlay(step)
    for shot in found.get("shots", []):
        phase = str(shot.get("phase") or "captured")
        src = shot.get("src")
        if not src:
            figures.append(
                f"<figure class='missing'><figcaption>{e(str(shot.get('name','frame')))} "
                f"— {e(str(shot.get('missing','unreadable')))}</figcaption></figure>")
            continue
        figures.append(
            f"<figure><div class='frame'><img src='{e(str(src))}' loading='lazy' "
            f"alt='{e(phase)} frame for step {step.get('index','')}'>"
            f"{overlay if phase in ('after', 'failure', 'captured') else ''}</div>"
            f"<figcaption><b>{e(phase)}</b> · {e(str(shot.get('name','frame')))}</figcaption>"
            "</figure>")
    return ("<div class='shots'>" + "".join(figures) + "</div>"
            if figures else "<p class='missing'>No screenshots were captured for this step.</p>")


def _render_hierarchy(found: dict[str, Any]) -> str:
    e = html.escape
    hierarchies = found.get("hierarchies") or {}
    links = []
    for phase, item in hierarchies.items():
        if item.get("href"):
            links.append(f"<a href='{e(str(item['href']))}'>{e(phase)} hierarchy JSON</a>")
    diff = found.get("hierarchy_diff")
    if diff:
        summary = (f"{diff['added_count']} added · {diff['removed_count']} removed · "
                   f"{diff['changed_count']} changed")
        body = e(json.dumps({key: diff[key] for key in ("added", "removed", "changed")},
                            ensure_ascii=False, indent=2))
        result = (f"<p><b>{e(summary)}</b></p><pre>{body}</pre>"
                  + ("<p class='missing'>Diff preview is truncated to 25 entries per group.</p>"
                     if diff.get("truncated") else ""))
    elif hierarchies:
        phases = ", ".join(str(name) for name in hierarchies)
        result = f"<p class='missing'>Captured {e(phases)} hierarchy; a before/after pair is required for a diff.</p>"
    else:
        result = "<p class='missing'>No hierarchy evidence was captured for this step.</p>"
    if links:
        result += "<p>" + " · ".join(links) + "</p>"
    return result


def _requests_for_step(manifest: dict[str, Any], index: int) -> list[dict[str, Any]]:
    for item in (manifest.get("network") or {}).get("requests_by_step", []):
        if item.get("step_index") == index:
            return item.get("requests") or []
    return []


def _render_requests(manifest: dict[str, Any], index: int) -> str:
    e = html.escape
    network = manifest.get("network") or {}
    if not network.get("available"):
        reason = network.get("reason") or "network capture was not attached"
        return (f"<p class='missing'>{e(str(reason))}; request evidence is "
                "unavailable, not empty.</p>")
    requests = _requests_for_step(manifest, index)
    if not requests:
        return "<p class='missing'>No requests started during this step.</p>"
    rows = []
    for request in requests:
        mocked = " · mocked" if request.get("mocked") else ""
        previews = {
            "request_headers": request.get("request_headers_preview") or {},
            "request_body": request.get("request_body_preview"),
            "response_headers": request.get("response_headers_preview") or {},
            "response_body": request.get("response_body_preview"),
        }
        rows.append(
            "<article class='request'>"
            f"<p><b>{e(str(request.get('method','')))}</b> "
            f"<code>{e(str(request.get('url','')))}</code><br>"
            f"status <b>{e(str(request.get('status','')))}</b> · "
            f"{e(str(request.get('duration_ms',0)))} ms{e(mocked)}</p>"
            f"<pre>{e(json.dumps(previews, ensure_ascii=False, indent=2))}</pre>"
            "</article>")
    return "".join(rows)


def render_run_page(manifest: dict[str, Any], evidence: dict[int, dict],
                    base: Path | None = None, *, standalone: bool = False,
                    control_url: str | None = None,
                    csrf_token: str | None = None) -> str:
    """Interactive evidence page with addressable steps and safe replay forms."""
    e = html.escape
    status = manifest.get("status", "unknown")
    color = {"passed": "#1a7f37", "failed": "#b42318",
             "replayed": "#9a6700"}.get(status, "#555")
    nav = []
    blocks = []
    reproduction = _shorten(str(manifest.get("reproduction", "")), base)
    steps = sorted(manifest.get("steps", []),
                   key=lambda item: item.get("index", 0))
    first_causal_failure = next(
        (step for step in steps if step.get("status") == "failed"
         and step.get("failure_class") == "test_failure"), None)
    for step in steps:
        index = step.get("index", 0)
        found = evidence.get(index, {})
        step_status = str(step.get("status", ""))
        badge = {"passed": "✓", "failed": "✗", "skipped": "○"}.get(
            step_status, "·")
        label = str(step.get("label")
                    or (step.get("canonical_args") or {}).get("name")
                    or step.get("command") or "step")
        nav.append(
            f"<a class='nav-step s-{e(step_status)}' href='#step-{index}' "
            f"data-search='{e((label + ' ' + str(step.get('command',''))).lower())}'>"
            f"<span>{badge}</span><b>{index}</b> {e(label)}</a>")
        metadata = {
            "step_id": step.get("step_id"), "source_id": step.get("source_id"),
            "source": {"file": _shorten(str(step.get("flow", "")), base),
                       "line": step.get("line"), "column": step.get("column")},
            "selector": step.get("selector"), "target": step.get("target"),
            "arguments": step.get("canonical_args"),
            "precondition": step.get("precondition_fingerprint"),
            "postcondition": step.get("postcondition_fingerprint"),
        }
        metadata = {key: value for key, value in metadata.items()
                    if value not in (None, {}, "")}
        messages = []
        if step.get("skip_reason"):
            messages.append(f"<p class='notice s-skipped'>Skipped: "
                            f"{e(str(step['skip_reason']))}</p>")
        if step.get("error"):
            messages.append(
                f"<p class='notice s-failed'><b>{e(str(step.get('error_code','')))}</b> "
                f"({e(str(step.get('failure_class','')))})<br>{e(str(step['error']))}</p>")
        replay_command = f"{reproduction} --until-step {index} --evidence always"
        for kind in ("screenshot", "hierarchy", "logs", "network"):
            replay_command += f" --collect {kind}"
        rendered_logs = "".join(
            f"<h4>{e(phase)}</h4><pre>{e(text)}</pre>"
            for phase, text in (found.get("logs") or {}).items()
        ) or "<p class='missing'>No per-step logs were captured.</p>"
        if control_url and csrf_token:
            action = (
                "<div class='replay'><b>Reconstruct this state</b>"
                "<p>This explicitly re-executes every mutation in the flow prefix.</p>"
                f"<form method='post' action='{e(control_url)}'>"
                f"<input type='hidden' name='token' value='{e(csrf_token)}'>"
                f"<input type='hidden' name='run_id' value='{e(str(manifest.get('run_id','')))}'>"
                f"<input type='hidden' name='step_index' value='{index}'>"
                "<button type='submit'>Replay to this step</button></form></div>")
        else:
            action = ("<p class='replay'><b>Replay to this state</b><br>"
                      f"<code>{e(replay_command)}</code></p>")
        blocks.append(
            f"<section class='step-card' id='step-{index}' tabindex='-1' "
            f"data-step-id='{e(str(step.get('step_id') or ''))}'>"
            f"<a class='stable-anchor' id='{e(str(step.get('step_id') or ''))}'></a>"
            f"<header><h2 class='s-{e(step_status)}'>{badge} Step {index} · "
            f"<code>{e(str(step.get('command','')))}</code></h2>"
            f"<span>{step.get('duration_ms',0)} ms · {step.get('attempts',1)} attempt(s)</span></header>"
            + "".join(messages)
            + f"<details><summary>Step record</summary><pre>"
              f"{e(json.dumps(metadata, ensure_ascii=False, indent=2))}</pre></details>"
            + f"<details open><summary>Screenshots and matched target</summary>"
              f"{_render_shots(step, found)}</details>"
            + f"<details><summary>UI hierarchy diff</summary>"
              f"{_render_hierarchy(found)}</details>"
            + f"<details><summary>Device logs</summary>"
              f"{rendered_logs}</details>"
            + f"<details><summary>Network requests</summary>"
              f"{_render_requests(manifest, int(index))}</details>"
            + action + "</section>")

    stray = evidence.get(0, {}).get("orphans") or []
    orphans = ("<section class='step-card'><h2>Unattached evidence</h2><ul>"
               + "".join(f"<li><code>{e(str(name))}</code></li>" for name in stray)
               + "</ul></section>") if stray else ""
    sensitive = ("<p class='sensitive'>⚠ This run used secrets or sensitive "
                 "input; frames may show private data. Share deliberately.</p>"
                 if manifest.get("sensitive") else "")
    failure = manifest.get("primary_error") or {}
    failure_summary = (f"<p class='notice s-failed'><b>Primary failure: "
                       f"{e(str(failure.get('error_code','')))}</b><br>"
                       f"{e(str(failure.get('error','')))}</p>" if failure else "")
    hook_failures = manifest.get("hook_failures") or []
    hook_summary = ("<details><summary>Cleanup failures</summary><pre>"
                    + e(json.dumps(hook_failures, ensure_ascii=False, indent=2))
                    + "</pre></details>" if hook_failures else "")
    environment = {
        "identity": {
            "attempt_id": manifest.get("attempt_id"),
            "execution_status": manifest.get("execution_status") or manifest.get("status"),
            "proof_verdict": manifest.get("proof_verdict"),
        },
        "environment": manifest.get("environment") or {},
        "capability_snapshot": manifest.get("capability_snapshot"),
        "setup_catalog": manifest.get("setup") or {},
        "side_effects": manifest.get("side_effects") or [],
        "evidence_mode": manifest.get("evidence_mode"),
        "evidence_collect": manifest.get("evidence_collect") or [],
        "started_at_ms": manifest.get("started_at_ms"),
        "finished_at_ms": manifest.get("finished_at_ms"),
    }
    back = "" if standalone else '<p><a href="index.html">← all flows</a></p>'
    restore_note = (
        "<p class='restore-note'><b>Portable restore:</b> Autonom reconstructs "
        "the selected state by replaying the flow from its start and stops after "
        "that step. Cleanup hooks are skipped. This is not presented as a native "
        "app snapshot.</p>")
    causal = (
        f"<p class='notice s-failed'><b>First causal failure:</b> "
        f"<a href='#step-{first_causal_failure.get('index')}'>step "
        f"{first_causal_failure.get('index')}</a> · "
        f"{e(str(first_causal_failure.get('error_code') or 'failure'))}</p>"
        if first_causal_failure else "")
    return f"""<!doctype html><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy"
      content="default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(str(manifest.get('flow_name','flow')))} — {e(status)}</title>
<style>
  :root {{ color-scheme: light; --line:#d0d7de; --muted:#57606a; --panel:#f6f8fa; }}
  * {{ box-sizing: border-box; }} body {{ margin:0; color:#1f2328;
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }}
  .page {{ max-width:1180px; margin:auto; padding:28px 24px; }}
  .layout {{ display:grid; grid-template-columns:230px minmax(0,1fr); gap:24px; align-items:start; }}
  aside {{ position:sticky; top:16px; max-height:calc(100vh - 32px); overflow:auto;
    border:1px solid var(--line); border-radius:10px; padding:10px; }}
  .nav-filter {{ width:100%; margin:8px 0; padding:7px; border:1px solid var(--line);
    border-radius:6px; }} .shortcuts {{ font-size:11px; color:var(--muted); margin:8px 4px 2px; }}
  .nav-step {{ display:grid; grid-template-columns:18px 24px 1fr; gap:5px; padding:7px;
    color:#1f2328; text-decoration:none; border-radius:6px; }} .nav-step:hover {{ background:var(--panel); }}
  .nav-step:focus-visible,button:focus-visible,input:focus-visible {{ outline:3px solid #0969da; outline-offset:2px; }}
  code {{ background:var(--panel); padding:.1em .3em; border-radius:4px; overflow-wrap:anywhere; }}
  pre {{ background:var(--panel); border-radius:7px; padding:12px; overflow:auto;
    max-height:360px; font-size:12px; white-space:pre-wrap; }}
  .step-card {{ border:1px solid var(--line); border-radius:12px; padding:18px; margin:0 0 18px;
    scroll-margin-top:18px; }} .step-card>header {{ display:flex; justify-content:space-between;
    gap:16px; align-items:baseline; border-bottom:1px solid var(--line); margin:-4px 0 10px; }}
  h1 {{ margin:.2em 0; }} h2 {{ font-size:17px; }} details {{ border-top:1px solid #eaeef2;
    padding:10px 0; }} summary {{ cursor:pointer; font-weight:650; }}
  .shots {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; margin-top:10px; }}
  figure {{ margin:0; }} .frame {{ display:inline-block; position:relative; max-width:100%; }}
  img {{ display:block; width:auto; max-width:100%; max-height:540px; border:1px solid var(--line); border-radius:8px; }}
  figcaption {{ color:var(--muted); font-size:12px; padding-top:4px; }}
  .target-box {{ position:absolute; border:3px solid #ff2d55; background:#ff2d5526;
    box-shadow:0 0 0 1px #fff; pointer-events:none; }}
  .status {{ color:{color}; }} .s-failed {{ color:#b42318; }} .s-passed {{ color:#1a7f37; }}
  .s-skipped,.s-replayed {{ color:#9a6700; }} .muted,.missing {{ color:var(--muted); }}
  .notice,.sensitive,.restore-note,.replay {{ padding:10px 12px; border-radius:8px; background:var(--panel); }}
  .sensitive {{ background:#fff8c5; }} .restore-note {{ background:#ddf4ff; }}
  .request {{ border-left:3px solid #54aeff; padding-left:12px; }} button {{ background:#1f883d;
    color:white; border:0; border-radius:6px; padding:8px 12px; font-weight:650; cursor:pointer; }}
  .stable-anchor {{ position:relative; top:-12px; }}
  @media(max-width:760px) {{ .layout {{ grid-template-columns:1fr; }} aside {{ position:relative;
    top:auto; max-height:none; }} }}
</style>
<div class="page">{back}
<h1>{e(str(manifest.get('flow_name','flow')))} <span class="status">{e(status)}</span></h1>
{sensitive}
{failure_summary}{causal}{hook_summary}
<p class="muted"><code>{e(_shorten(str(manifest.get('flow_path','')), base))}</code><br>
{e(str(manifest.get('platform','')))} {e(str(manifest.get('target_id','')))} ·
app <code>{e(str(manifest.get('app_id','')))}</code> · run <code>{e(str(manifest.get('run_id','')))}</code> ·
execution <b>{e(str(manifest.get('execution_status') or status))}</b> ·
proof <b>{e(str(manifest.get('proof_verdict') or 'not evaluated'))}</b></p>
{restore_note}
<details><summary>Setup, capabilities, environment, and evidence policy</summary><pre>{e(json.dumps(environment, ensure_ascii=False, indent=2))}</pre></details>
<div class="layout"><aside aria-label="Step navigator"><b>Timeline</b>
<input class="nav-filter" id="nav-filter" aria-label="Filter steps" placeholder="Filter steps (/)" />
{''.join(nav)}<p class="shortcuts">Keyboard: j/k next/previous · / filter · Enter open</p></aside>
<main>{''.join(blocks)}{orphans}</main></div></div>
<script type="module">
(()=>{{const links=[...document.querySelectorAll('.nav-step')],filter=document.getElementById('nav-filter');
let current=Math.max(0,links.findIndex(link=>link.hash===location.hash));
function visible(){{return links.filter(link=>link.hidden===false)}}
function go(delta){{const choices=visible();if(!choices.length)return;const active=choices.indexOf(document.activeElement);const index=(active>=0?active:Math.max(0,choices.findIndex(link=>link.hash===location.hash)));const next=choices[(index+delta+choices.length)%choices.length];next.focus();next.click()}}
filter.addEventListener('input',()=>{{const query=filter.value.trim().toLowerCase();for(const link of links)link.hidden=query&&!link.dataset.search.includes(query)}});
document.addEventListener('keydown',event=>{{if(event.target===filter){{if(event.key==='Escape'){{filter.value='';filter.dispatchEvent(new Event('input'));filter.blur()}}return}}if(event.key==='/'){{event.preventDefault();filter.focus()}}else if(event.key==='j'){{event.preventDefault();go(1)}}else if(event.key==='k'){{event.preventDefault();go(-1)}}}});
}})();
</script>
"""


def _shorten(text: str, base: Path | None) -> str:
    """Drop a leading base directory so a shared report carries no local paths."""
    if not base:
        return text
    prefix = str(base).rstrip("/") + "/"
    return text.replace(prefix, "")


def render_suite_html(manifests: list[dict[str, Any]],
                      base: Path | None = None,
                      pages: dict[str, str] | None = None) -> str:
    """One page for a whole suite run: totals, then every flow with its steps.

    Same containment rules as the single-run report (no external fetch,
    everything escaped). Screenshots stay in the per-run reports — a suite
    page inlining 46 runs' images would be hundreds of megabytes; each row
    links to its own report instead.

    ``base`` strips that directory prefix from flow paths and reproduction
    commands, so a report committed to a repository carries repo-relative
    paths instead of one machine's home directory.
    """
    e = html.escape
    passed = [m for m in manifests if m.get("status") == "passed"]
    failed = [m for m in manifests if m.get("status") == "failed"]
    replayed = [m for m in manifests if m.get("status") == "replayed"]
    total_ms = sum(step.get("duration_ms", 0)
                   for m in manifests for step in m.get("steps", []))
    status = "failed" if failed else "passed"
    color = "#b42318" if failed else "#1a7f37"

    def step_rows(manifest: dict[str, Any]) -> str:
        rows = []
        for step in sorted(manifest.get("steps", []),
                           key=lambda item: item.get("index", 0)):
            badge = {"passed": "✓", "failed": "✗", "skipped": "○"}.get(
                step.get("status", ""), "·")
            detail = ""
            if step.get("label"):
                detail = e(str(step["label"]))
            elif step.get("selector"):
                detail = f"<code>{e(json.dumps(step['selector'], ensure_ascii=False))}</code>"
            status = step.get("status")
            if status == "failed" and step.get("error"):
                detail += (f"<br><b>{e(str(step.get('error_code','')))}</b> "
                           f"{e(str(step['error']))}")
            if step.get("skip_reason"):
                # an optional step keeps the error it tolerated — showing it in
                # failure red would call a deliberate skip a defect
                tolerated = (f" (tolerated {e(str(step.get('error_code','')))})"
                             if step.get("error") else "")
                detail += (f"<br><i class='muted'>skipped: "
                           f"{e(str(step['skip_reason']))}{tolerated}</i>")
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
        page = pages.get(str(manifest.get("run_id"))) if pages else None
        link = (f" <a href='{e(page)}'>full report →</a>" if page else "")
        blocks.append(
            f"<details{open_attr}><summary class='s-{e(flow_status)}'>"
            f"<b>{e(str(manifest.get('flow_name','flow')))}</b> "
            f"<span class='muted'>{e(str(manifest.get('flow_id') or ''))} · "
            f"{duration/1000:.1f}s · {e(flow_status)}</span></summary>"
            f"<p>{link}</p>"
            f"<p class='muted'><code>"
            f"{e(_shorten(str(manifest.get('flow_path','')), base))}</code><br>"
            f"reproduce: <code>"
            f"{e(_shorten(str(manifest.get('reproduction','')), base))}</code></p>"
            "<table><tr><th>#</th><th>command</th><th></th><th>time</th>"
            f"<th>detail</th></tr>{step_rows(manifest)}</table></details>")

    sensitive_note = (
        "<p class='sensitive'>⚠ Some runs used secrets or sensitive input; "
        "their frames may show private data. Share deliberately.</p>"
        if any(m.get("sensitive") for m in manifests) else "")
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
  .sensitive {{ background: #fff8c5; padding: .5rem .8rem; border-radius: 6px; }}
</style>
<h1>Flow suite <span class="status">{e(status)}</span></h1>
<div class="totals">
  <div>{len(manifests)}<span>flows</span></div>
  <div class="s-passed">{len(passed)}<span>passed</span></div>
  <div class="s-failed">{len(failed)}<span>failed</span></div>
  <div class="s-skipped">{len(replayed)}<span>prefix replays</span></div>
  <div>{total_ms/1000:.0f}s<span>total step time</span></div>
</div>
{sensitive_note}
<h2>Failures</h2>
<ul>{failed_list}</ul>
<h2>Flows</h2>
{''.join(blocks)}
"""


def recovered_retry_indexes(manifest: dict[str, Any]) -> set[int]:
    """Step indexes that failed inside a retry and were later superseded.

    The executor keeps every attempt in ``steps`` on purpose (the timeline
    is the history). JUnit must not treat those recovered attempts as
    final ``<failure>`` cases — a passed retry is a passed test.
    """
    recovered: set[int] = set()
    blocks = manifest.get("blocks") or []
    for block in blocks:
        if block.get("command") != "retry":
            continue
        attempts = block.get("attempts_detail") or []
        if not attempts:
            continue
        superseded = (attempts if block.get("status") == "passed"
                      else attempts[:-1])
        for attempt in superseded:
            if attempt.get("status") != "failed":
                continue
            first = attempt.get("first_index")
            last = attempt.get("last_index")
            if isinstance(first, int) and isinstance(last, int) and last >= first:
                recovered.update(range(first, last + 1))
    if recovered:
        return recovered
    # v1 manifests / no ledger: a passed run's failed steps that carry
    # retry_attempt are recovered history, not the outcome.
    if manifest.get("status") == "passed":
        for step in manifest.get("steps") or []:
            if (step.get("status") == "failed"
                    and step.get("retry_attempt") is not None
                    and not step.get("hook")):
                index = step.get("index")
                if isinstance(index, int):
                    recovered.add(index)
    return recovered


def _is_junit_failure(step: dict[str, Any], recovered: set[int]) -> bool:
    return (step.get("status") == "failed"
            and step.get("index") not in recovered)


def render_suite_junit(manifests: list[dict[str, Any]]) -> str:
    """One JUnit document with a <testsuite> per flow — what CI expects."""
    suites = [render_junit(m).split("\n", 1)[1].strip() for m in manifests]
    tests = sum(len(m.get("steps", [])) for m in manifests)
    failures = sum(
        1 for m in manifests
        for s in m.get("steps", [])
        if (m.get("status") != "replayed"
            and _is_junit_failure(s, recovered_retry_indexes(m))))
    total = sum(s.get("duration_ms", 0)
                for m in manifests for s in m.get("steps", [])) / 1000
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<testsuites tests="{tests}" failures="{failures}" '
            f'time="{total:.3f}">' + "".join(suites) + "</testsuites>\n")


def render_junit(manifest: dict[str, Any]) -> str:
    suite_name = manifest.get("flow_id") or manifest.get("flow_name") or "flow"
    steps = manifest.get("steps", [])
    prefix_replay = manifest.get("status") == "replayed"
    recovered = recovered_retry_indexes(manifest)
    failures = (0 if prefix_replay else
                sum(1 for s in steps if _is_junit_failure(s, recovered)))
    skipped = (len(steps) if prefix_replay else
               sum(1 for s in steps
                   if s.get("status") == "skipped"
                   or (s.get("status") == "failed"
                       and s.get("index") in recovered)))
    total_time = sum(s.get("duration_ms", 0) for s in steps) / 1000
    cases = []
    for step in steps:
        name = f"{step.get('index', 0):03d} {step.get('command', '')}"
        if step.get("label"):
            name += f" — {step['label']}"
        time_s = step.get("duration_ms", 0) / 1000
        body = ""
        if prefix_replay:
            body = '<skipped message="prefix replay"/>'
        elif step.get("status") == "failed" and step.get("index") in recovered:
            body = '<skipped message="retried"/>'
        elif step.get("status") == "failed":
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
