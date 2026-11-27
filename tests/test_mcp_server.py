"""The persistent MCP server of the agent loop (`autonom mcp serve`).

Two layers. `autonom_lib.mcp_server.Server` is driven in this process with a
stub in place of the CLI: it proves the protocol answers and the exact argv
each tool hands to the CLI. Then the real `autonom mcp serve` is started as a
subprocess over the fake device of `test_wait.py` and a whole conversation is
sent down one pipe: the fake call log proves each input went out once, and
that the device tools the commands start never read a request from the
server's stdin. No real device.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import __version__, mcp_server  # noqa: E402

try:
    from test_observe import SERIAL  # noqa: E402
    from test_wait import WaitCase  # noqa: E402
except ImportError:  # direct `python3 -m unittest tests.test_mcp_server` runs
    from tests.test_observe import SERIAL  # noqa: E402
    from tests.test_wait import WaitCase  # noqa: E402


def request(msg_id: int, method: str, params: Any = None) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call(msg_id: int, name: str, **arguments: Any) -> dict[str, Any]:
    return request(msg_id, "tools/call", {"name": name, "arguments": arguments})


class Stub:
    """Stands in for the CLI: records each argv and stdin, answers a reply."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.answer: tuple[int, str, str] = (0, json.dumps({"ok": True}), "")

    def __call__(self, argv: list[str], stdin: str | None) -> tuple[int, str, str]:
        self.calls.append((list(argv), stdin))
        return self.answer


class ServerCase(unittest.TestCase):

    def setUp(self) -> None:
        self.cli = Stub()
        self.server = mcp_server.Server(self.cli, io.BytesIO(), io.BytesIO())

    def text(self, name: str, **arguments: Any) -> tuple[str, bool]:
        answer = self.server.handle(call(1, name, **arguments))
        assert answer is not None
        result = answer["result"]
        self.assertEqual([item["type"] for item in result["content"]], ["text"])
        return result["content"][0]["text"], bool(result.get("isError"))

    def argv(self, name: str, **arguments: Any) -> list[str]:
        self.cli.calls.clear()
        self.text(name, **arguments)
        [(argv, _stdin)] = self.cli.calls
        return argv


class ProtocolTests(ServerCase):

    def test_initialize_echoes_a_known_version_and_names_the_server(self) -> None:
        answer = self.server.handle(request(1, "initialize", {"protocolVersion": "2025-03-26"}))
        result = answer["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertEqual(result["serverInfo"], {"name": "autonom", "title": "Autonom",
                                                "version": __version__})
        self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
        self.assertIn("Never sleep", result["instructions"])

    def test_an_unknown_version_gets_the_latest(self) -> None:
        answer = self.server.handle(request(1, "initialize", {"protocolVersion": "1999-01-01"}))
        self.assertEqual(answer["result"]["protocolVersion"],
                         mcp_server.SUPPORTED_PROTOCOL_VERSIONS[0])

    def test_the_tools_are_the_loop_and_each_has_a_closed_schema(self) -> None:
        tools = self.server.handle(request(1, "tools/list"))["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools],
                         ["devices", "session_start", "session_stop", "screen", "tap",
                          "type_text", "fill", "swipe", "key", "wait", "steps", "screenshot",
                          "autonom"])
        for tool in tools:
            with self.subTest(tool=tool["name"]):
                schema = tool["inputSchema"]
                self.assertEqual((schema["type"], schema["additionalProperties"]),
                                 ("object", False))
                self.assertLessEqual(set(schema.get("required", [])), set(schema["properties"]))
                self.assertGreater(len(tool["description"]), 20)

    def test_notifications_get_no_answer_and_ping_gets_an_empty_one(self) -> None:
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0",
                                              "method": "notifications/initialized"}))
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "id": 9, "result": {}}))
        self.assertEqual(self.server.handle(request(2, "ping")),
                         {"jsonrpc": "2.0", "id": 2, "result": {}})

    def test_what_it_does_not_know_is_an_error_not_an_exit(self) -> None:
        self.assertEqual(self.server.handle(request(3, "resources/list"))["error"]["code"],
                         mcp_server.METHOD_NOT_FOUND)
        self.assertEqual(self.server.handle({"id": 4, "method": "ping"})["error"]["code"],
                         mcp_server.INVALID_REQUEST)
        self.assertEqual(self.server.handle(call(5, "teleport"))["error"]["code"],
                         mcp_server.INVALID_PARAMS)
        self.assertEqual(self.cli.calls, [])

    def test_serve_answers_in_order_and_survives_a_line_that_is_not_json(self) -> None:
        lines = [json.dumps(request(1, "ping")), "not json", "",
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                 json.dumps([request(2, "ping"), request(3, "ping")])]
        out = io.BytesIO()
        server = mcp_server.Server(self.cli, io.BytesIO(("\n".join(lines) + "\n").encode()), out)
        self.assertEqual(server.serve(), 0, "ends when stdin ends")
        answers = [json.loads(line) for line in out.getvalue().decode().splitlines()]
        self.assertEqual([(item.get("id"), "error" in item) for item in answers],
                         [(1, False), (None, True), (2, False), (3, False)])
        self.assertEqual(answers[1]["error"]["code"], mcp_server.PARSE_ERROR)

    def test_a_crash_in_one_call_is_an_internal_error(self) -> None:
        def broken(argv: list[str], stdin: str | None) -> tuple[int, str, str]:
            raise RuntimeError("boom")

        logged: list[str] = []
        server = mcp_server.Server(broken, io.BytesIO(), io.BytesIO(), logged.append)
        answer = server.handle(call(1, "devices"))
        self.assertEqual(answer["error"]["code"], mcp_server.INTERNAL_ERROR)
        self.assertIn("RuntimeError", logged[0])


class ArgvTests(ServerCase):
    """Each tool is the verb an agent would type."""

    def test_session(self) -> None:
        self.assertEqual(
            self.argv("session_start", serial="emulator-5554", app_id="com.example.app",
                      launch=True, label="smoke"),
            ["session", "start", "--profile", "agent", "--serial=emulator-5554",
             "--app-id=com.example.app", "--label=smoke", "--launch"])
        self.assertEqual(self.argv("session_stop", session_id="s_1"),
                         ["session", "stop", "--session-id=s_1"])
        self.assertEqual(self.argv("devices"), ["devices"])

    def test_screen(self) -> None:
        self.assertEqual(self.argv("screen"), ["ui", "tree", "--format", "outline"])
        self.assertEqual(self.argv("screen", interactable=True, max_nodes=80),
                         ["ui", "tree", "--format", "outline", "--max-nodes=80",
                          "--interactable"])

    def test_tap_observes_answers_in_json_and_carries_until_and_shot(self) -> None:
        self.assertEqual(
            self.argv("tap", text="Orders", until=["gone:text=Loading", "request=*/orders*"],
                      until_timeout_ms=15000, shot="orders list"),
            ["ui", "tap", "--text=Orders", "--observe", "--json", "--until=gone:text=Loading",
             "--until=request=*/orders*", "--until-timeout-ms=15000", "--shot=orders list"])
        self.assertEqual(self.argv("tap", desc="Log In", index=1, exact=True),
                         ["ui", "tap", "--desc=Log In", "--index=1", "--mode", "exact",
                          "--observe", "--json"])
        self.assertEqual(self.argv("tap", x=540, y=1210, hold_ms=600),
                         ["ui", "tap", "--x=540", "--y=1210", "--duration=600", "--observe",
                          "--json"])
        self.assertEqual(self.argv("tap", id="app:id/ok", until="text=Done")[-1],
                         "--until=text=Done", "one condition may be a plain string")

    def test_a_value_that_looks_like_a_flag_stays_a_value(self) -> None:
        self.assertEqual(self.argv("tap", text="--help"),
                         ["ui", "tap", "--text=--help", "--observe", "--json"])
        self.assertEqual(self.argv("type_text", value="--sensitive", sensitive=True),
                         ["ui", "type", "--observe", "--json", "--sensitive", "--",
                          "--sensitive"])
        self.assertEqual(self.argv("key", key="BACK"),
                         ["ui", "key", "--observe", "--json", "--", "BACK"])

    def test_fill_and_swipe(self) -> None:
        self.assertEqual(self.argv("fill", value="new", id="app:id/name"),
                         ["ui", "fill", "--resource-id=app:id/name", "--observe", "--json",
                          "--", "new"])
        self.assertEqual(self.argv("swipe", **{"from": [540, 1600], "to": [540, 600],
                                               "seconds": 0.4}),
                         ["ui", "swipe", "--from=540,1600", "--to=540,600", "--duration=0.4",
                          "--observe", "--json"])

    def test_wait_is_one_of_three(self) -> None:
        self.assertEqual(self.argv("wait", text="Welcome", timeout_ms=15000),
                         ["ui", "wait", "--timeout-ms=15000", "--json", "--text=Welcome"])
        self.assertEqual(self.argv("wait", id="app:id/spinner", gone=True),
                         ["ui", "wait", "--json", "--resource-id=app:id/spinner", "--gone"])
        self.assertEqual(self.argv("wait", settled=True), ["ui", "wait", "--json", "--settled"])
        self.assertEqual(self.argv("wait", request="*/v1/orders*", method="POST", status=201),
                         ["network", "requests", "wait", "--path", "*/v1/orders*",
                          "--method=POST", "--status=201", "--json"])

    def test_steps_go_on_stdin(self) -> None:
        steps = [{"tapOn": {"text": "Profile"}}, "back"]
        self.cli.calls.clear()
        self.text("steps", steps=steps)
        [(argv, stdin)] = self.cli.calls
        self.assertEqual(argv, ["ui", "do", "--json"])
        self.assertEqual(json.loads(stdin or ""), steps)

    def test_any_other_verb_by_its_arguments(self) -> None:
        self.assertEqual(self.argv("autonom", args=["network", "mock", "list"]),
                         ["network", "mock", "list"])

    def test_arguments_a_tool_cannot_use_are_told_to_the_model(self) -> None:
        for name, arguments, word in (
                ("tap", {}, "tap needs text, desc or id, or both x and y"),
                ("tap", {"text": "A", "x": 1, "y": 2}, "tap needs"),
                ("tap", {"x": 1}, "tap needs"),
                ("tap", {"text": 5}, "text must be a string"),
                ("tap", {"text": "A", "colour": "red"}, "tap takes no colour"),
                ("wait", {}, "exactly one of"),
                ("wait", {"text": "A", "settled": True}, "exactly one of"),
                ("swipe", {"from": [1], "to": [2, 3]}, "from must be [x, y]"),
                ("type_text", {}, "value is required"),
                ("steps", {"steps": []}, "non-empty list"),
                ("autonom", {"args": []}, "non-empty list of strings")):
            with self.subTest(name=name, arguments=arguments):
                text, failed = self.text(name, **arguments)
                self.assertTrue(failed)
                self.assertIn(word, text)
        self.assertEqual(self.cli.calls, [], "nothing reached the CLI")


class ReplyTests(ServerCase):

    def test_an_action_is_worded_like_an_agent_session(self) -> None:
        self.cli.answer = (0, json.dumps({
            "ok": True, "x": 540, "y": 390, "ref": "n3",
            "screen": {"outline": 'n1 text "General"', "count": 1, "truncated": False},
            "changes": {"added": 1, "removed": 0, "changed": 0, "lines": ['+ n1 text "General"']},
            "foreground": {"app": "com.android.settings", "screen": ".MainActivity"}}), "")
        text, failed = self.text("tap", text="General")
        self.assertFalse(failed)
        self.assertEqual(text.splitlines(), [
            "ui tap ok at 540,390 (n3)",
            "screen: com.android.settings .MainActivity, 1 nodes",
            "changes: +1 -0 ~0", '+ n1 text "General"'])
        whole, _ = self.text("tap", text="General", whole_screen=True)
        self.assertEqual(whole.splitlines()[-2:], ["outline:", 'n1 text "General"'])

    def test_an_expected_failure_is_an_error_with_its_code_and_hint(self) -> None:
        self.cli.answer = (2, "", json.dumps({
            "ok": False, "error_code": "no_matching_node", "error": "no matching node to tap",
            "hint": "Run 'autonom ui tree'."}) + "\n")
        text, failed = self.text("tap", text="Nowhere")
        self.assertTrue(failed)
        self.assertEqual(text, "error no_matching_node: no matching node to tap.\n"
                               "Run 'autonom ui tree'.")

    def test_an_unmet_wait_is_a_reply_not_an_error(self) -> None:
        self.cli.answer = (1, json.dumps({
            "ok": False, "wait": "visible", "selector": {"text": "About"}, "met": False,
            "count": 0, "elapsed_ms": 5003, "timeout_ms": 5000,
            "warnings": [{"code": "wait_not_met", "error": "not met", "hint": "Read the "
                                                                              "screen."}]}), "")
        text, failed = self.text("wait", text="About")
        self.assertFalse(failed)
        self.assertEqual(text.splitlines(), [
            'wait NOT met after 5003 ms: text "About" (0 on screen)', "! Read the screen."])

    def test_the_other_verbs_reply(self) -> None:
        self.cli.answer = (0, json.dumps({"ok": True, "count": 2, "format": "outline",
                                          "outline": 'n1 text "A"\nn2 button "B"',
                                          "screen": [1080, 1920]}), "")
        self.assertEqual(self.text("screen")[0],
                         'screen: 2 nodes, 1080x1920\nn1 text "A"\nn2 button "B"')
        self.cli.answer = (0, json.dumps({"ok": True, "path": "/s/shots/0001_x.jpg",
                                          "width": 460, "height": 1000}), "")
        self.assertEqual(self.text("screenshot", label="x")[0],
                         "shot: /s/shots/0001_x.jpg (460x1000)")
        self.cli.answer = (0, '{"ok":true,"count":0,"mocks":[]}\n', "")
        self.assertEqual(self.text("autonom", args=["network", "mock", "list"])[0],
                         '{"ok":true,"count":0,"mocks":[]}', "printed as it is")


# --- the real server over the fake device --------------------------------------------------


class ServeCliTests(WaitCase):

    def setUp(self) -> None:
        super().setUp()
        self.update_state(after_input={"ui_dump": self.general})
        # the tools pass no --adb: the server's environment names it
        self.env["AUTONOM_ADB"] = str(self.adb)

    def converse(self, *messages: dict[str, Any]) -> dict[int, dict[str, Any]]:
        """Every message down one pipe at once, then EOF; the answers by id."""
        completed = subprocess.run(
            [sys.executable, str(CLI), "mcp", "serve"], cwd=self.root, env=self.env, text=True,
            input="".join(json.dumps(message) + "\n" for message in messages),
            capture_output=True, check=False, timeout=180)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)
        answers = [json.loads(line) for line in completed.stdout.splitlines()]
        return {answer["id"]: answer for answer in answers}

    def said(self, answer: dict[str, Any]) -> list[str]:
        self.assertNotIn("error", answer, answer)
        self.assertFalse(answer["result"].get("isError"), answer)
        return answer["result"]["content"][0]["text"].splitlines()

    def test_a_whole_loop_in_one_process(self) -> None:
        answers = self.converse(
            request(1, "initialize", {"protocolVersion": "2025-06-18"}),
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            call(2, "session_start", serial=SERIAL, app_id="com.android.settings"),
            call(3, "screen"),
            call(4, "tap", text="General", until=["text=About"], shot="general"),
            call(5, "wait", text="Language"),
            call(6, "steps", steps=[{"assertVisible": {"text": "About"}}]),
            call(7, "key", key="KEYCODE_BACK"),
            call(8, "tap", text="Nowhere"),
            call(9, "session_stop"))
        self.assertEqual(sorted(answers), list(range(1, 10)), "every request was answered")
        self.assertRegex(self.said(answers[2])[0],
                         rf"^session s_\w+ on android {SERIAL}, app com\.android\.settings$")
        screen = self.said(answers[3])
        self.assertEqual(screen[0], "screen: 4 nodes, 1080x1920")
        self.assertTrue(any('"Display"' in line for line in screen))
        tap = self.said(answers[4])
        self.assertEqual(tap[0], "ui tap ok at 540,390 (n3)")
        self.assertRegex(tap[1], r'^until met in \d+ ms: text "About"$')
        self.assertEqual(tap[2:4], ["screen: com.android.settings .MainActivity, 3 nodes",
                                    "changes: +3 -4 ~0"])
        [shot] = [line for line in tap if line.startswith("shot: ")]
        self.assertTrue(Path(shot[len("shot: "):]).is_file())
        self.assertRegex(self.said(answers[5])[0],
                         r'^wait met in \d+ ms: text "Language" \(1 on screen\)$')
        self.assertEqual(self.said(answers[6])[:2],
                         ["ui do passed: 1 step(s)", "  1. assertVisible passed"])
        self.assertEqual(self.said(answers[7])[0], "ui key ok KEYCODE_BACK")
        refused = answers[8]["result"]
        self.assertTrue(refused["isError"])
        self.assertTrue(refused["content"][0]["text"].startswith("error no_matching_node:"))
        self.assertRegex(self.said(answers[9])[0], r"^session s_\w+ stopped$")
        self.assertEqual(self.inputs(), [["input", "tap", "540", "390"],
                                         ["input", "keyevent", "KEYCODE_BACK"]],
                         "each input went out once")

    def test_a_default_session_gets_the_same_short_replies(self) -> None:
        self.start()  # started elsewhere, without the agent profile
        answers = self.converse(call(1, "tap", text="General"),
                                call(2, "autonom", args=["session", "show"]))
        self.assertEqual(self.said(answers[1])[:3],
                         ["ui tap ok at 540,390 (n3)",
                          "screen: com.android.settings .MainActivity, 3 nodes",
                          "changes: +3 -4 ~0"])
        shown = json.loads("\n".join(self.said(answers[2])))
        self.assertEqual(shown["session"]["profile"], "default")

    def test_verbs_that_never_end_are_refused(self) -> None:
        self.start()
        answers = self.converse(
            call(1, "autonom", args=["canvas", "serve", "--serial", SERIAL]),
            call(2, "autonom", args=["mcp", "serve"]),
            call(3, "autonom", args=["logs", "follow", "--source", "device"]),
            call(4, "autonom", args=["journal", "--follow"]),
            call(5, "autonom", args=["journal", "--follow", "--from-start", "--max-seconds",
                                     "0.2"]))
        for msg_id, word in ((1, "canvas serve runs until it is stopped"),
                             (2, "mcp serve runs until it is stopped"),
                             (3, "logs follow needs --max-seconds"),
                             (4, "journal --follow needs --max-seconds")):
            with self.subTest(msg_id=msg_id):
                result = answers[msg_id]["result"]
                self.assertTrue(result["isError"])
                self.assertIn(word, result["content"][0]["text"])
        self.assertFalse(answers[5]["result"].get("isError"), answers[5])

    def test_the_verb_is_not_journaled_and_its_calls_are(self) -> None:
        self.start()
        self.converse(call(1, "tap", text="General"))
        code, payload, _ = self.android("journal", "--max", "20")
        self.assertEqual(code, 0, payload)
        verbs = [entry.get("verb") for entry in payload["entries"]]
        self.assertIn("ui tap", verbs)
        self.assertNotIn("mcp serve", verbs)


if __name__ == "__main__":
    unittest.main()
