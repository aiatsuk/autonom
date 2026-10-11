"""Ticket pre-test: one ticket and its diff checked on a device, inside a session.

A pre-test lives under ``<session artifacts_dir>/pretests/<pretest_id>/`` and
holds the record (``pretest.json``), the redacted ticket, the diff context, the
device baseline, the budget, and two append-only event logs (checks and
findings). ``pretests/active.json`` names the session's single active pre-test
and its open check; every evidence writer reads only that small file, so a
session without a pre-test behaves byte for byte as before.

Modules (each imported on demand, never from here, so the journal's cheap
attribution lookup does not pull in git, device or flow code):

- ``store``    — layout, record, locks, active pointer, create/stop/pack marks;
- ``context``  — diff hunks, renames, binaries, flow selection, Runtime Map hints;
- ``baseline`` — crash watermark, log cursor, network line count, metrics;
- ``checks``   — check events and their folded state;
- ``findings`` — finding events, evidence refs and their resolution;
- ``kit``      — the edge-case kit and its coverage.
"""
from __future__ import annotations

RECORD_SCHEMA = "autonom.pretest/v1"
CONTEXT_SCHEMA = "autonom.pretest-context/v1"
BASELINE_SCHEMA = "autonom.pretest-baseline/v1"
