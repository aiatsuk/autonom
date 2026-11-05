"""Test package initializer.

Runs only for ``python3 -m unittest tests.test_x`` style runs (under
``unittest discover -s tests`` the modules import under top-level names and
this file never executes; ``tests/run_parallel.py`` puts the same default in
every worker's environment, and ``test_aa_env_snapshot`` applies it for a
plain discover run).

The harness default (INV-5): Android UI verbs use uiautomator and adb input,
never the on-device helper, unless a test asks for the helper itself.
"""
import os

os.environ.setdefault("AUTONOM_ANDROID_UI_BACKEND", "uiautomator")
