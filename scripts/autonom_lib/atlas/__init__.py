"""Atlas-lite: a local, observed-only application graph (research doc §10).

Atlas never claims to know the whole app — it records screens and
transitions that were actually seen, with evidence references back to the
sessions and runs that saw them. Anything it has not observed is explicitly
unknown, never inferred.
"""
from __future__ import annotations

ATLAS_SCHEMA_VERSION = 1
