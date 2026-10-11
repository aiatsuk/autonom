"""Every CLI verb has exactly one control class (contract 5.3).

The parser is walked the way `docs/CAPABILITIES.md` is checked against it:
every leaf verb path (aliases such as `runtime-map` included) and every bare
group form that runs on its own (`devices`, `canvas`). Each must sit in
exactly one of the tables `_CONTROL_READONLY`, `_CONTROL_LIFECYCLE`,
`_CONTROL_HOST`, `_CONTROL_MUTATING`, `_CONTROL_FLOW`, `_CONTROL_BY_ACTION`,
so a new verb cannot ship without deciding whether it needs the lease.
"""
from __future__ import annotations

import argparse
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_cli():
    spec = importlib.util.spec_from_file_location("autonom_cli_classification",
                                                  ROOT / "scripts/autonom.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def verb_paths(parser: argparse.ArgumentParser) -> list[str]:
    """Every verb path the parser accepts: leaves, aliases, bare runnable groups."""
    found: list[str] = []

    def walk(node: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        subparsers = [action for action in node._actions  # noqa: SLF001
                      if isinstance(action, argparse._SubParsersAction)]  # noqa: SLF001
        if not subparsers:
            found.append(" ".join(path))
            return
        if path and node._defaults.get("func") is not None:  # noqa: SLF001
            found.append(" ".join(path))
        for action in subparsers:
            for name, child in action.choices.items():
                walk(child, path + (name,))

    walk(parser, ())
    return found


class VerbClassificationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.cli = load_cli()
        cls.parser = cls.cli.build_parser()
        cls.cli._ROOT_PARSER = cls.parser
        cls.paths = verb_paths(cls.parser)
        cls.tables = {
            "readonly": cls.cli._CONTROL_READONLY,
            "lifecycle": cls.cli._CONTROL_LIFECYCLE,
            "host": cls.cli._CONTROL_HOST,
            "mutating": cls.cli._CONTROL_MUTATING,
            "flow": cls.cli._CONTROL_FLOW,
            "by_action": frozenset(cls.cli._CONTROL_BY_ACTION),
        }

    def test_the_walk_reaches_leaves_aliases_and_bare_groups(self) -> None:
        for verb in ("version", "devices", "devices list", "canvas", "canvas serve",
                     "network mock add", "metrics memory warn", "runtime-map diff",
                     "atlas update", "control acquire", "simulator clipboard"):
            self.assertIn(verb, self.paths)
        self.assertGreater(len(self.paths), 140)

    def test_every_verb_is_in_exactly_one_table(self) -> None:
        unclassified, doubled = [], {}
        for verb in self.paths:
            homes = [name for name, table in self.tables.items() if verb in table]
            if not homes:
                unclassified.append(verb)
            elif len(homes) > 1:
                doubled[verb] = homes
        self.assertEqual(unclassified, [], f"verbs without a control class: {unclassified}")
        self.assertEqual(doubled, {}, f"verbs in more than one control table: {doubled}")

    def test_no_table_names_a_verb_the_cli_does_not_have(self) -> None:
        known = set(self.paths)
        for name, table in self.tables.items():
            with self.subTest(table=name):
                self.assertEqual(sorted(set(table) - known), [])

    def test_contract_samples(self) -> None:
        expected = {
            "readonly": ("ui tree", "screenshot", "record start", "metrics frames reset",
                         "metrics memory capture", "control status", "session list",
                         "journal", "devices"),
            "lifecycle": ("session start", "session stop", "devices boot", "canvas",
                          "canvas serve", "cleanup", "control acquire", "control release",
                          "control renew"),
            "host": ("network start", "network mock add", "network mock clear"),
            "mutating": ("devices shutdown", "ui tap", "ui type", "open", "media add",
                         "network attach", "network detach", "network stop",
                         "metrics memory warn", "session clear", "location clear"),
            "flow": ("flow run", "proof", "replay", "ci run", "teach approve",
                     "report serve", "tour"),
        }
        for name, verbs in expected.items():
            for verb in verbs:
                with self.subTest(verb=verb):
                    self.assertIn(verb, self.tables[name])
        self.assertEqual(self.cli._CONTROL_BY_ACTION["ui accessibility"], {"status"})
        self.assertEqual(self.cli._CONTROL_BY_ACTION["simulator clipboard"], {"get"})
        self.assertEqual(self.cli._CONTROL_BY_ACTION["simulator keyboard"], {"show"})
        self.assertEqual(self.cli._CONTROL_BY_ACTION["simulator animations"], {"show"})

    def mode(self, *argv: str) -> str:
        args = self.parser.parse_args(list(argv))
        return self.cli._control_mode(args)

    def test_mode_of_parsed_commands(self) -> None:
        cases = [
            (("ui", "tap", "--x", "1", "--y", "2"), "mutating"),
            (("ui", "tree"), "readonly"),
            (("ui", "accessibility", "status"), "readonly"),
            (("ui", "accessibility", "enable"), "mutating"),
            (("ui", "accessibility", "reset"), "mutating"),
            (("simulator", "clipboard", "get"), "readonly"),
            (("simulator", "clipboard", "set"), "mutating"),
            (("simulator", "keyboard", "show"), "readonly"),
            (("simulator", "keyboard", "pin"), "mutating"),
            (("simulator", "battery", "set"), "mutating"),
            (("session", "start"), "lifecycle"),
            (("session", "start", "--launch", "com.example.app"), "mutating"),
            (("session", "start", "--launch"), "mutating"),
            (("session", "start", "--install", "app.apk"), "mutating"),
            (("teach", "approve", "f.yaml"), "readonly"),
            (("teach", "approve", "f.yaml", "--run"), "flow"),
            (("tour",), "readonly"),
            (("tour", "--run"), "flow"),
            (("flow", "run", "f.yaml"), "flow"),
            (("network", "mock", "add", "--url", "https://x/"), "host"),
            (("runtime-map", "show"), "readonly"),
            (("devices",), "readonly"),
            (("devices", "shutdown"), "mutating"),
            (("canvas",), "lifecycle"),
            (("control", "status"), "readonly"),
            (("control", "acquire", "--holder", "agent:a"), "lifecycle"),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(self.mode(*argv), expected)

    def test_every_mode_is_a_control_mode(self) -> None:
        from autonom_lib import control as control_mod  # noqa: PLC0415
        for verb in self.paths:
            self.assertIn(self.cli._control_mode(self._namespace_for(verb)), control_mod.MODES)

    def _namespace_for(self, verb: str) -> argparse.Namespace:
        """A namespace whose subcommand dests spell `verb` (no parsing needed)."""
        namespace = argparse.Namespace()
        node = self.parser
        for word in verb.split():
            action = next(item for item in node._actions  # noqa: SLF001
                          if isinstance(item, argparse._SubParsersAction))  # noqa: SLF001
            setattr(namespace, action.dest, word)
            node = action.choices[word]
        return namespace


if __name__ == "__main__":
    unittest.main()
