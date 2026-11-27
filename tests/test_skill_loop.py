"""The start skill is the short work loop, and what it teaches is real.

`plugins/autonom/skills/autonom/SKILL.md` is what an agent reads first: it
has to stay short, carry the rule against sleeping, and point at recipes
that exist. Every `autonom ...` command line in it and in its recipes is
parsed with the real parser, so a flag that is renamed or removed fails here
instead of teaching agents a command that does not run.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import re
import shlex
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "plugins/autonom/skills"
START = SKILLS / "autonom"
RECIPES = sorted((START / "recipes").glob("*.md"))

# The start skill is the loop, not the manual: the map lives in references/.
START_LIMIT = 6 * 1024
RECIPE_LIMIT = 3 * 1024


def load_cli():
    spec = importlib.util.spec_from_file_location("autonom_cli_skill_loop",
                                                  ROOT / "scripts/autonom.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def commands(text: str) -> list[str]:
    """Every `autonom ...` command inside the fenced bash blocks of `text`,
    comments removed; for `... | autonom ui do` the part after the pipe."""
    found = []
    for block in re.findall(r"```bash\n(.*?)```", text, re.DOTALL):
        for raw in block.splitlines():
            line = raw.strip()
            if "| autonom " in line:
                line = line.split("| ", 1)[1]
            if not line.startswith("autonom "):
                continue
            words = shlex.split(line, comments=True)
            if words[1:]:
                found.append(shlex.join(words[1:]))
    return found


class StartSkillTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.start = (START / "SKILL.md").read_text(encoding="utf-8")
        cls.flat = " ".join(cls.start.split())

    def test_it_stays_short(self) -> None:
        self.assertLessEqual(len(self.start.encode("utf-8")), START_LIMIT)
        self.assertEqual(len(RECIPES), 5)
        for recipe in RECIPES:
            with self.subTest(recipe=recipe.name):
                self.assertLessEqual(len(recipe.read_bytes()), RECIPE_LIMIT)

    def test_it_forbids_sleeping_and_names_the_waits(self) -> None:
        self.assertIn("**Never `sleep`.**", self.start)
        for text in ("--until text=V", "gone:text=V", "request=", "autonom ui wait --text V",
                     "ui wait --settled", "autonom network requests wait --path GLOB",
                     "--until-timeout-ms", "--timeout-ms"):
            self.assertIn(text, self.flat, text)

    def test_it_says_to_read_the_reply_and_what_an_alert_line_means(self) -> None:
        for text in ("Read the reply", "`>/dev/null`", "`! not on the fast path:`",
                     "`! until_not_met`", "never send it again", "`--shot LABEL`",
                     "`autonom ui do`", "--i-understand-mitm"):
            self.assertIn(text, self.flat, text)

    def test_every_recipe_and_reference_it_points_at_exists(self) -> None:
        linked = re.findall(r"\]\(((?:recipes|references)/[a-z-]+\.md)\)", self.start)
        self.assertEqual(sorted(set(linked)),
                         sorted([f"recipes/{recipe.name}" for recipe in RECIPES]
                                + ["references/system-map.md"]))
        for relative in linked:
            self.assertTrue((START / relative).is_file(), relative)

    def test_the_other_skills_carry_the_rule_too(self) -> None:
        screen = " ".join((SKILLS / "mobile-screen/SKILL.md").read_text(encoding="utf-8").split())
        self.assertIn("## Waiting: never `sleep`", screen)
        for text in ("--until text=V", "`ui wait` with the selector flags",
                     "`network requests wait --path GLOB`", "`until_not_met`"):
            self.assertIn(text, screen, text)
        network = (SKILLS / "mobile-network/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Do not `sleep` while the app talks to the backend", network)
        session = (SKILLS / "mobile-session/SKILL.md").read_text(encoding="utf-8")
        self.assertIn("do not `sleep`", session)

    def test_no_skill_teaches_a_sleep(self) -> None:
        for path in sorted(SKILLS.glob("*/SKILL.md")) + RECIPES:
            with self.subTest(path=str(path.relative_to(SKILLS))):
                self.assertIsNone(re.search(r"^\s*sleep \d", path.read_text(encoding="utf-8"),
                                            re.MULTILINE))


class TaughtCommandsTests(unittest.TestCase):
    """What the loop teaches parses with the real CLI."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.parser = load_cli().build_parser()

    def parse(self, command: str) -> argparse.Namespace:
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            try:
                args, extras = self.parser.parse_known_args(shlex.split(command))
            except SystemExit:
                self.fail(f"autonom {command}: {stderr.getvalue().strip()[:300]}")
        self.assertEqual(extras, [], f"autonom {command}")
        return args

    def test_the_start_skill_and_every_recipe(self) -> None:
        pages = [START / "SKILL.md", *RECIPES]
        total = 0
        for page in pages:
            listed = commands(page.read_text(encoding="utf-8"))
            self.assertTrue(listed, f"{page.name} shows no command")
            for command in listed:
                with self.subTest(page=page.name, command=command):
                    self.parse(command)
                    total += 1
        self.assertGreater(total, 50)

    def test_the_until_conditions_it_shows_are_conditions(self) -> None:
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        from autonom_lib import wait

        shown = 0
        for page in [START / "SKILL.md", *RECIPES]:
            for command in commands(page.read_text(encoding="utf-8")):
                for spec in getattr(self.parse(command), "until", None) or ():
                    with self.subTest(page=page.name, spec=spec):
                        wait.parse_until(spec)
                        shown += 1
        self.assertGreater(shown, 8)

    def test_the_extraction_reads_piped_and_commented_lines(self) -> None:
        text = ("```bash\nautonom ui key BACK   # go back\n"
                "echo '[\"back\"]' | autonom ui do\n# autonom not this\n```\n")
        self.assertEqual(commands(text), ["ui key BACK", "ui do"])


class FlutterSkillTests(unittest.TestCase):

    def test_it_says_when_to_reload_restart_or_rebuild(self) -> None:
        text = " ".join((SKILLS / "flutter-debugger-agent/SKILL.md")
                        .read_text(encoding="utf-8").split())
        for word in ("hot reload", "hot restart", "--pid-file", "kill -USR1", "kill -USR2",
                     '"method":"app.restart"', "fullRestart", "flutter attach",
                     "`main()`, `initState()`", "native code",
                     'autonom ui wait --text "<the new label>"', "profile mode"):
            self.assertIn(word, text, word)


if __name__ == "__main__":
    unittest.main()
