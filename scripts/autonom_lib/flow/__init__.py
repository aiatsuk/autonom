"""Flow DSL v1 — a strict, stdlib-only YAML-subset flow language.

Layering (docs/plans/PHASE_5_FLOW_DSL.md):

    parser.py     text → positioned node tree; no flow-schema knowledge
    schema.py     node tree → typed model; command registry + failure classes
    validator.py  semantic checks, runFlow containment + cycle detection
    canonical.py  shorthand expansion + deterministic emitter (flow fmt)

The parser/schema/validator/canonical layers never import device backends;
the executor (a later slice) consumes the typed model and never parses text.
"""
from __future__ import annotations

FLOW_SCHEMA_ID = "autonom.dev/flow/v1"
EVENT_SCHEMA_VERSION = 1
