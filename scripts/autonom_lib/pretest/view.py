"""Offline HTML view of a pre-test pack (tp-019): the pack's ``index.html``.

``render(summary, review=None)`` turns a ``summary.json`` dict (schema
``autonom.pretest-summary/v1``) and an optional review status into one
self-contained HTML document a QA engineer opens straight from the pack:

- the ticket verdict, the review state and the stats strip on top;
- findings (confirmed first as cards, automatic signals as quiet rows), checks
  with verdicts, evidence and repro steps, device health, changed files;
- a rail with the review choices and their copyable commands, the edge-case
  kit coverage and the timing.

Rules the document keeps (tp-019, design brief):

- Offline: inline CSS, one small inline script, no font, image or script
  from anywhere else. Screenshots are ``<img>`` tags whose ``src`` is a
  pack-relative path (``blobs/sha256/…``); any other path is shown as text,
  never linked. No ``scheme://`` sequence appears in the output: in text it is
  written ``:&#47;&#47;`` (renders the same), in the embedded JSON ``:\\/\\/``.
- Untrusted data: every string goes through ``html.escape`` (text and
  attributes); class names and ids come from fixed tables or a strict
  pattern, never from data. The summary is embedded as
  ``<script type="application/json" id="pretest-data">`` with ``<``, ``>`` and
  ``&`` written as ``\\u003c``, ``\\u003e`` and ``\\u0026``.
- The inline script only toggles the check filters, fills Copy buttons,
  opens every ``details`` before printing and, when the page is opened from a
  ``file:`` URL, writes the pack's real directory (``location.pathname``
  decoded, minus ``/index.html``, single-quoted for a POSIX shell) into the
  review commands in place of ``<pack>``; it reads no other data and
  evaluates nothing. With scripts off every fact is still visible and the
  commands keep the ``<pack>`` placeholder.
- A screenshot taken while mocks were active carries a neutral "Mocked" tag
  on its caption; each check lists the requests captured in its window
  (method, host, path, status, mocked, duration); the verdict reason names
  the check's title ("chk-9 failed: <title>").
- Deterministic: the same input gives byte-identical output (no clock, no
  random ids).

``review`` is the dict ``review.status(pack)`` returns. The view reads, all
optional: ``decision``/``author``/``at`` (or ``created_at``/``ts``)/``note``
of the latest decision (top level or under ``latest``), ``decisions`` (every
decision, oldest first) and ``confirmed``/``dismissed`` (automatic finding
ids — a list of ids, a list of ``{"finding_id", "author"}`` or a mapping of
id to author).

Malformed parts (a check that is not an object, a missing field) are skipped
or shown as missing; only a summary that is not an object, a summary with
another schema, a review that is not an object, or a summary that cannot be
written as JSON is refused with ``report_model_invalid``.
"""
from __future__ import annotations

import html
import json
import re
from typing import Any, Iterable

from .. import errors

SUMMARY_SCHEMA = "autonom.pretest-summary/v1"

# verdict -> (css status class, glyph, word)
_VERDICTS: dict[str, tuple[str, str, str]] = {
    "pass": ("pass", "\u2713", "Pass"),
    "fail": ("fail", "\u2715", "Fail"),
    "blocked": ("blocked", "\u25a0", "Blocked"),
    "not_covered": ("nc", "\u25cb", "Not covered"),
    "inconclusive": ("inc", "?", "Inconclusive"),
}
# Worst first: the order a kit chip picks its dot color from several verdicts.
_VERDICT_RANK = ("fail", "blocked", "inconclusive", "not_covered", "pass")
_SEVERITIES = {"blocker": "Blocker", "major": "Major", "minor": "Minor", "info": "Info"}
_SEVERITY_RANK = ("blocker", "major", "minor", "info")
_SOURCES = {"crash": "Crash", "log": "Log", "request": "Request", "metrics": "Metrics",
            "a11y": "Accessibility", "analytics": "Analytics"}
_DECISIONS = {"approve": "Approved", "more-checks": "More checks requested",
              "return-to-dev": "Returned to dev"}
_EVIDENCE_KINDS = {"screenshot": "Screenshot", "tree": "UI tree", "action": "Action",
                   "request": "Request", "log": "Log", "run": "Flow run", "crash": "Crash",
                   "recording": "Recording"}
_PLATFORMS = {"android": "Android", "ios": "iOS"}
_FILE_STATUS = ("M", "A", "D", "R")

_CHECK_ANCHOR = re.compile(r"^chk-[0-9]{1,9}$")
_CHECK_REASON = re.compile(r"^check (chk-[0-9]{1,9}) (.+)$")
_SAFE_PATH = re.compile(r"^(?:blobs|runs)/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*/?$")
_ISO_MINUTE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})")


def _refuse(message: str, **extra: Any) -> errors.AutonomError:
    return errors.AutonomError(
        errors.REPORT_MODEL_INVALID, message,
        hint="Render the dict summary.build_summary returns (schema "
             f"{SUMMARY_SCHEMA}) and the dict review.status returns, or no review.",
        **extra)


# --- small value helpers ------------------------------------------------------------


def _e(value: Any) -> str:
    """Escape for text and double-quoted attributes; break any ``scheme://``."""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    return html.escape(text, quote=True).replace("://", ":&#47;&#47;")


def _str(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, (int, float)):
        return _fmt_num(value)
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _fmt_num(value: float) -> str:
    if isinstance(value, int):
        return str(value)
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _signed(value: Any) -> str:
    n = _num(value)
    if n is None:
        return _str(value) or "?"
    text = _fmt_num(n)
    return text if n < 0 else "+" + text


def _dicts(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _strs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (_str(item) for item in value) if text]


def _dur(value: Any) -> str | None:
    n = _num(value)
    if n is None or n < 0:
        return None
    if n == 0:
        return "0s"
    if n < 1:
        return "<1s"
    hours, rest = divmod(int(round(n)), 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


def _when(value: Any) -> str | None:
    text = _str(value)
    if not text:
        return None
    match = _ISO_MINUTE.match(text)
    if not match:
        return text
    utc = text.endswith("Z") or text.endswith("+00:00")
    return f"{match.group(1)} {match.group(2)}" + (" UTC" if utc else "")


def _time(value: Any) -> str:
    shown = _when(value)
    if not shown:
        return ""
    return f'<time datetime="{_e(value)}">{_e(shown)}</time>'


def _lookup(table: dict[str, str], value: Any) -> str | None:
    return table.get(value) if isinstance(value, str) else None


def _verdict(value: Any) -> tuple[str, str, str]:
    if isinstance(value, str) and value in _VERDICTS:
        return _VERDICTS[value]
    word = _str(value)
    return ("nc", "?", word.replace("_", " ").capitalize() if word else "Unknown")


def _check_verdict(check: dict[str, Any]) -> str:
    verdict = check.get("verdict")
    if isinstance(verdict, str) and verdict:
        return verdict
    return "not_covered" if check.get("state") in (None, "added") else "inconclusive"


def _worst(verdicts: Iterable[str]) -> str | None:
    found = [v for v in verdicts if v in _VERDICT_RANK]
    return min(found, key=_VERDICT_RANK.index) if found else None


def _safe_path(value: Any) -> str | None:
    text = _str(value)
    if not text or not _SAFE_PATH.match(text):
        return None
    if any(part in ("..", ".") for part in text.rstrip("/").split("/")):
        return None
    return text


def _pack_name(value: Any) -> str | None:
    """The pre-test id of a parent pack path, never the host path itself."""
    text = _str(value)
    if not text:
        return None
    parts = [part for part in re.split(r"[\\/]+", text) if part]
    for part in reversed(parts):
        if part.startswith("pt_"):
            return part
    return parts[-1] if parts else None


def _script_json(summary: dict[str, Any]) -> str:
    try:
        text = json.dumps(summary, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise _refuse("the summary cannot be written as JSON",
                      reason=type(exc).__name__) from None
    return (text.replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("://", ":\\/\\/"))


# --- the model the sections read -------------------------------------------------------


class _Review:
    def __init__(self, review: dict[str, Any] | None) -> None:
        review = review or {}
        decisions = _dicts(review.get("decisions"))
        latest = review.get("latest") if isinstance(review.get("latest"), dict) else None
        if latest is None and decisions:
            latest = decisions[-1]
        if latest is None and _str(review.get("decision")):
            latest = review
        self.latest = latest
        if decisions and latest is not None and decisions[-1] == latest:
            self.history = decisions[:-1]
        else:
            self.history = decisions if latest is not None else []
        self.confirmed = self._ids(review.get("confirmed"))
        self.dismissed = self._ids(review.get("dismissed"))

    @staticmethod
    def _ids(value: Any) -> dict[str, str | None]:
        found: dict[str, str | None] = {}
        if isinstance(value, dict):
            for key, who in value.items():
                author = _str(who.get("author")) if isinstance(who, dict) else _str(who)
                if _str(key):
                    found[str(key)] = author
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    key = _str(item.get("finding_id")) or _str(item.get("id"))
                    if key:
                        found[key] = _str(item.get("author"))
                elif _str(item):
                    found[str(_str(item))] = None
        return found

    @staticmethod
    def stamp(entry: dict[str, Any]) -> Any:
        for key in ("at", "created_at", "ts", "written_at"):
            if _str(entry.get(key)):
                return entry.get(key)
        return None

    @property
    def decision(self) -> str | None:
        return _str(self.latest.get("decision")) if self.latest else None

    def state_word(self) -> str:
        decision = self.decision
        if not decision:
            return "Awaiting review"
        return _DECISIONS.get(decision, decision)

    def state_html(self) -> str:
        if not self.latest or not self.decision:
            return _e("Awaiting review")
        parts = [_e(self.state_word())]
        author = _str(self.latest.get("author"))
        if author:
            parts.append("by " + _e(author))
        stamp = _time(self.stamp(self.latest))
        text = " ".join(parts)
        return text + (" \u00b7 " + stamp if stamp else "")


class _Model:
    def __init__(self, summary: dict[str, Any], review: dict[str, Any] | None) -> None:
        self.s = summary
        self.review = _Review(review)
        self.evidence = {}
        for item in _dicts(summary.get("evidence")):
            key = _str(item.get("id"))
            if key and key not in self.evidence:
                self.evidence[key] = item
        self.checks = [c for c in _dicts(summary.get("checks")) if _str(c.get("check_id"))]
        self.anchors: dict[str, str] = {}
        for index, check in enumerate(self.checks, start=1):
            cid = str(_str(check.get("check_id")))
            if cid in self.anchors:
                continue
            # Data never names an element id unless it is a plain ``chk-N``.
            self.anchors[cid] = cid if _CHECK_ANCHOR.match(cid) else f"check-row-{index}"
        self.by_check = {str(_str(c.get("check_id"))): c for c in self.checks}
        self.findings = _dicts(summary.get("findings"))

    # links -------------------------------------------------------------------------
    def check_link(self, value: Any) -> str:
        cid = _str(value)
        if not cid:
            return ""
        anchor = self.anchors.get(cid)
        if anchor:
            return f'<a href="#{anchor}">{_e(cid)}</a>'
        return _e(cid)

    # findings ---------------------------------------------------------------------
    def split_findings(self) -> tuple[list[tuple[dict, str | None]], list[dict]]:
        """(confirmed cards with an optional review confirmer, automatic rows)."""
        cards: list[tuple[dict, str | None]] = []
        rows: list[dict] = []
        for finding in self.findings:
            fid = _str(finding.get("finding_id")) or ""
            auto = finding.get("origin") == "auto"
            if auto and fid in self.review.dismissed:
                rows.append(finding)
            elif auto and fid in self.review.confirmed:
                cards.append((finding, self.review.confirmed[fid] or "review"))
            elif auto and finding.get("confirmed") is not True:
                rows.append(finding)
            else:
                cards.append((finding, None))

        def rank(item: tuple[dict, str | None]) -> int:
            severity = item[0].get("severity")
            return _SEVERITY_RANK.index(severity) if severity in _SEVERITY_RANK else 4

        cards.sort(key=rank)
        return cards, rows


# --- components --------------------------------------------------------------------


def _status_chip(verdict: Any) -> str:
    status, _glyph, word = _verdict(verdict)
    return (f'<span class="vchip s-{status}"><span class="dot" aria-hidden="true"></span>'
            f'{_e(word)}</span>')


def _evidence(model: _Model, ids: Any, check_id: Any) -> str:
    refs = _strs(ids)
    if not refs:
        return ""
    shots: list[str] = []
    others: list[str] = []
    for ref in refs:
        item = model.evidence.get(ref)
        if item is None:
            others.append(f'<li><span class="missing">{_e(ref)} (not in this pack)</span></li>')
            continue
        kind = _str(item.get("kind")) or "evidence"
        label = _str(item.get("label")) or ref
        path = _safe_path(item.get("path"))
        owner = _str(item.get("check_id")) or _str(check_id)
        if kind == "screenshot" and path:
            size = ""
            width, height = _num(item.get("width")), _num(item.get("height"))
            if width and height and width > 0 and height > 0:
                size = f' width="{int(width)}" height="{int(height)}"'
            mocked = item.get("mocked") is True
            alt = (f"Screenshot: {label}" + (f", {owner}" if owner else "")
                   + (", mocked data" if mocked else ""))
            tag = ' <span class="tag">Mocked</span>' if mocked else ""
            shots.append(
                f'<li><a href="{_e(path)}"><img src="{_e(path)}" alt="{_e(alt)}"'
                f' loading="lazy"{size}></a><span class="cap">{_e(label)}{tag}</span></li>')
            continue
        word = _EVIDENCE_KINDS.get(kind, kind.capitalize())
        text = f"{_e(word)}: {_e(label)}"
        if path:
            others.append(f'<li><a href="{_e(path)}">{text}</a></li>')
        else:
            raw = _str(item.get("path"))
            others.append(f"<li>{text}" + (f' <code>{_e(raw)}</code>' if raw else "") + "</li>")
    out = ""
    if shots:
        out += '<ul class="shots">' + "".join(shots) + "</ul>"
    if others:
        out += '<ul class="ev">' + "".join(others) + "</ul>"
    return out


def _mock_rules(value: Any) -> list[str]:
    """Active mock rules as short text: ``m_3 GET */api/x → 404``."""
    if not isinstance(value, list):
        return []
    out = []
    for rule in value:
        if isinstance(rule, dict):
            parts = [_str(rule.get(key)) for key in ("id", "method")]
            parts.append(_str(rule.get("url")) or _str(rule.get("match"))
                         or _str(rule.get("host")))
            text = " ".join(part for part in parts if part)
            status = _str(rule.get("status"))
            if status:
                text += f" \u2192 {status}"
            if text:
                out.append(text)
        elif _str(rule):
            out.append(str(_str(rule)))
    return out


def _requests(check: dict[str, Any]) -> str:
    """The requests captured in a check's window, collapsed."""
    items = _dicts(check.get("requests"))
    more = _num(check.get("requests_truncated"))
    more = int(more) if more and more > 0 else 0
    if not items and not more:
        return ""
    rows = []
    for item in items:
        method = _str(item.get("method")) or "?"
        where = (_str(item.get("host")) or "") + (_str(item.get("path")) or "")
        status = _str(item.get("status")) or "no status"
        extra = []
        if item.get("mocked") is True:
            extra.append("mocked")
        duration = _num(item.get("duration_ms"))
        if duration is not None and duration >= 0:
            extra.append(f"{_fmt_num(duration)} ms")
        rows.append(f"<li><b>{_e(method)}</b> {_e(where)} \u2014 {_e(status)}"
                    + (f' <span class="sub">({_e(", ".join(extra))})</span>' if extra else "")
                    + "</li>")
    if more:
        rows.append(f'<li><span class="sub">\u2026 and {more} more not kept in the pack'
                    "</span></li>")
    return (f'<details class="repro"><summary>{_chev()}Requests '
            f'<span class="count">{len(items) + more}</span></summary>'
            f'<ol class="steps">{"".join(rows)}</ol></details>')


def _step(step: Any) -> str:
    if isinstance(step, str):
        return f"<li>{_e(step)}</li>" if step.strip() else ""
    if not isinstance(step, dict):
        return ""
    verb = _str(step.get("verb")) or "step"
    parts = [f"<b>{_e(verb)}</b>"]
    args = _strs(step.get("args"))
    if args:
        parts.append(_e(" ".join(args)))
    selector = step.get("selector")
    if isinstance(selector, dict) and selector:
        shown = " ".join(f"{key}={_str(value) or ''}" for key, value in selector.items())
        parts.append(f"<b>{_e(shown)}</b>")
    extra = []
    if step.get("origin") == "human":
        extra.append("by hand")
    mocks = _mock_rules(step.get("mocks_active"))
    if mocks:
        extra.append("mocks: " + ", ".join(mocks))
    if extra:
        parts.append(f'<span class="sub">({_e("; ".join(extra))})</span>')
    return "<li>" + " ".join(parts) + "</li>"


def _steps(steps: Any) -> str:
    if not isinstance(steps, list):
        return ""
    items = "".join(_step(step) for step in steps)
    return f'<ol class="steps">{items}</ol>' if items else ""


def _chev() -> str:
    return '<span class="chev" aria-hidden="true"></span>'


# --- sections ------------------------------------------------------------------------


def _topline(s: dict[str, Any]) -> str:
    parts = ['<span>Autonom \u00b7 Pre-test pack</span>']
    pid = _str(s.get("pretest_id"))
    if pid:
        parts.append(f'<code class="pid">{_e(pid)}</code>')
    stamp = _time(s.get("created_at"))
    if stamp:
        parts.append(stamp)
    return '<div class="topline">' + "".join(parts) + "</div>"


def _reason_text(model: _Model, reason: str) -> str:
    """``check chk-9 failed`` as ``chk-9 failed: <the check's title>``."""
    match = _CHECK_REASON.match(reason)
    if not match:
        return reason
    title = _str((model.by_check.get(match.group(1)) or {}).get("title"))
    text = f"{match.group(1)} {match.group(2)}"
    return f"{text}: {title}" if title else text


def _hero(model: _Model) -> str:
    s = model.s
    status, glyph, word = _verdict(s.get("verdict"))
    eyebrow = []
    if _str(s.get("ticket_ref")):
        eyebrow.append(_e(_str(s.get("ticket_ref"))))
    target = s.get("target") if isinstance(s.get("target"), dict) else {}
    platform = _str(target.get("platform"))
    if platform:
        eyebrow.append(_e(_PLATFORMS.get(platform, platform)))
    if _str(target.get("target_id")):
        eyebrow.append(_e(_str(target.get("target_id"))))
    if _str(s.get("app_id")):
        eyebrow.append(_e(_str(s.get("app_id"))))
    parent = _pack_name(s.get("parent_pack"))
    if parent:
        kind = s.get("kind")
        lead = "Rerun of pack" if kind == "rerun" else "Follows pack"
        eyebrow.append(f"{lead} {_e(parent)}")
    title = _str(s.get("title")) or "Untitled pre-test"
    reasons = []
    for reason in s.get("verdict_reasons") if isinstance(s.get("verdict_reasons"), list) else []:
        if isinstance(reason, dict):
            reason = reason.get("message") or reason.get("reason") or reason.get("text")
        if _str(reason):
            reasons.append(_str(reason))
    out = [f'<header class="hero s-{status}">']
    if eyebrow:
        out.append('<p class="eyebrow">' + " \u00b7 ".join(eyebrow) + "</p>")
    out.append(f"<h1>{_e(title)}</h1>")
    out.append('<div class="pills">'
               f'<span class="pill s-{status}"><span aria-hidden="true">{glyph}</span> '
               f'<span class="sr">Ticket verdict: </span>{_e(word)}</span>'
               f'<span class="rstate">{model.review.state_html()}</span></div>')
    if reasons:
        out.append(f'<p class="reason">{_e(_reason_text(model, reasons[0]))}</p>')
    out.append("</header>")
    return "".join(out)


def _stats(model: _Model) -> str:
    s = model.s
    counts: dict[str, int] = {}
    for check in model.checks:
        verdict = _check_verdict(check)
        counts[verdict] = counts.get(verdict, 0) + 1
    words = {"pass": "pass", "fail": "fail", "blocked": "blocked",
             "not_covered": "not covered", "inconclusive": "inconclusive"}
    parts = [f"{counts[key]} {words[key]}" for key in words if counts.get(key)]
    others = sum(n for key, n in counts.items() if key not in words)
    if others:
        parts.append(f"{others} other")
    cards, rows = model.split_findings()
    timing = s.get("timing") if isinstance(s.get("timing"), dict) else {}
    wall = _dur(timing.get("wall_s"))
    actions = _num(timing.get("actions"))
    budget = s.get("budget") if isinstance(s.get("budget"), dict) else {}
    max_actions = _num(budget.get("max_actions"))
    if actions is None:
        actions_text, actions_sub = "\u2014", "not recorded"
    else:
        actions_text = _fmt_num(actions)
        actions_sub = f"of {_fmt_num(max_actions)}" if max_actions else "device actions"
    tiles = [
        ("Checks", str(len(model.checks)), " \u00b7 ".join(parts) or "none planned"),
        ("Findings", str(len(cards) + len(rows)),
         f"{len(cards)} confirmed \u00b7 {len(rows)} automatic"),
        ("Time", wall or "\u2014", "wall time" if wall else "not recorded"),
        ("Actions", actions_text, actions_sub),
    ]
    items = "".join(
        f'<li class="tile"><span class="k">{_e(k)}</span><span class="n">{_e(n)}</span>'
        f'<span class="d">{_e(d)}</span></li>' for k, n, d in tiles)
    return f'<ul class="stats" aria-label="Summary">{items}</ul>'


def _jump() -> str:
    links = (("findings", "Findings"), ("checks", "Checks"), ("health", "Device health"),
             ("review", "Review"), ("coverage", "Coverage"))
    return ('<nav class="jump" aria-label="Sections">'
            + "".join(f'<a href="#{key}">{word}</a>' for key, word in links) + "</nav>")


def _finding_card(model: _Model, finding: dict[str, Any], confirmer: str | None) -> str:
    severity = finding.get("severity")
    sev_word = _lookup(_SEVERITIES, severity) or _str(severity) or "Unrated"
    sev_cls = severity if _lookup(_SEVERITIES, severity) else "info"
    fid = _str(finding.get("finding_id"))
    out = ['<li class="card finding">',
           f'<div class="fhead"><span class="sev sev-{sev_cls}">{_e(sev_word)}</span>'
           + (f'<code class="fid">{_e(fid)}</code>' if fid else "") + "</div>",
           f'<h3>{_e(_str(finding.get("title")) or "Untitled finding")}</h3>']
    expected = _str(finding.get("expected"))
    actual = _str(finding.get("actual"))
    out.append('<dl class="ea">'
               f'<div><dt>Expected</dt><dd>{_e(expected) if expected else "<span class=sub>Not stated</span>"}</dd></div>'
               f'<div><dt>Actual</dt><dd>{_e(actual) if actual else "<span class=sub>Not stated</span>"}</dd></div>'
               "</dl>")
    meta = []
    check_id = finding.get("check_id")
    if _str(check_id):
        meta.append("in " + model.check_link(check_id))
    if finding.get("origin") == "auto":
        source = _lookup(_SOURCES, finding.get("source")) or _str(finding.get("source")) or ""
        meta.append(_e(("Automatic " + source.lower()).strip() if source else "Automatic"))
    else:
        meta.append("Agent")
    if confirmer:
        meta.append("confirmed by " + _e(confirmer))
    elif finding.get("confirmed") is False and finding.get("origin") != "auto":
        meta.append("Unconfirmed")
    reproduced = _str(finding.get("reproduced"))
    stability = _str(finding.get("stability"))
    if reproduced:
        meta.append("Reproduced " + _e(reproduced) + (", " + _e(stability) if stability else ""))
    elif stability:
        meta.append(_e(stability.capitalize()))
    draft = _str(finding.get("draft_flow"))
    if draft:
        meta.append(f'Draft flow: <code>{_e(draft)}</code>')
    out.append('<p class="meta">' + " \u00b7 ".join(meta) + "</p>")
    out.append(_evidence(model, finding.get("evidence"), check_id))
    steps = finding.get("repro") if isinstance(finding.get("repro"), list) else None
    if steps is None and _str(check_id) in model.by_check:
        steps = model.by_check[str(_str(check_id))].get("repro")
    rendered = _steps(steps)
    if rendered:
        out.append(f"<details class=\"repro\"><summary>{_chev()}Steps to reproduce</summary>"
                   f"{rendered}</details>")
    out.append("</li>")
    return "".join(out)


def _auto_row(model: _Model, finding: dict[str, Any]) -> str:
    fid = _str(finding.get("finding_id")) or ""
    source = (_lookup(_SOURCES, finding.get("source")) or _str(finding.get("source"))
              or "Signal")
    title = _e(_str(finding.get("title")) or "Untitled signal")
    dismissed = fid in model.review.dismissed
    if dismissed:
        title = f"<s>{title}</s>"
    meta = [_e(source)]
    if _str(finding.get("check_id")):
        meta.append("in " + model.check_link(finding.get("check_id")))
    if fid:
        meta.append(f"<code>{_e(fid)}</code>")
    if dismissed:
        meta.append("dismissed by " + _e(model.review.dismissed[fid] or "review"))
    actual = _str(finding.get("actual"))
    return ('<li class="auto"><div class="arow"><span class="chip-auto">Automatic</span>'
            f'<span class="atitle">{title}</span></div>'
            + (f'<p class="aact">{_e(actual)}</p>' if actual else "")
            + '<p class="meta">' + " \u00b7 ".join(meta) + "</p>"
            + _evidence(model, finding.get("evidence"), finding.get("check_id")) + "</li>")


def _findings(model: _Model) -> str:
    cards, rows = model.split_findings()
    total = len(cards) + len(rows)
    out = ['<section id="findings" aria-labelledby="findings-h">',
           f'<h2 id="findings-h">Findings <span class="count">{total}</span></h2>']
    if not total:
        out.append('<p class="empty">No findings.</p>')
    if cards:
        out.append('<ol class="cards">' + "".join(
            _finding_card(model, finding, who) for finding, who in cards) + "</ol>")
    if rows:
        out.append('<h3 class="subhead">Automatic signals \u2014 unconfirmed</h3>')
        out.append('<ul class="autos">' + "".join(_auto_row(model, f) for f in rows) + "</ul>")
    out.append("</section>")
    return "".join(out)


def _check_row(model: _Model, check: dict[str, Any], seen: set[str]) -> str:
    cid = str(_str(check.get("check_id")))
    verdict = _check_verdict(check)
    status, _glyph, _word = _verdict(verdict)
    never_run = check.get("state") in (None, "added") and not _str(check.get("verdict"))
    took = _dur(check.get("duration_s"))
    if never_run or (verdict == "not_covered" and took is None):
        duration = "Never run"
    else:
        duration = took or "\u2014"
    sub = [_e(_str(check.get("kind")) or "check")]
    if _str(check.get("case")):
        sub.append(_e(_str(check.get("case"))))
    runs = _strs(check.get("run_ids"))
    if runs:
        sub.append("runs " + _e(", ".join(runs)))
    is_open = verdict in ("fail", "blocked", "inconclusive")
    body = []
    why = _str(check.get("why"))
    if why:
        body.append(f'<p><span class="lbl">Why</span> {_e(why)}</p>')
    note = _str(check.get("note"))
    if note:
        body.append(f'<p><span class="lbl">Note</span> {_e(note)}</p>')
    before = check.get("before") if isinstance(check.get("before"), dict) else None
    after = check.get("after") if isinstance(check.get("after"), dict) else None
    if before or after:
        pair = ['<div class="ba">']
        pair.append('<span class="lbl">Before</span> '
                    + (_status_chip(before.get("verdict")) if before else '<span class="sub">none</span>'))
        pair.append('<span aria-hidden="true">\u2192</span>')
        pair.append('<span class="lbl">After</span> '
                    + (_status_chip(after.get("verdict")) if after else '<span class="sub">none</span>'))
        pair.append("</div>")
        body.append("".join(pair))
        if before:
            body.append(_evidence(model, before.get("evidence"), cid))
        if after:
            body.append(_evidence(model, after.get("evidence"), cid))
    body.append(_evidence(model, check.get("evidence"), cid))
    steps = _steps(check.get("repro"))
    if steps:
        body.append('<p class="lbl">Steps</p>' + steps)
    requests = _requests(check)
    if requests:
        body.append(requests)
    content = "".join(body) or '<p class="sub">Nothing recorded for this check.</p>'
    filt = status if status in ("pass", "fail", "blocked", "nc") else "other"
    anchor = model.anchors.get(cid)
    ident = f' id="{anchor}"' if anchor and anchor not in seen else ""
    if anchor:
        seen.add(anchor)
    return (f'<details class="row"{ident} data-v="{filt}"'
            + (" open" if is_open else "") + ">"
            f"<summary>{_chev()}<span class=\"rmain\">{_status_chip(verdict)} "
            f'<span class="rtitle">{_e(_str(check.get("title")) or "Untitled check")}</span>'
            f'<span class="rsub"><code>{_e(cid)}</code> \u00b7 ' + " \u00b7 ".join(sub)
            + f'</span></span><span class="dur">{_e(duration)}</span></summary>'
            f'<div class="rbody">{content}</div></details>')


def _checks(model: _Model) -> str:
    out = ['<section id="checks" aria-labelledby="checks-h">',
           f'<h2 id="checks-h">Checks <span class="count">{len(model.checks)}</span></h2>']
    if not model.checks:
        out.append('<p class="empty">No checks were planned \u2014 the ticket is not covered.</p>')
        out.append("</section>")
        return "".join(out)
    counts = {"all": len(model.checks)}
    for check in model.checks:
        status = _verdict(_check_verdict(check))[0]
        counts[status] = counts.get(status, 0) + 1
    chips = (("all", "All"), ("fail", "Failed"), ("blocked", "Blocked"),
             ("nc", "Not covered"), ("pass", "Passed"))
    out.append('<div class="filters" id="check-filters" role="group" '
               'aria-label="Show checks" hidden>' + "".join(
                   f'<button type="button" data-f="{key}" aria-pressed="'
                   f'{"true" if key == "all" else "false"}">{word} '
                   f'<span class="cnt">{counts.get(key, 0)}</span></button>'
                   for key, word in chips) + "</div>")
    lists = []
    for key, word in (("not_covered", "Not covered"), ("blocked", "Blocked"), ("open", "Open")):
        ids = _strs(model.s.get(key))
        if ids:
            lists.append(f"{word}: " + ", ".join(model.check_link(i) for i in ids))
    if lists:
        out.append('<p class="meta lists">' + " \u00b7 ".join(lists) + "</p>")
    seen: set[str] = set()
    out.append('<div class="list" id="check-list">'
               + "".join(_check_row(model, check, seen) for check in model.checks) + "</div>")
    out.append("</section>")
    return "".join(out)


def _health_part(title: str, part: Any, items_key: str, render_item, empty: str) -> str:
    if not isinstance(part, dict):
        count = '<span class="unav">Unavailable</span>'
        body = '<p class="unav">Not captured for this pre-test.</p>'
        return (f'<details class="row"><summary>{_chev()}<span class="rmain">'
                f'<span class="rtitle">{_e(title)}</span></span>'
                f'<span class="dur">{count}</span></summary><div class="rbody">{body}</div></details>')
    if part.get("state") == "unavailable":
        reason = _str(part.get("reason")) or "no reason recorded"
        return (f'<details class="row"><summary>{_chev()}<span class="rmain">'
                f'<span class="rtitle">{_e(title)}</span></span>'
                f'<span class="dur"><span class="unav">Unavailable</span></span></summary>'
                f'<div class="rbody"><p class="unav">Unavailable: {_e(reason)}</p></div></details>')
    items = _dicts(part.get(items_key))
    if not items:
        return (f'<details class="row"><summary>{_chev()}<span class="rmain">'
                f'<span class="rtitle">{_e(title)}</span></span>'
                f'<span class="dur">{_e(empty)}</span></summary>'
                f'<div class="rbody"><p class="sub">{_e(empty)}.</p></div></details>')
    body = "".join(render_item(item) for item in items)
    return (f'<details class="row" open><summary>{_chev()}<span class="rmain">'
            f'<span class="rtitle">{_e(title)}</span></span>'
            f'<span class="dur">{len(items)}</span></summary>'
            f'<div class="rbody">{body}</div></details>')


def _health(model: _Model) -> str:
    health = model.s.get("health")
    out = ['<section id="health" aria-labelledby="health-h">',
           '<h2 id="health-h">Device health</h2>']
    keys = ("crashes", "logs", "requests", "metrics")
    if not isinstance(health, dict) or not any(key in health for key in keys):
        out.append('<p class="empty">Nothing was captured.</p></section>')
        return "".join(out)

    def link(item: dict[str, Any]) -> str:
        return (" \u00b7 " + model.check_link(item.get("check_id"))) if _str(item.get("check_id")) else ""

    def crash(item: dict[str, Any]) -> str:
        return ('<ul class="plain"><li><code>' + _e(_str(item.get("name")) or "crash")
                + "</code>" + (" \u00b7 " + _time(item.get("time")) if _str(item.get("time")) else "")
                + link(item) + "</li></ul>")

    def log(item: dict[str, Any]) -> str:
        level = (_str(item.get("level")) or "").lower()
        err = " err" if level in ("error", "fatal", "e", "f", "fault", "critical") else ""
        count = _num(item.get("count"))
        return (f'<ul class="logs"><li class="{err.strip()}"><span class="badge">'
                f'{_e(_fmt_num(count) if count is not None else "?")}</span>'
                f'<span class="lline">{_e(_str(item.get("pattern")) or "")}'
                + (f' <span class="sub">({_e(level)}{link(item)})</span>' if level or link(item) else "")
                + "</span></li></ul>")

    def request(item: dict[str, Any]) -> str:
        method = _str(item.get("method")) or "?"
        path = _str(item.get("path")) or "/"
        status = _str(item.get("status")) or (_str(item.get("reason")) or "no response")
        reason = _str(item.get("reason"))
        host = _str(item.get("host"))
        return ('<ul class="reqs"><li>'
                f'<span class="rq"><b>{_e(method)}</b> <code>{_e(path)}</code></span>'
                f'<span>\u2014 {_e(status)}</span>'
                + (f'<span class="sub">{_e(reason)}</span>' if reason and reason != status else "")
                + (f'<span class="sub"><code>{_e(host)}</code></span>' if host else "")
                + (f"<span>{model.check_link(item.get('check_id'))}</span>"
                   if _str(item.get("check_id")) else "")
                + "</li></ul>")

    def metric(item: dict[str, Any]) -> str:
        name = _str(item.get("metric")) or "metric"
        before, after = _str(item.get("before")), _str(item.get("after"))
        return ('<ul class="plain"><li>'
                f"<b>{_e(name)}</b> {_e(_signed(item.get('delta')))} (lead, not a leak)"
                + (f' <span class="sub">{_e(before or "?")} \u2192 {_e(after or "?")}</span>'
                   if before or after else "")
                + link(item) + "</li></ul>")

    out.append('<div class="list">')
    out.append(_health_part("Crashes", health.get("crashes"), "new", crash, "None since start"))
    out.append(_health_part("Error logs", health.get("logs"), "groups", log, "None since start"))
    out.append(_health_part("Failed requests", health.get("requests"), "failed", request,
                            "None since start"))
    out.append(_health_part("Metrics leads", health.get("metrics"), "leads", metric,
                            "None since start"))
    out.append("</div></section>")
    return "".join(out)


def _review(model: _Model) -> str:
    commands = model.s.get("review_commands") if isinstance(
        model.s.get("review_commands"), dict) else {}
    options = (
        ("approve", "Approve", "The pre-test is enough; QA takes it from here."),
        ("more_checks", "More checks", "Starts a linked pre-test with the checks you name."),
        ("return_to_dev", "Return to dev", "Sends the findings back to the developer."),
    )
    out = ['<section id="review" class="panel" aria-labelledby="review-h">',
           '<h2 id="review-h">Review</h2>',
           f'<p class="rnow"><span class="rstate">{model.review.state_html()}</span></p>']
    note = _str(model.review.latest.get("note")) if model.review.latest else None
    if note:
        out.append(f'<p class="sub">{_e(note)}</p>')
    for key, title, consequence in options:
        command = _str(commands.get(key))
        slot = key.replace("_", "-")
        out.append(f'<div class="opt"><h3>{_e(title)}</h3><p class="sub">{_e(consequence)}</p>')
        if command:
            out.append(f'<code class="cmd" id="cmd-{slot}" data-pack>{_e(command)}</code>'
                       f'<button type="button" class="copy" data-for="cmd-{slot}" hidden>Copy</button>'
                       '<span class="sr" aria-live="polite"></span>')
        else:
            out.append('<p class="sub">Command not recorded.</p>')
        out.append("</div>")
    if model.review.history:
        out.append('<h3 class="subhead">History</h3><ol class="hist">')
        for entry in reversed(model.review.history):
            decision = _str(entry.get("decision")) or "decision"
            parts = [_e(_DECISIONS.get(decision, decision))]
            if _str(entry.get("author")):
                parts.append(_e(_str(entry.get("author"))))
            stamp = _time(_Review.stamp(entry))
            if stamp:
                parts.append(stamp)
            if _str(entry.get("note")):
                parts.append(_e(_str(entry.get("note"))))
            out.append("<li>" + " \u00b7 ".join(parts) + "</li>")
        out.append("</ol>")
    out.append("</section>")
    return "".join(out)


def _coverage(model: _Model) -> str:
    kit = _dicts(model.s.get("kit"))
    out = ['<section id="coverage" aria-labelledby="coverage-h">',
           '<h2 id="coverage-h">Coverage</h2>']
    if not kit:
        out.append('<p class="empty">No edge-case kit coverage was recorded.</p></section>')
        return "".join(out)
    covered = sum(1 for item in kit if item.get("covered") is True)
    out.append(f'<p class="meta">{covered} of {len(kit)} edge cases covered</p><ul class="kit">')
    for item in kit:
        title = _str(item.get("title")) or _str(item.get("id")) or "case"
        if item.get("covered") is True:
            verdicts = _strs(item.get("verdicts"))
            worst = _worst(verdicts)
            status = _verdict(worst)[0] if worst else "nc"
            words = ", ".join(_verdict(v)[2].lower() for v in verdicts) or "covered"
            out.append(f'<li class="s-{status}"><span class="dot" aria-hidden="true"></span>'
                       f'{_e(title)} <span class="vw">{_e(words)}</span></li>')
        else:
            out.append(f'<li class="skip">{_e(title)} <span class="vw">skipped</span></li>')
    out.append("</ul></section>")
    return "".join(out)


def _files(model: _Model) -> str:
    files = [f for f in _dicts(model.s.get("changed_files")) if _str(f.get("path"))]
    out = ['<section id="files" aria-labelledby="files-h">',
           f'<h2 id="files-h">Changed files <span class="count">{len(files)}</span></h2>']
    if model.s.get("diff_truncated") is True:
        out.append('<p class="meta">The diff was too large to read in full; '
                   "this list may be incomplete.</p>")
    if not files:
        out.append('<p class="empty">No changed files were recorded.</p></section>')
        return "".join(out)
    out.append('<ul class="files">')
    for item in files:
        status = (_str(item.get("status")) or "?")[:1].upper()
        letter = status if status in _FILE_STATUS else "?"
        path = _e(_str(item.get("path")))
        old = _str(item.get("renamed_from"))
        shown = f"{_e(old)} \u2192 {path}" if old else path
        out.append(f'<li><span class="fs" title="{letter}">{letter}</span>'
                   f'<code>{shown}</code></li>')
    out.append("</ul></section>")
    return "".join(out)


def _timing(model: _Model) -> str:
    timing = model.s.get("timing") if isinstance(model.s.get("timing"), dict) else {}
    rows = [("Wall time", _dur(timing.get("wall_s")) or "\u2014")]
    for key, word in (("actions", "Actions"), ("checks", "Checks"),
                      ("findings", "Findings"), ("auto_findings", "Automatic findings")):
        value = _num(timing.get(key))
        rows.append((word, _fmt_num(value) if value is not None else "\u2014"))
    out = ['<section id="timing" aria-labelledby="timing-h">',
           '<h2 id="timing-h">Timing</h2><dl class="tm">']
    for key, value in rows:
        out.append(f"<div><dt>{_e(key)}</dt><dd>{_e(value)}</dd></div>")
    out.append("</dl>")
    per_check = timing.get("checks_s") if isinstance(timing.get("checks_s"), dict) else {}
    if per_check:
        out.append('<h3 class="subhead">Per check</h3><dl class="tm">')
        for cid, seconds in per_check.items():
            out.append(f"<div><dt>{model.check_link(cid)}</dt>"
                       f"<dd>{_e(_dur(seconds) or chr(0x2014))}</dd></div>")
        out.append("</dl>")
    out.append("</section>")
    return "".join(out)


# --- document ----------------------------------------------------------------------

_CSS = """
:root{color-scheme:light dark;
--bg:#fbfbfd;--text:#1d1d1f;--text2:#6a6a6f;--accent:#007563;--surface:#fff;--field:#f2f2f5;
--line:rgb(0 0 0/.09);--mono-bg:#f2f2f5;
--pass:#1a7f37;--fail:#b42318;--blocked:#8a5a00;--nc:#6a6a6f;--inc:#6e40c9;
--r1:22px;--r2:14px;--r3:8px;
--t-title:clamp(1.625rem,1.1rem + 1.8vw,2.25rem);--t-h2:1.3125rem;--t-h3:1.0625rem;
--t-body:1rem;--t-sub:.875rem;--t-foot:.8125rem;--t-cap:.75rem;
--mono:ui-monospace,SFMono-Regular,Menlo,monospace;--sans:system-ui,-apple-system,"Segoe UI",sans-serif}
@media (prefers-color-scheme:dark){:root{
--bg:#000;--text:#f5f5f7;--text2:#a1a1a6;--accent:#00c2a8;--surface:#161617;--field:#232326;
--line:rgb(255 255 255/.16);--mono-bg:#161617;
--pass:#3fb950;--fail:#ff7b72;--blocked:#d29922;--nc:#a1a1a6;--inc:#b392f0}
.shots img{filter:brightness(.85)}
.shots a:hover img,.shots a:focus-visible img{filter:none}}
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html{-webkit-text-size-adjust:100%;text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:var(--t-body)/1.5 var(--sans);overflow-wrap:break-word}
a{color:var(--accent);text-underline-offset:2px}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
code,.cmd{font-family:var(--mono);font-size:.875em;overflow-wrap:anywhere}
h1{font-size:var(--t-title);line-height:1.2;letter-spacing:-.02em;margin:0;overflow-wrap:anywhere}
h2{font-size:var(--t-h2);line-height:1.2;letter-spacing:-.012em;margin:0 0 16px}
h3{font-size:var(--t-h3);line-height:1.2;margin:0}
p{margin:0}
.sr{position:absolute;width:1px;height:1px;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
.sub{color:var(--text2)}
.empty{color:var(--text2)}
.count{color:var(--text2);font-weight:400;font-variant-numeric:tabular-nums;margin-left:4px}
.lbl{color:var(--text2);font-size:var(--t-foot);margin-right:6px}
.meta{font-size:var(--t-foot);color:var(--text2);margin-top:12px}
.wrap{max-width:1200px;margin:0 auto;padding:0 16px}
.topline{display:flex;flex-wrap:wrap;align-items:center;gap:4px 12px;min-height:44px;border-bottom:1px solid var(--line);font-size:var(--t-foot);color:var(--text2)}
.topline code{color:var(--text)}
.s-pass{--v:var(--pass)}.s-fail{--v:var(--fail)}.s-blocked{--v:var(--blocked)}.s-nc{--v:var(--nc)}.s-inc{--v:var(--inc)}
.hero{border-top:3px solid var(--v);margin-top:24px;padding-top:24px}
.eyebrow{font-size:var(--t-sub);color:var(--text2);margin-bottom:8px;overflow-wrap:anywhere}
.pills{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-top:16px}
.pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:6px 14px;font-size:var(--t-h3);font-weight:600;line-height:1.2;background:color-mix(in srgb,var(--v) 12%,transparent);color:var(--v)}
.rstate{display:inline-block;border-radius:999px;padding:6px 12px;background:var(--field);font-size:var(--t-sub)}
.reason{margin-top:12px;overflow-wrap:anywhere}
.stats{list-style:none;margin:32px 0 0;padding:0;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.tile{background:var(--field);border-radius:var(--r2);padding:16px;display:flex;flex-direction:column;gap:2px;min-width:0}
.tile .k{font-size:var(--t-sub);color:var(--text2)}
.tile .n{font-size:var(--t-h2);font-weight:600;line-height:1.2;font-variant-numeric:tabular-nums}
.tile .d{font-size:var(--t-foot);color:var(--text2)}
.jump{display:flex;flex-wrap:wrap;gap:0 20px;margin-top:16px}
.jump a{display:inline-flex;align-items:center;min-height:44px}
.layout{display:grid;grid-template-columns:minmax(0,1fr);gap:32px;margin-top:32px}
.col-main,.rail{display:grid;grid-template-columns:minmax(0,1fr);gap:32px;align-content:start;min-width:0}
section{min-width:0}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r1);padding:20px 16px}
.cards,.autos,.plain,.ev,.hist{list-style:none;margin:0;padding:0}
.cards{display:grid;gap:16px}
.fhead{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.fid{font-size:var(--t-foot);color:var(--text2)}
.sev,.chip-auto{display:inline-flex;align-items:center;border-radius:999px;padding:2px 10px;font-size:var(--t-foot);font-weight:600}
.sev-blocker{background:var(--fail);color:var(--bg)}
.sev-major{background:color-mix(in srgb,var(--fail) 12%,transparent);color:var(--fail)}
.sev-minor{background:color-mix(in srgb,var(--blocked) 12%,transparent);color:var(--blocked)}
.sev-info,.chip-auto{background:var(--field);color:var(--text)}
.ea{display:grid;grid-template-columns:minmax(0,1fr);gap:12px;margin:12px 0 0}
.ea dt{font-size:var(--t-cap);color:var(--text2)}
.ea dd{margin:2px 0 0;overflow-wrap:anywhere}
.subhead{margin:24px 0 8px;font-size:var(--t-h3)}
.autos li{border-top:1px solid var(--line);padding:12px 0}
.autos li:last-child{border-bottom:1px solid var(--line)}
.arow{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.atitle{font-weight:600;overflow-wrap:anywhere}
.aact{margin-top:4px;font-size:var(--t-sub);overflow-wrap:anywhere}
.autos .meta{margin-top:4px}
.shots{list-style:none;margin:12px 0 0;padding:0;display:flex;flex-wrap:wrap;gap:12px}
.shots li{width:calc(50% - 6px)}
.shots a{display:block;border-radius:var(--r2)}
.shots img{display:block;width:100%;height:auto;border-radius:var(--r2);border:1px solid var(--line);background:var(--field)}
.shots .cap{display:block;margin-top:4px;font-size:var(--t-cap);color:var(--text2);overflow-wrap:anywhere}
.tag{display:inline-block;margin-left:4px;padding:0 6px;border-radius:999px;background:var(--field);color:var(--text2);font-size:var(--t-cap);font-weight:600}
.ev{margin-top:8px;display:flex;flex-wrap:wrap;gap:4px 16px;font-size:var(--t-foot)}
.ev li{overflow-wrap:anywhere;min-width:0}
.ev a{display:inline-flex;align-items:center;min-height:44px}
.missing{color:var(--text2)}
details>summary{list-style:none;cursor:pointer}
details>summary::-webkit-details-marker{display:none}
.chev{display:inline-block;flex:none;width:8px;height:8px;margin:0 6px 0 2px;border-right:2px solid var(--text2);border-bottom:2px solid var(--text2);transform:rotate(-45deg);transition:transform .2s ease-out}
details[open]>summary .chev{transform:rotate(45deg)}
.toggled[open]>summary~*{animation:fade .2s ease-out}
@keyframes fade{from{opacity:0}to{opacity:1}}
.repro{margin-top:12px}
.repro>summary{display:inline-flex;align-items:center;min-height:44px;font-size:var(--t-sub);border-radius:var(--r3)}
.steps{margin:4px 0 0;padding-left:1.75em;font-family:var(--mono);font-size:var(--t-foot);color:var(--text2);overflow-wrap:anywhere}
.steps b{color:var(--text);font-weight:600}
.steps li+li{margin-top:2px}
.filters{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 12px}
.filters button{min-height:44px;border:0;border-radius:999px;padding:0 16px;background:var(--field);color:var(--text);font:inherit;font-size:var(--t-sub);cursor:pointer}
.filters button[aria-pressed=true]{background:color-mix(in srgb,var(--accent) 14%,transparent);color:var(--accent)}
.cnt{font-variant-numeric:tabular-nums;opacity:.8}
.lists{margin:0 0 12px}
.list{background:var(--surface);border:1px solid var(--line);border-radius:var(--r2)}
.row+.row{border-top:1px solid var(--line)}
.row>summary{display:grid;grid-template-columns:auto minmax(0,1fr) auto;align-items:center;gap:4px 12px;min-height:52px;padding:12px 16px;border-radius:var(--r2)}
.rmain{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;min-width:0}
.rtitle{font-weight:600;overflow-wrap:anywhere;min-width:0}
.rsub{flex-basis:100%;font-size:var(--t-foot);color:var(--text2);overflow-wrap:anywhere}
.dur{font-size:var(--t-foot);color:var(--text2);font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.rbody{padding:0 16px 16px;display:grid;gap:8px;min-width:0;overflow-wrap:anywhere}
.vchip{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:2px 10px;font-size:var(--t-foot);font-weight:600;background:color-mix(in srgb,var(--v) 12%,transparent);color:var(--v);white-space:nowrap}
.dot{display:inline-block;flex:none;width:8px;height:8px;border-radius:50%;background:var(--v)}
.ba{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.unav{color:var(--text2);font-style:italic}
.logs,.reqs{list-style:none;margin:0;padding:0}
.logs li{display:flex;align-items:flex-start;gap:8px;background:var(--mono-bg);border-radius:var(--r3);padding:8px 10px;font-family:var(--mono);font-size:var(--t-foot);overflow-wrap:anywhere;min-width:0}
.logs li.err{background:color-mix(in srgb,var(--fail) 12%,transparent)}
.lline{min-width:0}
.badge{flex:none;min-width:2.5em;text-align:center;border-radius:999px;background:var(--surface);font-variant-numeric:tabular-nums}
.reqs li{display:flex;flex-direction:column;gap:2px;font-size:var(--t-sub);overflow-wrap:anywhere;min-width:0}
.plain li{font-size:var(--t-sub);overflow-wrap:anywhere}
.files{list-style:none;margin:0;padding:0;display:grid;gap:6px;font-size:var(--t-foot)}
.files li{display:flex;align-items:baseline;gap:8px;min-width:0}
.files code{min-width:0}
.fs{display:inline-flex;justify-content:center;flex:none;min-width:1.6em;border-radius:var(--r3);background:var(--field);font-family:var(--mono);font-weight:600}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r1);padding:20px}
.panel h2{margin-bottom:8px}
.opt{border-top:1px solid var(--line);margin-top:16px;padding-top:16px}
.opt .sub{font-size:var(--t-sub);margin-top:2px}
.cmd{display:block;margin-top:8px;padding:10px 12px;background:var(--mono-bg);border:1px solid var(--line);border-radius:var(--r3);font-size:var(--t-foot);white-space:pre-wrap;user-select:all;-webkit-user-select:all}
.copy{min-height:44px;margin-top:8px;padding:0 16px;border:0;border-radius:var(--r3);background:var(--field);color:var(--text);font:inherit;font-size:var(--t-sub);white-space:nowrap;cursor:pointer}
.copy.ok{color:var(--accent)}
.hist li{font-size:var(--t-foot);color:var(--text2);padding:6px 0;border-top:1px solid var(--line);overflow-wrap:anywhere}
.kit{list-style:none;margin:12px 0 0;padding:0;display:flex;flex-wrap:wrap;gap:8px}
.kit li{display:inline-flex;align-items:center;gap:6px;border:1px solid transparent;border-radius:999px;padding:6px 12px;background:var(--field);font-size:var(--t-foot);max-width:100%;overflow-wrap:anywhere}
.kit li.skip{background:transparent;border-color:var(--line)}
.vw{color:var(--text2)}
.tm{margin:0}
.tm div{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-top:1px solid var(--line);font-size:var(--t-sub)}
.tm dd{margin:0;font-variant-numeric:tabular-nums;text-align:right}
footer{margin-top:48px;padding:24px 0 48px;border-top:1px solid var(--line);font-size:var(--t-foot);color:var(--text2)}
@media (min-width:600px){
.wrap{padding:0 24px}
.layout,.col-main,.rail{gap:48px}
.layout{margin-top:48px}
.card{padding:20px 24px}
.ea{grid-template-columns:repeat(2,minmax(0,1fr))}
.shots li{width:132px}
.rbody{padding:0 16px 16px 44px}
.reqs li{flex-direction:row;flex-wrap:wrap;gap:4px 12px}}
@media (min-width:48em){.stats{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media (min-width:64em){
.layout{grid-template-columns:minmax(0,1fr) 340px;column-gap:40px;grid-template-areas:"main rail" "files timing"}
.col-main{grid-area:main}.rail{grid-area:rail}#files{grid-area:files}#timing{grid-area:timing}
.panel.stick{position:sticky;top:24px}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
@media print{.filters,.copy,.jump{display:none!important}.panel.stick{position:static}.toggled[open]>summary~*{animation:none}}
@media print{details::details-content{content-visibility:visible;display:contents}}
"""

# Pure helpers of the page script (tested on their own): the pack directory
# from a file: URL path, and a POSIX shell single-quoted word.
_JS_PACK = r"""
function packDir(path){
 var p;
 try{p=decodeURIComponent(path||'');}catch(err){return '';}
 var name='/index.html';
 if(p.length<=name.length||p.slice(-name.length)!==name){return '';}
 return p.slice(0,-name.length);
}
function shellQuote(word){return "'"+String(word).split("'").join("'\\''")+"'";}
"""

_JS = """
(function(){
var d=document;""" + _JS_PACK + """
if(location.protocol==='file:'){
 var dir=packDir(location.pathname);
 if(dir){d.querySelectorAll('code.cmd[data-pack]').forEach(function(c){
  c.textContent=c.textContent.split('<pack>').join(shellQuote(dir));
 });}
}
var bar=d.getElementById('check-filters');
if(bar){
 bar.hidden=false;
 var buttons=bar.querySelectorAll('button[data-f]');
 var rows=d.querySelectorAll('#check-list > details');
 buttons.forEach(function(b){b.addEventListener('click',function(){
  var f=b.getAttribute('data-f');
  buttons.forEach(function(x){x.setAttribute('aria-pressed',x===b?'true':'false');});
  rows.forEach(function(r){r.hidden=!(f==='all'||r.getAttribute('data-v')===f);});
 });});
}
d.querySelectorAll('button.copy').forEach(function(b){
 var src=d.getElementById(b.getAttribute('data-for'));
 var live=b.nextElementSibling;
 var timer=0;
 b.hidden=false;
 function say(text,cls){
  b.textContent=text;b.className=cls?'copy '+cls:'copy';
  if(live){live.textContent=text;}
  clearTimeout(timer);
  timer=setTimeout(function(){b.textContent='Copy';b.className='copy';if(live){live.textContent='';}},1500);
 }
 function fallback(){
  try{
   var range=d.createRange();range.selectNodeContents(src);
   var sel=window.getSelection();sel.removeAllRanges();sel.addRange(range);
   var ok=d.execCommand('copy');sel.removeAllRanges();
   say(ok?'Copied':'Copy failed',ok?'ok':'');
  }catch(err){say('Copy failed','');}
 }
 b.addEventListener('click',function(){
  if(!src){say('Copy failed','');return;}
  if(navigator.clipboard&&navigator.clipboard.writeText){
   navigator.clipboard.writeText(src.textContent).then(function(){say('Copied','ok');},fallback);
  }else{fallback();}
 });
});
var panel=d.getElementById('review');
function fit(){
 if(panel){panel.classList.toggle('stick',window.matchMedia('(min-width:64em)').matches&&panel.offsetHeight+48<=window.innerHeight);}
}
window.addEventListener('resize',fit);fit();
d.querySelectorAll('details').forEach(function(x){x.addEventListener('toggle',function(){x.classList.add('toggled');});});
window.addEventListener('beforeprint',function(){d.querySelectorAll('details').forEach(function(x){x.open=true;});});
})();
"""

_CSP = ("default-src 'none'; img-src 'self' file:; style-src 'unsafe-inline'; "
        "script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")


def render(summary: dict[str, Any], *, review: dict[str, Any] | None = None) -> str:
    """The pack's ``index.html`` for ``summary`` (and the review status, if any)."""
    if not isinstance(summary, dict):
        raise _refuse("the pre-test view needs a summary object",
                      got=type(summary).__name__)
    schema = summary.get("schema")
    if schema != SUMMARY_SCHEMA:
        shown = schema if isinstance(schema, str) else type(schema).__name__
        raise _refuse(f"the summary schema is not {SUMMARY_SCHEMA}", schema=shown[:80])
    if review is not None and not isinstance(review, dict):
        raise _refuse("the review status must be an object", got=type(review).__name__)
    data = _script_json(summary)
    model = _Model(summary, review)
    verdict_word = _verdict(summary.get("verdict"))[2]
    title = _str(summary.get("title")) or "Untitled pre-test"
    doc = (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{_e(verdict_word)} \u00b7 {_e(title)} \u00b7 Pre-test</title>"
        f"<style>{_CSS}</style></head><body><div class=\"wrap\">"
        + _topline(summary) + _hero(model) + _stats(model) + _jump()
        + '<main class="layout">'
        + '<div class="col-main">' + _findings(model) + _checks(model) + _health(model) + "</div>"
        + '<aside class="rail" aria-label="Review and coverage">'
        + _review(model) + _coverage(model) + "</aside>"
        + _files(model) + _timing(model)
        + "</main>"
        + "<footer><p>Generated by Autonom \u00b7 verify with "
        "<code>autonom report verify &lt;pack&gt;</code></p></footer>"
        + "</div>"
        + f'<script type="application/json" id="pretest-data">{data}</script>'
        + f"<script>{_JS}</script>"
        + "</body></html>\n")
    # Belt and braces: no ``scheme://`` sequence survives anywhere in the page.
    return doc.replace("://", ":&#47;&#47;")
