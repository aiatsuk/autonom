from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1].parent
UI_SCRIPTS = ROOT / "plugins/autonom/skills/android-debugger-agent/scripts"


def ensure_ui_scripts_on_path() -> Path:
    path = str(UI_SCRIPTS)
    if path not in sys.path:
        sys.path.insert(0, path)
    return UI_SCRIPTS
