from __future__ import annotations

import ast
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts/autonom.py"
FLOWS = ROOT / "tests/fixtures/flows_sample.jsonl"
FAKE_ADB = ROOT / "tests/fakes/fake_adb.py"

sys.path.insert(0, str(ROOT / "scripts"))
from autonom_lib import consent, errors  # noqa: E402
from autonom_lib.network import (  # noqa: E402
    device_proxy_android, har, mitm_addon, mocks, proxy, redact, store,
)
from autonom_lib.platform import Target  # noqa: E402

try:
    from env_isolation import EnvSandboxMixin  # noqa: E402  (discover -s tests)
except ImportError:  # direct `python3 -m unittest tests.test_...` runs
    from tests.env_isolation import EnvSandboxMixin  # noqa: E402


def make_record(root: Path) -> dict:
    artifacts = root / ".autonom" / "s_test"
    (artifacts / "network").mkdir(parents=True, exist_ok=True)
    return {"session_id": "s_test", "platform": "android", "target_id": "emulator-5554",
            "artifacts_dir": str(artifacts), "network": {}}


class AddonBoundaryTests(unittest.TestCase):
    """CAP-NET-002 / RISK-007 — the addon runs in mitmproxy's own interpreter."""

    def test_addon_imports_nothing_from_the_repository(self) -> None:
        tree = ast.parse((ROOT / "scripts/autonom_lib/network/mitm_addon.py").read_text("utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
                if node.level:  # a relative import would reach into autonom_lib
                    self.fail("addon uses a relative import")
        self.assertNotIn("autonom_lib", imported)
        self.assertTrue(imported <= {"json", "os", "time", "typing", "fnmatch", "re", "mitmproxy",
                                     "__future__"}, imported)

    def test_addon_is_importable_without_mitmproxy(self) -> None:
        self.assertEqual(mitm_addon.addons, [])

    def test_redaction_tables_stay_in_sync(self) -> None:
        self.assertEqual(redact.REDACTED_HEADERS, mitm_addon.REDACTED_HEADERS)
        self.assertEqual(tuple(redact.SENSITIVE_FIELDS), tuple(mitm_addon.SENSITIVE_FIELDS))

    def test_both_scrubbers_agree(self) -> None:
        for body in ('{"password": "hunter2", "email": "a@b.c"}',
                     "user=bob&password=hunter2",
                     '{"data":{"access_token":"abc","keep":"visible"}}',
                     "no credentials here", ""):
            with self.subTest(body=body):
                self.assertEqual(redact.scrub_body(body), mitm_addon.scrub_body(body))


class RedactionTests(unittest.TestCase):
    """CAP-NET-004 / INV-03 — credentials never reach disk."""

    def test_sensitive_headers_are_masked_but_named(self) -> None:
        headers = redact.redact_headers({"Authorization": "Bearer x", "Content-Type": "json"})
        self.assertEqual(headers["authorization"], redact.PLACEHOLDER)
        self.assertEqual(headers["content-type"], "json")

    def test_every_declared_header_is_masked(self) -> None:
        headers = redact.redact_headers({name: "secret" for name in redact.REDACTED_HEADERS})
        self.assertEqual(set(headers.values()), {redact.PLACEHOLDER})

    def test_credential_body_fields_are_masked(self) -> None:
        scrubbed = redact.scrub_body('{"email":"a@b.c","password":"hunter2"}')
        self.assertNotIn("hunter2", scrubbed)
        self.assertIn("a@b.c", scrubbed)

    def test_nested_and_form_credentials_are_masked(self) -> None:
        self.assertNotIn("abc", redact.scrub_body('{"d":{"access_token":"abc"}}'))
        self.assertNotIn("hunter2", redact.scrub_body("user=bob&password=hunter2&x=1"))

    def test_previews_are_truncated_with_a_marker(self) -> None:
        text = redact.preview("x" * 5000)
        self.assertLessEqual(len(text), redact.PREVIEW_LIMIT + len(redact.TRUNCATION_MARKER))
        self.assertTrue(text.endswith(redact.TRUNCATION_MARKER))


class ProxyBindTests(unittest.TestCase):
    """CAP-NET-001-S02 / INV-05 / RISK-008 — loopback only, no way to widen it."""

    def test_argv_always_binds_loopback(self) -> None:
        argv = proxy.build_argv("mitmdump", port=8080, directory=Path("/tmp/n"),
                                confdir=Path("/tmp/n/ca"), capture_bodies=False)
        self.assertIn("--listen-host", argv)
        self.assertEqual(argv[argv.index("--listen-host") + 1], "127.0.0.1")

    def test_no_wildcard_bind_anywhere_in_the_network_package(self) -> None:
        for path in (ROOT / "scripts/autonom_lib/network").glob("*.py"):
            with self.subTest(module=path.name):
                self.assertNotIn("0.0.0.0", path.read_text("utf-8"))

    def test_host_network_tools_are_never_invoked(self) -> None:
        """N-13 — changing macOS network services is out of bounds."""
        for path in ROOT.rglob("scripts/**/*.py"):
            text = path.read_text("utf-8")
            for forbidden in ("networksetup", "scutil"):
                self.assertNotIn(forbidden, text, f"{path.name} references {forbidden}")

    def test_stop_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = make_record(Path(tmp))
            self.assertEqual(proxy.stop(record), {"was_running": False})

    def test_world_writable_artifacts_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = make_record(Path(tmp))
            os.chmod(record["artifacts_dir"], 0o777)
            with self.assertRaises(errors.AutonomError) as caught:
                proxy.assert_safe_permissions(record)
            self.assertEqual(caught.exception.code, errors.UNSAFE_ARTIFACTS_PERMISSIONS)


class CaContainmentTests(unittest.TestCase):
    """CAP-ATTACH-003 / TEST-508 — the CA private key must never enter artifacts.

    This test was planned but not written in the first pass, and the violation it
    would have caught shipped: mitmproxy's confdir was placed inside
    `<artifacts_dir>`, so `mitmproxy-ca.pem` — which carries the private key —
    landed in the session directory.

    Every test here redirects `AUTONOM_HOME` at setUp. Without that isolation the
    fixtures below would write fake PEM material into the operator's real CA store
    and break the next real proxy start — which is exactly what happened once.
    """

    KEY_MARKERS = (b"PRIVATE KEY", b"BEGIN RSA", b"BEGIN EC PARAMETERS")

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self._previous = os.environ.get("AUTONOM_HOME")
        os.environ["AUTONOM_HOME"] = self.home.name

    def tearDown(self) -> None:
        if self._previous is None:
            os.environ.pop("AUTONOM_HOME", None)
        else:
            os.environ["AUTONOM_HOME"] = self._previous
        self.home.cleanup()

    def test_confdir_is_never_inside_the_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = make_record(Path(tmp))
            store = proxy.ca_store()
            self.assertFalse(
                str(store).startswith(str(Path(record["artifacts_dir"]))),
                "the CA confdir must live outside session artifacts",
            )

    def test_only_certificate_files_may_be_published(self) -> None:
        for name in proxy.CERT_ONLY_FILES:
            self.assertIn("cert", name)
        for forbidden in ("mitmproxy-ca.pem", "mitmproxy-ca.p12", "mitmproxy-dhparam.pem"):
            self.assertNotIn(forbidden, proxy.CERT_ONLY_FILES)

    def test_publishing_copies_no_private_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = make_record(Path(tmp))
            store = proxy.ca_store()
            # Stand-in CA material, shaped like what mitmproxy actually writes.
            (store / "mitmproxy-ca-cert.pem").write_bytes(b"-----BEGIN CERTIFICATE-----\nx\n")
            (store / "mitmproxy-ca.pem").write_bytes(b"-----BEGIN PRIVATE KEY-----\nsecret\n")
            proxy.publish_certificate(record)

            for path in Path(record["artifacts_dir"]).rglob("*"):
                if not path.is_file():
                    continue
                blob = path.read_bytes()
                for marker in self.KEY_MARKERS:
                    self.assertNotIn(marker, blob, f"{path.name} contains private key material")

    def test_ca_store_is_owner_only(self) -> None:
        self.assertEqual(oct(proxy.ca_store().stat().st_mode)[-3:], "700")

    def test_ca_store_honours_an_explicit_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AUTONOM_HOME"] = tmp
            self.assertEqual(proxy.ca_store(), Path(tmp) / "ca")

    def test_the_real_machine_store_is_never_touched_by_tests(self) -> None:
        """Guard the hygiene rule itself: AUTONOM_HOME must be redirected."""
        self.assertTrue(str(proxy.ca_store()).startswith(self.home.name))


_REAL_STDIN = None


def setUpModule() -> None:
    """Keep the consent gate away from the terminal for every test here.

    `consent.require` prompts for the confirmation phrase whenever stdin is a
    TTY. Run from a real shell, the suite therefore stopped and waited for a
    human, and mistyping the phrase failed a test about proxy restoration. It
    went unnoticed because headless runs have no TTY, so the branch was never
    taken there.

    This lives at module scope rather than in a mixin on purpose: a mixin only
    protects classes that remember to call `super().setUp()`, and the first
    class that forgot silently lost the protection. Module setup cannot be
    forgotten by a subclass.

    The interactive branch is still covered — explicitly, through the `prompt`
    seam, with no terminal involved.
    """
    global _REAL_STDIN
    _REAL_STDIN = sys.stdin
    sys.stdin = io.StringIO()


def tearDownModule() -> None:
    if _REAL_STDIN is not None:
        sys.stdin = _REAL_STDIN


class _FakeTty(io.StringIO):
    def isatty(self) -> bool:
        return True


class ConsentTests(EnvSandboxMixin, unittest.TestCase):
    """CAP-ATTACH-001 / INV-04 — no bypass, no caching, refusal before any action."""

    OPERATION = consent.Operation("device_proxy", "android:emulator-5554", "change the proxy",
                                  ("--i-understand-mitm",))

    def test_on_a_terminal_the_exact_phrase_is_required(self) -> None:
        previous = sys.stdin
        sys.stdin = _FakeTty()
        try:
            entry = consent.require(self.OPERATION, acknowledged=True,
                                    stream=io.StringIO(),
                                    prompt=lambda: consent.PHRASE_EN)
            self.assertEqual(entry["operation"], "device_proxy")

            for wrong in ("nah", "yes", "", consent.PHRASE_EN[:-5]):
                with self.assertRaises(errors.AutonomError) as caught:
                    consent.require(self.OPERATION, acknowledged=True,
                                    stream=io.StringIO(), prompt=lambda: wrong)
                self.assertEqual(caught.exception.code, errors.CONSENT_DECLINED)
        finally:
            sys.stdin = previous

    def test_the_suite_never_reaches_for_a_terminal(self) -> None:
        """Guard the guard: if stdin were a TTY here, the suite would block."""
        self.assertFalse(sys.stdin.isatty())

    def test_missing_flag_refuses(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            consent.require(self.OPERATION, acknowledged=False)
        self.assertEqual(caught.exception.code, errors.CONSENT_REQUIRED)

    def test_extra_flag_requirement_refuses(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            consent.require(self.OPERATION, acknowledged=True, extra_required=False)
        self.assertEqual(caught.exception.code, errors.CONSENT_REQUIRED)

    def test_no_environment_variable_can_grant_consent(self) -> None:
        # set_env restores the ambient values afterwards — CI itself is set
        # job-wide on GitHub Actions and must survive this test.
        self.set_env(AUTONOM_CONSENT="1", AUTONOM_I_UNDERSTAND_MITM="1",
                     CI="1", AUTONOM_YES="1")
        with self.assertRaises(errors.AutonomError):
            consent.require(self.OPERATION, acknowledged=False)

    def test_phrase_matching_is_exact_but_tolerant_of_whitespace(self) -> None:
        self.assertTrue(consent.phrase_accepted(consent.PHRASE_EN))
        self.assertTrue(consent.phrase_accepted("  " + consent.PHRASE_EN + ".  "))
        for wrong in ("yes", "y", "I agree", "", consent.PHRASE_EN[:-5]):
            self.assertFalse(consent.phrase_accepted(wrong), wrong)

    def test_grant_produces_an_audit_entry_without_secrets(self) -> None:
        entry = consent.require(self.OPERATION, acknowledged=True)
        self.assertEqual(entry["operation"], "device_proxy")
        self.assertIn("at", entry)
        record: dict = {}
        consent.record(record, entry)
        self.assertEqual(len(record["consent_log"]), 1)


class AndroidAttachTests(EnvSandboxMixin, unittest.TestCase):
    """CAP-ATTACH-002 / INV-07 — restore the prior value, never a hard-coded one."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.set_env(AUTONOM_FAKE_STATE=str(self.state))
        self.target = Target("android", "emulator-5554", str(FAKE_ADB), {"serial": "emulator-5554"})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _round_trip(self, previous: str | None) -> str | None:
        settings = {"http_proxy": previous} if previous is not None else {}
        self.state.write_text(json.dumps({"settings": settings}), encoding="utf-8")
        record = make_record(Path(self.tmp.name))
        device_proxy_android.attach(self.target, record, port=8080, acknowledged=True)
        self.assertEqual(
            json.loads(self.state.read_text())["settings"]["http_proxy"], "10.0.2.2:8080"
        )
        device_proxy_android.detach(self.target, record)
        return json.loads(self.state.read_text())["settings"]["http_proxy"]

    def test_existing_proxy_is_restored_exactly(self) -> None:
        self.assertEqual(self._round_trip("proxy.corp:3128"), "proxy.corp:3128")

    def test_unset_proxy_is_restored_to_unset(self) -> None:
        self.assertEqual(self._round_trip(None), device_proxy_android.UNSET)

    def test_literal_unset_marker_round_trips(self) -> None:
        self.assertEqual(self._round_trip(":0"), ":0")

    def test_attach_without_consent_changes_nothing(self) -> None:
        self.state.write_text(json.dumps({"settings": {"http_proxy": "proxy.corp:3128"}}),
                              encoding="utf-8")
        record = make_record(Path(self.tmp.name))
        with self.assertRaises(errors.AutonomError) as caught:
            device_proxy_android.attach(self.target, record, port=8080, acknowledged=False)
        self.assertEqual(caught.exception.code, errors.CONSENT_REQUIRED)
        self.assertEqual(
            json.loads(self.state.read_text())["settings"]["http_proxy"], "proxy.corp:3128"
        )

    def test_detach_when_not_attached_is_a_no_op(self) -> None:
        record = make_record(Path(self.tmp.name))
        self.assertEqual(device_proxy_android.detach(self.target, record), {"was_attached": False})


class MockRuleTests(unittest.TestCase):
    """CAP-MOCK-001..006 — the persistent registry and its CRUD."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self.tmp.name) / "registry"
        self.registry.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_add_copies_the_body_so_the_source_can_be_deleted(self) -> None:
        source = Path(self.tmp.name) / "err.json"
        source.write_text('{"error":"boom"}', encoding="utf-8")
        rule = mocks.add(url_glob="*/v1/login", method="POST", status=500,
                         body_file=source, registry=self.registry)
        source.unlink()
        self.assertTrue(Path(rule["response"]["body_path"]).exists())
        self.assertEqual(Path(rule["response"]["body_path"]).read_text(), '{"error":"boom"}')

    def test_inline_json_body_is_stored_verbatim(self) -> None:
        rule = mocks.add(url_glob="*/v1/profile", body_text='{"id":7}',
                         registry=self.registry)
        self.assertEqual(Path(rule["response"]["body_path"]).read_text(), '{"id":7}')

    def test_missing_body_file_is_rejected_without_writing_rules(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            mocks.add(url_glob="*", body_file=Path("/nope.json"), registry=self.registry)
        self.assertEqual(caught.exception.code, errors.BODY_FILE_NOT_FOUND)
        self.assertEqual(mocks.load(self.registry), [])

    def test_ids_are_monotonic_and_never_recycled(self) -> None:
        """A recycled id would silently re-point an existing body file."""
        for _ in range(3):
            mocks.add(url_glob="*/a", registry=self.registry)
        self.assertEqual([r["id"] for r in mocks.load(self.registry)],
                         ["m_1", "m_2", "m_3"])
        mocks.remove("m_2", registry=self.registry)
        self.assertEqual([r["id"] for r in mocks.load(self.registry)], ["m_1", "m_3"])
        fresh = mocks.add(url_glob="*/b", registry=self.registry)
        self.assertEqual(fresh["id"], "m_4")

    def test_unknown_id_is_an_error(self) -> None:
        for call in (lambda: mocks.remove("m_99", registry=self.registry),
                     lambda: mocks.get("m_99", registry=self.registry),
                     lambda: mocks.update("m_99", status=500, registry=self.registry)):
            with self.assertRaises(errors.AutonomError) as caught:
                call()
            self.assertEqual(caught.exception.code, errors.MOCK_NOT_FOUND)

    def test_update_touches_only_the_fields_given(self) -> None:
        rule = mocks.add(url_glob="*/v1/a", method="POST", status=200,
                         body_text="original", note="first", registry=self.registry)
        updated = mocks.update(rule["id"], status=503, registry=self.registry)
        self.assertEqual(updated["response"]["status"], 503)
        self.assertEqual(updated["match"]["url_glob"], "*/v1/a")
        self.assertEqual(updated["match"]["method"], "POST")
        self.assertEqual(updated["note"], "first")
        self.assertEqual(Path(updated["response"]["body_path"]).read_text(), "original")

    def test_enable_disable_round_trip(self) -> None:
        rule = mocks.add(url_glob="*/a", registry=self.registry)
        mocks.set_enabled(rule["id"], False, registry=self.registry)
        self.assertEqual(mocks.active(self.registry), [])
        self.assertEqual(len(mocks.load(self.registry)), 1)
        mocks.set_enabled(None, True, all_rules=True, registry=self.registry)
        self.assertEqual(len(mocks.active(self.registry)), 1)

    def test_remove_and_clear_delete_the_bodies_too(self) -> None:
        first = mocks.add(url_glob="*/a", body_text="a", registry=self.registry)
        second = mocks.add(url_glob="*/b", body_text="b", registry=self.registry)
        mocks.remove(first["id"], registry=self.registry)
        self.assertFalse(Path(first["response"]["body_path"]).exists())
        mocks.clear(registry=self.registry)
        self.assertFalse(Path(second["response"]["body_path"]).exists())
        self.assertEqual(mocks.load(self.registry), [])

    def test_writes_are_atomic(self) -> None:
        source = Path(ROOT / "scripts/autonom_lib/network/mocks.py").read_text("utf-8")
        self.assertIn("os.replace", source)
        self.assertNotIn('open(path, "w"', source)

    def test_a_corrupt_registry_reads_as_empty_rather_than_crashing(self) -> None:
        mocks.registry_file(self.registry).write_text("{ not json", encoding="utf-8")
        self.assertEqual(mocks.load(self.registry), [])

    def test_rules_persist_across_sessions_by_design(self) -> None:
        """The 0.6.0 contract is deliberately reversed here.

        Rules used to die with the session. They now outlive it, because a mock
        is a standing decision rather than a per-run flag. The safety property
        that reversal costs is repaid by `summary()` — see the loudness tests.
        """
        mocks.add(url_glob="*/a", registry=self.registry)
        self.assertEqual(len(mocks.load(self.registry)), 1)
        self.assertEqual(mocks.summary(self.registry)["active"], 1)

    def test_summary_names_what_would_be_faked(self) -> None:
        mocks.add(url_glob="*/a", host="api.devbackend.net", registry=self.registry)
        disabled = mocks.add(url_glob="*/b", registry=self.registry)
        mocks.set_enabled(disabled["id"], False, registry=self.registry)
        state = mocks.summary(self.registry)
        self.assertEqual((state["total"], state["active"]), (2, 1))
        self.assertEqual(state["targets"], ["api.devbackend.net"])

    def test_registry_lives_outside_any_repository(self) -> None:
        """A body is often a captured response; a captured response often has a
        token. Keeping the registry out of the working tree makes committing one
        impossible by accident."""
        previous = os.environ.get("AUTONOM_HOME")
        os.environ["AUTONOM_HOME"] = str(Path(self.tmp.name) / "home")
        try:
            resolved = mocks.registry_dir()
        finally:
            if previous is None:
                os.environ.pop("AUTONOM_HOME", None)
            else:
                os.environ["AUTONOM_HOME"] = previous
        self.assertNotIn(str(ROOT), str(resolved))
        self.assertEqual(oct(resolved.stat().st_mode & 0o777), "0o700")


class UrlSugarTests(unittest.TestCase):
    """`--url <exact endpoint>` — the one-liner shape."""

    def test_url_becomes_a_literal_glob_with_the_query_ignored(self) -> None:
        match = mocks.url_to_match("https://api.devbackend.net/post/update/12341?ts=9")
        self.assertEqual(match["url_glob"], "https://api.devbackend.net/post/update/12341")
        self.assertEqual(match["host"], "api.devbackend.net")
        self.assertTrue(match["ignore_query"])

    def test_ignore_query_matches_with_and_without_a_query_string(self) -> None:
        rule = {"enabled": True, "match": mocks.url_to_match(
            "https://api.devbackend.net/post/update/12341")}
        for url in ("https://api.devbackend.net/post/update/12341",
                    "https://api.devbackend.net/post/update/12341?ts=9&x=1"):
            self.assertTrue(mitm_addon.rule_matches(
                rule, "POST", url, "api.devbackend.net"), url)

    def test_ignore_query_does_not_widen_the_path(self) -> None:
        """`/12341` must not swallow `/123415` — the classic prefix mistake."""
        rule = {"enabled": True, "match": mocks.url_to_match(
            "https://api.devbackend.net/post/update/12341")}
        self.assertFalse(mitm_addon.rule_matches(
            rule, "POST", "https://api.devbackend.net/post/update/123415",
            "api.devbackend.net"))

    def test_a_hand_written_glob_keeps_exact_query_control(self) -> None:
        rule = {"enabled": True, "match": {"url_glob": "*/update/12341",
                                           "ignore_query": False}}
        self.assertFalse(mitm_addon.rule_matches(
            rule, "POST", "https://api.devbackend.net/post/update/12341?ts=9", "h"))


class RegistryAddonContractTests(unittest.TestCase):
    """The registry file is the only channel between the CLI and the addon.

    They run in *different interpreters* — mitmproxy ships its own — so nothing
    but the file shape connects them, and no type checker spans the gap. If the
    writer emits a shape the reader cannot parse, every other test still passes
    while nothing is ever mocked.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _reader(self) -> mitm_addon.AutonomRecorder:
        reader = mitm_addon.AutonomRecorder()
        reader.mocks_path = str(mocks.registry_file(self.registry))
        return reader

    def test_the_addon_reads_exactly_what_the_cli_wrote(self) -> None:
        written = mocks.add(url_glob="*/v1/login", method="POST", status=500,
                            body_text='{"error":"boom"}', registry=self.registry)
        rules = self._reader()._load_rules()
        self.assertEqual([rule["id"] for rule in rules], [written["id"]])
        selected = mitm_addon.select_rule(
            rules, "POST", "https://api.example.net/v1/login", "api.example.net")
        self.assertIsNotNone(selected)
        self.assertEqual(selected["response"]["status"], 500)
        self.assertEqual(
            Path(selected["response"]["body_path"]).read_text("utf-8"),
            '{"error":"boom"}',
        )

    def test_a_disabled_rule_is_invisible_to_the_addon(self) -> None:
        written = mocks.add(url_glob="*", registry=self.registry)
        mocks.set_enabled(written["id"], False, registry=self.registry)
        rules = self._reader()._load_rules()
        self.assertIsNone(mitm_addon.select_rule(rules, "GET", "https://a/b", "a"))

    def test_a_mid_run_change_is_picked_up_without_a_restart(self) -> None:
        reader = self._reader()
        self.assertEqual(reader._load_rules(), [])
        mocks.add(url_glob="*/late", registry=self.registry)
        os.utime(mocks.registry_file(self.registry), (0, 0))  # force an mtime change
        self.assertEqual(len(reader._load_rules()), 1)

    def test_the_url_sugar_survives_the_round_trip(self) -> None:
        selector = mocks.url_to_match("https://api.devbackend.net/post/update/12341")
        mocks.add(url_glob=selector["url_glob"], host=selector["host"],
                  ignore_query=selector["ignore_query"], body_text="{}",
                  registry=self.registry)
        rules = self._reader()._load_rules()
        hit = mitm_addon.select_rule(
            rules, "POST",
            "https://api.devbackend.net/post/update/12341?ts=9",
            "api.devbackend.net")
        self.assertIsNotNone(hit)

    def test_proxy_argv_points_the_addon_at_the_registry(self) -> None:
        argv = proxy.build_argv("mitmdump", port=8080, directory=Path("/tmp/n"),
                                confdir=Path("/tmp/n/ca"), capture_bodies=False,
                                mocks_file=mocks.registry_file(self.registry))
        self.assertIn(f"autonom_mocks={mocks.registry_file(self.registry)}", argv)


class MockLoudnessTests(unittest.TestCase):
    """The control that pays for persistence.

    Rules now outlive the session, so the only thing standing between a stale
    rule and a fabricated verdict is that every entry point says it is there.
    If these tests go, the feature becomes a trap.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self, *argv: str) -> dict:
        env = dict(os.environ)
        env["AUTONOM_HOME"] = self.tmp.name
        result = subprocess.run(
            [sys.executable, str(CLI), *argv],
            cwd=self.tmp.name, env=env, text=True, timeout=120,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_doctor_announces_an_enabled_rule_and_falls_silent_when_disabled(self) -> None:
        self._run("network", "mock", "add",
                  "--url", "https://api.devbackend.net/post/update/12341",
                  "--json", '{"ok":true}')

        loud = self._run("doctor")
        self.assertEqual(loud["mocks"]["active"], 1)
        self.assertIn("persistent_mocks_active",
                      [w.get("code") for w in loud.get("warnings", [])])

        self._run("network", "mock", "disable", "--all")
        quiet = self._run("doctor")
        self.assertEqual(quiet["mocks"]["active"], 0)
        self.assertNotIn("persistent_mocks_active",
                         [w.get("code") for w in quiet.get("warnings", [])])

    def test_inline_json_sets_a_content_type_and_survives_a_reread(self) -> None:
        added = self._run("network", "mock", "add",
                          "--url", "https://api.devbackend.net/post/update/12341",
                          "--json", '{"status":"mocked"}', "--note", "demo")
        self.assertEqual(added["mock"]["response"]["headers"]["Content-Type"],
                         "application/json")

        shown = self._run("network", "mock", "show", added["mock"]["id"])
        self.assertEqual(json.loads(shown["body_preview"]), {"status": "mocked"})
        self.assertEqual(shown["mock"]["note"], "demo")
        # The preview is scrubbed and re-serialised; the stored file is what
        # actually gets served, byte for byte.
        stored = Path(shown["mock"]["response"]["body_path"]).read_text("utf-8")
        self.assertEqual(stored, '{"status":"mocked"}')

    def test_a_credential_in_a_mock_body_is_masked_in_the_preview(self) -> None:
        added = self._run("network", "mock", "add", "--match", "*/login",
                          "--json", '{"token":"SENTINEL-abc123"}')
        shown = self._run("network", "mock", "show", added["mock"]["id"])
        self.assertNotIn("SENTINEL-abc123", json.dumps(shown))

    def test_crud_needs_no_session_and_no_device(self) -> None:
        """Rules can be prepared before anything is plugged in."""
        added = self._run("network", "mock", "add", "--match", "*/v1/login",
                          "--status", "500")
        updated = self._run("network", "mock", "update", added["mock"]["id"],
                            "--status", "503")
        self.assertEqual(updated["mock"]["response"]["status"], 503)
        listed = self._run("network", "mock", "list")
        self.assertEqual(listed["count"], 1)
        self._run("network", "mock", "remove", added["mock"]["id"])
        self.assertEqual(self._run("network", "mock", "list")["count"], 0)

    def test_url_and_match_together_are_refused(self) -> None:
        env = dict(os.environ)
        env["AUTONOM_HOME"] = self.tmp.name
        result = subprocess.run(
            [sys.executable, str(CLI), "network", "mock", "add",
             "--url", "https://a/b", "--match", "*/b"],
            cwd=self.tmp.name, env=env, text=True, timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error_code"],
                         errors.CONFLICTING_TARGET_FLAGS)


class MatchingTests(unittest.TestCase):
    """CAP-MOCK-003 — first enabled rule wins; narrowing actually narrows."""

    RULES = [
        {"id": "m_1", "enabled": False,
         "match": {"url_glob": "*/v1/login", "method": None, "host": None}},
        {"id": "m_2", "enabled": True,
         "match": {"url_glob": "*/v1/login", "method": "POST", "host": None}},
        {"id": "m_3", "enabled": True,
         "match": {"url_glob": "*/v1/login", "method": None, "host": None}},
    ]

    def test_disabled_rules_are_skipped_and_first_enabled_wins(self) -> None:
        chosen = mitm_addon.select_rule(self.RULES, "POST", "http://h/v1/login", "h")
        self.assertEqual(chosen["id"], "m_2")

    def test_method_narrows_the_match(self) -> None:
        chosen = mitm_addon.select_rule(self.RULES, "GET", "http://h/v1/login", "h")
        self.assertEqual(chosen["id"], "m_3")

    def test_host_narrows_the_match(self) -> None:
        rules = [{"id": "m_1", "enabled": True,
                  "match": {"url_glob": "*", "method": None, "host": "api.example.com"}}]
        self.assertIsNone(mitm_addon.select_rule(rules, "GET", "http://other/x", "other"))
        self.assertIsNotNone(
            mitm_addon.select_rule(rules, "GET", "http://api.example.com/x", "api.example.com")
        )

    def test_no_rule_matches_an_unrelated_url(self) -> None:
        self.assertIsNone(mitm_addon.select_rule(self.RULES, "POST", "http://h/v2/other", "h"))


class StoreTests(unittest.TestCase):
    """CAP-NET-003 / CAP-NET-005 — filtering, caps, and honest truncation."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.record = make_record(Path(self.tmp.name))
        shutil.copyfile(FLOWS, store.flows_path(self.record))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_fixture_flows_load(self) -> None:
        flows, warnings = store.read_all(self.record)
        self.assertGreaterEqual(len(flows), 3)
        self.assertEqual(warnings, [])

    def test_ids_are_unique_and_ordered(self) -> None:
        flows, _ = store.read_all(self.record)
        ids = [flow["id"] for flow in flows]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, sorted(ids))

    def test_filters_narrow_the_result(self) -> None:
        flows, _ = store.read_all(self.record)
        self.assertTrue(all(f["method"] == "POST"
                            for f in store.filter_flows(flows, method="POST")))
        self.assertTrue(all("login" in f["path"]
                            for f in store.filter_flows(flows, path_glob="*/login")))
        self.assertTrue(all(f["mocked"] for f in store.filter_flows(flows, mocked=True)))

    def test_truncation_is_reported_not_silent(self) -> None:
        payload = store.listing(self.record, max_items=1)
        self.assertEqual(payload["count"], 1)
        self.assertTrue(payload["truncated"])
        self.assertGreater(payload["total_matched"], 1)

    def test_a_partial_final_line_is_skipped_with_a_warning(self) -> None:
        path = store.flows_path(self.record)
        with path.open("a", encoding="utf-8") as handle:
            handle.write('{"id": "f_9999", "method": "GET"')  # interrupted mid-write
        flows, warnings = store.read_all(self.record)
        self.assertNotIn("f_9999", [f["id"] for f in flows])
        self.assertEqual(warnings[0]["code"], "truncated_flow_record")

    def test_unknown_flow_id_is_an_error(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            store.find(self.record, "f_9999")
        self.assertEqual(caught.exception.code, errors.FLOW_NOT_FOUND)

    def test_full_bodies_require_opt_in(self) -> None:
        with self.assertRaises(errors.AutonomError) as caught:
            store.require_bodies(self.record)
        self.assertEqual(caught.exception.code, errors.BODIES_NOT_CAPTURED)


class HarTests(unittest.TestCase):
    """CAP-NET-006 — a HAR a standard viewer opens, honest about its fidelity."""

    def setUp(self) -> None:
        self.flows = [json.loads(line) for line in FLOWS.read_text("utf-8").splitlines() if line]

    def test_structure_matches_har_1_2(self) -> None:
        log = har.build(self.flows)["log"]
        self.assertEqual(log["version"], "1.2")
        self.assertEqual(log["creator"]["name"], "autonom")
        self.assertEqual(len(log["entries"]), len(self.flows))
        for entry in log["entries"]:
            for key in ("startedDateTime", "time", "request", "response", "timings", "cache"):
                self.assertIn(key, entry)
            for key in ("method", "url", "headers", "queryString"):
                self.assertIn(key, entry["request"])
            for key in ("status", "headers", "content"):
                self.assertIn(key, entry["response"])

    def test_redaction_survives_export(self) -> None:
        log = har.build(self.flows)["log"]
        for entry in log["entries"]:
            for header in entry["request"]["headers"]:
                if header["name"] in redact.REDACTED_HEADERS:
                    self.assertEqual(header["value"], redact.PLACEHOLDER)

    def test_preview_only_export_says_so(self) -> None:
        self.assertIn("preview", har.build(self.flows, bodies_captured=False)["log"]["comment"])
        self.assertNotIn("comment", har.build(self.flows, bodies_captured=True)["log"])

    def test_true_size_is_reported_even_when_text_is_a_preview(self) -> None:
        log = har.build(self.flows, bodies_captured=False)["log"]
        for entry, flow in zip(log["entries"], self.flows):
            self.assertEqual(entry["response"]["content"]["size"],
                             flow["sizes"]["response_bytes"])

    def test_mocked_entries_are_tagged(self) -> None:
        log = har.build(self.flows)["log"]
        tagged = [e for e in log["entries"] if e["_autonom"]["mocked"]]
        self.assertEqual(len(tagged), sum(1 for f in self.flows if f["mocked"]))


if __name__ == "__main__":
    unittest.main()


class ConnectivityProbeTests(unittest.TestCase):
    """Intercepting the OS captive-portal probes takes the device offline.

    Found live: system components validate TLS against the *system* CA store
    only, never a user-installed one. Interception therefore fails the probe,
    Android drops VALIDATED from the network, and apps conclude they have no
    internet. The app under test went completely silent nine minutes after
    attach while the proxy itself was working perfectly.
    """

    def test_probe_hosts_are_tunnelled_by_default(self) -> None:
        argv = proxy.build_argv("mitmdump", port=8080, directory=Path("/tmp/n"),
                                confdir=Path("/tmp/n/ca"), capture_bodies=False)
        self.assertIn("--ignore-hosts", argv)
        pattern = argv[argv.index("--ignore-hosts") + 1]
        for host in ("connectivitycheck.gstatic.com", "www.google.com"):
            self.assertIn(host.replace(".", r"\."), pattern)

    def test_the_pattern_is_anchored_so_it_cannot_swallow_a_backend(self) -> None:
        """`www.google.com` must not match `www.google.com.evil.example`, and a
        backend that merely contains a probe host's name must still be captured."""
        import re

        pattern = re.compile(proxy.connectivity_check_pattern())
        self.assertTrue(pattern.match("connectivitycheck.gstatic.com:443"))
        self.assertIsNone(pattern.match("api-core.staging.example.com:443"))
        self.assertIsNone(pattern.match("api.connectivitycheck.gstatic.com:443"))

    def test_interception_can_be_forced_when_the_caller_means_it(self) -> None:
        argv = proxy.build_argv("mitmdump", port=8080, directory=Path("/tmp/n"),
                                confdir=Path("/tmp/n/ca"), capture_bodies=False,
                                intercept_connectivity_checks=True)
        self.assertNotIn("--ignore-hosts", argv)

    def test_an_explicit_pattern_is_added_alongside_the_default(self) -> None:
        argv = proxy.build_argv("mitmdump", port=8080, directory=Path("/tmp/n"),
                                confdir=Path("/tmp/n/ca"), capture_bodies=False,
                                ignore_hosts=r"^cdn\.example\.com:")
        self.assertEqual(argv.count("--ignore-hosts"), 2)


class ProxyApplicationTests(EnvSandboxMixin, unittest.TestCase):
    """A written proxy setting is not an applied one.

    `settings put global http_proxy` only writes a row; ConnectivityService
    reads it at startup through ProxyTracker. Attach therefore reported success
    while not one byte reached the proxy — `settings get` echoed the value back,
    `network status` said attached, and the capture stayed empty. An hour of
    live debugging, because every component was telling the truth about its own
    small part.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.log = Path(self.tmp.name) / "argv.jsonl"
        self.set_env(AUTONOM_FAKE_STATE=str(self.state),
                     AUTONOM_FAKE_LOG=str(self.log))
        self.state.write_text(json.dumps({"settings": {}}), encoding="utf-8")
        self.target = Target("android", "emulator-5554", str(FAKE_ADB),
                             {"serial": "emulator-5554"})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _argv(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        out = []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line).get("argv", []))
            except json.JSONDecodeError:
                continue
        return out

    def test_attach_makes_the_framework_adopt_the_setting(self) -> None:
        record = make_record(Path(self.tmp.name))
        detail = device_proxy_android.attach(self.target, record, port=8080,
                                             acknowledged=True)
        self.assertTrue(detail["setting_applied"]["applied"])
        flat = [" ".join(a) for a in self._argv()]
        self.assertTrue(any("svc wifi disable" in a for a in flat), flat)
        self.assertTrue(any("svc wifi enable" in a for a in flat), flat)
        self.assertNotIn("warnings", detail)

    def test_skipping_the_cycle_is_allowed_but_never_silent(self) -> None:
        record = make_record(Path(self.tmp.name))
        detail = device_proxy_android.attach(self.target, record, port=8080,
                                             acknowledged=True, network_cycle=False)
        self.assertFalse(detail["setting_applied"]["applied"])
        self.assertEqual([w["code"] for w in detail["warnings"]],
                         ["proxy_setting_not_applied"])
        flat = [" ".join(a) for a in self._argv()]
        self.assertFalse(any("svc wifi" in a for a in flat))

    def test_the_proxy_value_is_still_written_either_way(self) -> None:
        record = make_record(Path(self.tmp.name))
        device_proxy_android.attach(self.target, record, port=8080,
                                    acknowledged=True, network_cycle=False)
        settings = json.loads(self.state.read_text())["settings"]
        self.assertEqual(settings["http_proxy"], "10.0.2.2:8080")


class AndroidCaInstallTests(EnvSandboxMixin, unittest.TestCase):
    """Android CA install — scriptable, universal on a rootable image, gated.

    The flag shipped as a no-op on Android while iOS had it, so an agent could
    believe TLS was set up. It is now a real, consent-gated operation mirroring
    iOS `--install-ca` — mechanically the same steps we ran by hand on a live
    emulator, behind the same consent as every privileged change.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.log = Path(self.tmp.name) / "argv.jsonl"
        self.set_env(
            AUTONOM_HOME=self.tmp.name,
            AUTONOM_FAKE_STATE=str(self.state),
            AUTONOM_FAKE_LOG=str(self.log),
        )
        self.state.write_text("{}", encoding="utf-8")
        self.target = Target("android", "emulator-5554", str(FAKE_ADB),
                             {"serial": "emulator-5554"})
        # A real CA in the machine store so ca_certificate() finds one.
        ca = Path(self.tmp.name) / "ca" / "mitmproxy-ca-cert.pem"
        ca.parent.mkdir(parents=True, exist_ok=True)
        self._write_self_signed(ca)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def _write_self_signed(path: Path) -> None:
        import subprocess as sp
        key = path.with_suffix(".key")
        sp.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(key), "-out", str(path), "-days", "1",
                "-subj", "/CN=mitmproxy/O=mitmproxy"],
               capture_output=True, check=True, timeout=30)

    def _argv(self) -> list[list[str]]:
        out = []
        for line in self.log.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line).get("argv", []))
            except json.JSONDecodeError:
                continue
        return out

    def test_install_pushes_the_cert_into_the_user_store_after_root(self) -> None:
        record = make_record(Path(self.tmp.name))
        detail = device_proxy_android.install_ca_certificate(
            self.target, record, acknowledged=True)
        self.assertRegex(detail["installed"],
                         r"^/data/misc/user/0/cacerts-added/[0-9a-f]+\.0$")
        flat = [" ".join(a) for a in self._argv()]
        self.assertLess(next(i for i, a in enumerate(flat) if a.endswith(" root")),
                        next(i for i, a in enumerate(flat) if " push " in a),
                        "root must precede push")
        self.assertTrue(any("cacerts-added" in a and "chmod 644" in a for a in flat))
        self.assertEqual(record["consent_log"][-1]["operation"], "ca_install")

    def test_a_play_image_is_refused_with_a_reason_not_a_wrong_hash(self) -> None:
        self.state.write_text(json.dumps({"root_refused": True}), encoding="utf-8")
        record = make_record(Path(self.tmp.name))
        with self.assertRaises(errors.AutonomError) as caught:
            device_proxy_android.install_ca_certificate(
                self.target, record, acknowledged=True)
        self.assertEqual(caught.exception.code, errors.BACKEND_FAILED)
        self.assertIn("google_apis", caught.exception.hint)

    def test_without_consent_nothing_is_pushed(self) -> None:
        record = make_record(Path(self.tmp.name))
        with self.assertRaises(errors.AutonomError) as caught:
            device_proxy_android.install_ca_certificate(
                self.target, record, acknowledged=False)
        self.assertEqual(caught.exception.code, errors.CONSENT_REQUIRED)
        self.assertFalse(self.log.exists() and any(
            "push" in " ".join(a) for a in self._argv()))
