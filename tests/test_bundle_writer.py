"""Shared bundle writer and the Bundle v2 refactor onto it (tp-013, tp-028)."""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from autonom_lib import contracts, errors, report_bundle, report_model  # noqa: E402
from autonom_lib.bundle_writer import BundleWriter, verify_tree  # noqa: E402
from env_isolation import EnvSandboxMixin  # noqa: E402

FIXED_NOW = "2026-01-02T03:04:05Z"

# Captured from report_bundle.build BEFORE it moved onto BundleWriter (tp-028-S01).
# Regenerate only for an intentional Bundle v2 format change:
# `python3 tests/test_bundle_writer.py --print-golden` prints the current digests.
GOLDEN_FILES = {
    "blobs/sha256/78/785b0751fc2c53dc14a4ce3d800e69ef9ce1009eb327ccf458afe09c242c26c9":
        "785b0751fc2c53dc14a4ce3d800e69ef9ce1009eb327ccf458afe09c242c26c9",
    "blobs/sha256/ad/ad04aca8e83fb18a2af45e0f2840436366ce82a76d0e3f9ba4add352e14a69dd":
        "ad04aca8e83fb18a2af45e0f2840436366ce82a76d0e3f9ba4add352e14a69dd",
    "blobs/sha256/e1/e10365b24a9ebb1a5b833e7d6e5f022b194939471269e37e090cac1041f5379e":
        "e10365b24a9ebb1a5b833e7d6e5f022b194939471269e37e090cac1041f5379e",
    "catalog.json":
        "90e1a00d972dfb3b8f866e008055b1f20f71a6234db4efd02e95f057db057256",
    "finalized.json":
        "4da421047d0902aa8dd6a0276c67e82eb99eaa9511f109d00a37ee1ddc71dcad",
    "flow/login.yaml":
        "1275f41b85b2b9ffdba843b51b0ac0f75521317512c9e93bffc3ff68b5050e47",
    "flow/sub/helper.yaml":
        "11de9d58461bc2caaaa80957e052650ced6a9091d7a8529bb32527b6ae3fd404",
    "integrity.json":
        "3315b2be36b379143c229387c2d9bc79f8ab933e1b03602073186fe7c3ed7469",
    "manifest.json":
        "829cf3c253380db807aa9525bdd25659d55f0ac937ea32d0caf55dec1d830419",
    "model/report.json":
        "a72cd2eea117f37bc22363b27fb8b825ac9449f954c0ed9d90319160019be525",
    "replay.json":
        "b741a15ec76d416464a94e0fc691c86b89b41f36e4fed222c6964e2708d866a8",
    "run.json":
        "3cee1b3f453f61f58f6b70562e16b6b0568a55ef66ca3f2e21b6d8075170a6ca",
    "steps/src_a:1.json":
        "373524bbbb2fa5150d9231449f6b60a5e942e1690f0fb6df801ed6c262f734a8",
    "steps/src_b:2.json":
        "c6ef83c26e108fc74ab3f5883d741d609656699a92eb1cb928c1c5e08e5c83ff",
    "streams/events.ndjson":
        "e10365b24a9ebb1a5b833e7d6e5f022b194939471269e37e090cac1041f5379e",
    "summary.json":
        "8df0879349654e9683d1f7713c14c3b06e53c117afe48f1c9b35e58a38fc8df5",
}
GOLDEN_MODEL_SHA256 = "42ee62f3b904e184335a814d9f5d7ebafeb54f6b885c940dbb790385c1ae7028"


def golden_fixture(root: Path) -> dict:
    """Deterministic flow run used for the Bundle v2 golden digests.

    The fixture holds a nested flow workspace (two sources), one present and
    one missing attachment, an events stream and two steps, so every part of
    ``report_bundle.build`` writes something. Paths inside the manifest are
    absolute (as in real runs); the bundle rewrites them, so the digests do
    not depend on where the temporary directory lives.
    """
    flows = root / "workspace"
    (flows / "sub").mkdir(parents=True, exist_ok=True)
    (flows / "login.yaml").write_text(
        "schema: autonom.dev/flow/v1\nappId: com.example\nname: Login\n"
        "id: login-v1\n---\n- launchApp\n- runFlow: sub/helper.yaml\n",
        encoding="utf-8")
    (flows / "sub" / "helper.yaml").write_text(
        "schema: autonom.dev/flow/v1\nappId: com.example\n---\n- tapOn: Next\n",
        encoding="utf-8")
    run_id = "fr_golden"
    run_dir = root / "flows" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "events.ndjson").write_bytes(
        b'{"event":"run.start","seq":1}\n{"event":"run.end","seq":2}\n')
    (run_dir / "step-1-logs.txt").write_bytes(b"ready\nline two\n")
    (run_dir / "step-2-shot.png").write_bytes(bytes(range(256)) * 4)
    return {
        "schema_version": 3, "session_id": "s_golden", "run_id": run_id,
        "attempt_id": "attempt_golden", "flow_id": "login-v1",
        "flow_name": "Login", "flow_path": str(flows / "login.yaml"),
        "flow_sources": [
            {"path": str(flows / "login.yaml"), "relative": "login.yaml"},
            {"path": str(flows / "sub" / "helper.yaml"),
             "relative": "sub/helper.yaml"},
        ],
        "app_id": "com.example", "platform": "android",
        "target_id": "emulator-5554", "status": "failed",
        "execution_status": "failed", "proof_verdict": "fail",
        "primary_error": {"step_index": 2, "failure_class": "test_failure",
                          "error_code": "flow_assertion_timeout",
                          "error": "not visible"},
        "started_at_ms": 1000, "finished_at_ms": 1300,
        "properties": {"tenant": "demo", "password": "must-not-be-history"},
        "history_parameters": ["tenant"], "tags": ["smoke", "golden"],
        "steps": [
            {"index": 1, "step_id": "src_a:1", "source_id": "src_a",
             "command": "launchApp", "status": "passed",
             "started_at_ms": 1000, "finished_at_ms": 1100, "duration_ms": 100},
            {"index": 2, "step_id": "src_b:2", "source_id": "src_b",
             "command": "assertVisible", "status": "failed",
             "started_at_ms": 1100, "finished_at_ms": 1300, "duration_ms": 200,
             "failure_class": "test_failure",
             "error_code": "flow_assertion_timeout", "error": "not visible"},
        ],
        "artifacts": [f"flows/{run_id}/events.ndjson",
                      f"flows/{run_id}/step-1-logs.txt",
                      f"flows/{run_id}/step-2-shot.png",
                      f"flows/{run_id}/gone.txt"],
        "artifact_steps": [
            {"path": f"flows/{run_id}/step-1-logs.txt", "kind": "logs-after",
             "step_index": 1, "step_id": "src_a:1"},
            {"path": f"flows/{run_id}/step-2-shot.png", "kind": "screenshot",
             "step_index": 2, "step_id": "src_b:2"},
        ],
        "environment": {"platform": "android"},
        "setup": {"available": ["profile"], "selected": [{"kind": "profile"}]},
        "reproduction": f"autonom flow run {flows / 'login.yaml'}",
    }


def tree_digests(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): contracts.sha256_file(path)
            for path in sorted(root.rglob("*")) if path.is_file()}


def build_golden(root: Path) -> tuple[dict, Path]:
    manifest = golden_fixture(root)
    out = root / "bundle"
    with mock.patch.object(report_model, "utc_now", return_value=FIXED_NOW):
        result = report_bundle.build(manifest, artifacts_root=root, out=out)
    return result, out


def staging_leftovers(out: Path) -> list[str]:
    return sorted(name for name in os.listdir(out.parent)
                  if name.startswith(f".{out.name}.staging-"))


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class WriterCase(EnvSandboxMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.home = self.sandbox_home()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.out = self.root / "out" / "bundle"

    def source(self, name: str, data: bytes) -> Path:
        path = self.root / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def assertRefused(self, code: str, call, *args, **kwargs) -> errors.AutonomError:
        with self.assertRaises(errors.AutonomError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        payload = caught.exception.as_dict()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error_code"], code)
        self.assertTrue(payload["error"])
        return caught.exception


class BundleWriterTests(WriterCase):
    def build_sample(self, out: Path | None = None, *, text: str = "hello\n") -> dict:
        out = out or self.out
        with BundleWriter(out) as writer:
            writer.write_json("a/data.json", {"b": 2, "a": 1})
            writer.write_text("notes.md", text)
            blob = writer.add_blob(self.source("shot.png", b"\x89PNG-bytes"))
            again = writer.add_blob(self.source("copy.png", b"\x89PNG-bytes"))
            self.assertEqual(blob, again)
            result = writer.finalize({"kind": "sample/v1"},
                                     finalized_fields={"extra": "x"})
        return {"blob": blob, **result}

    def test_finalize_lists_files_seals_and_publishes_atomically(self) -> None:
        result = self.build_sample()
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["files"], 3)
        self.assertEqual(result["path"], str(self.out))
        self.assertEqual(staging_leftovers(self.out), [])
        integrity = json.loads((self.out / "integrity.json").read_text("utf-8"))
        listed = [item["path"] for item in integrity["files"]]
        self.assertEqual(listed, ["a/data.json", result["blob"]["path"], "notes.md"])
        self.assertNotIn("manifest.json", listed)
        self.assertNotIn("finalized.json", listed)
        self.assertEqual((self.out / "a/data.json").read_bytes(), b'{"a":1,"b":2}\n')
        self.assertEqual(json.loads((self.out / "manifest.json").read_text("utf-8")),
                         {"kind": "sample/v1"})
        finalized = json.loads((self.out / "finalized.json").read_text("utf-8"))
        self.assertEqual(finalized, {
            "schema": "autonom.finalized/v1", "extra": "x",
            "integrity_sha256": contracts.sha256_file(self.out / "integrity.json")})
        self.assertEqual(result["integrity_sha256"], finalized["integrity_sha256"])
        blob = result["blob"]
        self.assertEqual(blob["sha256"], contracts.sha256_bytes(b"\x89PNG-bytes"))
        self.assertEqual(blob["path"], f"blobs/sha256/{blob['sha256'][:2]}/{blob['sha256']}")
        self.assertEqual(blob["size"], len(b"\x89PNG-bytes"))
        for path in self.out.rglob("*"):
            self.assertEqual(mode(path), 0o700 if path.is_dir() else 0o600, path)
        self.assertEqual(mode(self.out), 0o700)
        self.assertEqual(verify_tree(self.out), [])

    def test_finalize_without_descriptor_writes_no_manifest(self) -> None:
        with BundleWriter(self.out) as writer:
            writer.write_json("manifest.json", {"kind": "pack/v1"})
            writer.finalize(None)
        listed = [item["path"] for item in json.loads(
            (self.out / "integrity.json").read_text("utf-8"))["files"]]
        self.assertEqual(listed, ["manifest.json"])
        self.assertEqual(verify_tree(self.out), [])

    def test_exception_inside_block_removes_staging(self) -> None:
        with self.assertRaises(RuntimeError):
            with BundleWriter(self.out) as writer:
                writer.write_text("x.txt", "x")
                staging = writer.staging
                self.assertTrue(staging.is_dir())
                raise RuntimeError("boom")
        self.assertFalse(staging.exists())
        self.assertFalse(self.out.exists())
        self.assertEqual(staging_leftovers(self.out), [])

    def test_failure_during_finalize_removes_staging_and_keeps_out_absent(self) -> None:
        writer = BundleWriter(self.out)
        writer.write_text("x.txt", "x")
        with mock.patch.object(Path, "replace", side_effect=OSError("rename failed")):
            with self.assertRaises(OSError):
                writer.finalize({"kind": "k"})
        self.assertFalse(writer.staging.exists())
        self.assertFalse(self.out.exists())
        self.assertRefused(errors.REPORT_BUNDLE_FINALIZED, writer.write_text, "y", "y")

    def test_refuses_non_empty_out_before_staging(self) -> None:
        self.out.mkdir(parents=True)
        (self.out / "keep.txt").write_text("user data", encoding="utf-8")
        error = self.assertRefused(errors.REPORT_BUNDLE_FINALIZED, BundleWriter, self.out)
        self.assertEqual(error.message,
                         "output directory is non-empty and not an Autonom finalized bundle")
        self.assertEqual(error.hint, "Choose an empty output directory.")
        self.assertEqual(error.extra, {"bundle": str(self.out)})
        self.assertEqual(staging_leftovers(self.out), [])
        self.assertEqual((self.out / "keep.txt").read_text("utf-8"), "user data")

    def test_refuses_out_that_is_a_file(self) -> None:
        self.out.parent.mkdir(parents=True)
        self.out.write_text("file", encoding="utf-8")
        self.assertRefused(errors.REPORT_BUNDLE_FINALIZED, BundleWriter, self.out)
        self.assertEqual(staging_leftovers(self.out), [])

    def test_empty_out_directory_is_replaced(self) -> None:
        self.out.mkdir(parents=True)
        self.build_sample()
        self.assertEqual(verify_tree(self.out), [])

    def test_out_filled_after_start_is_refused_at_finalize(self) -> None:
        writer = BundleWriter(self.out)
        writer.write_text("x.txt", "x")
        self.out.mkdir(parents=True)
        (self.out / "late.txt").write_text("late", encoding="utf-8")
        self.assertRefused(errors.REPORT_BUNDLE_FINALIZED, writer.finalize, {"k": 1})
        self.assertFalse(writer.staging.exists())
        self.assertEqual(sorted(os.listdir(self.out)), ["late.txt"])

    def test_idempotent_finalize_rewrites_nothing(self) -> None:
        first = self.build_sample()
        before = {path: path.stat().st_mtime_ns for path in self.out.rglob("*")}
        second = self.build_sample()
        self.assertTrue(second["idempotent"])
        self.assertEqual(second["integrity_sha256"], first["integrity_sha256"])
        self.assertEqual(second["files"], first["files"])
        after = {path: path.stat().st_mtime_ns for path in self.out.rglob("*")}
        self.assertEqual(before, after)
        self.assertEqual(staging_leftovers(self.out), [])

    def test_finalize_with_other_content_is_refused(self) -> None:
        self.build_sample()
        before = tree_digests(self.out)
        error = self.assertRefused(errors.REPORT_BUNDLE_FINALIZED,
                                   self.build_sample, text="changed\n")
        self.assertEqual(error.hint,
                         "Choose a new output directory; finalized raw evidence is immutable.")
        self.assertEqual(error.extra, {"bundle": str(self.out)})
        self.assertEqual(tree_digests(self.out), before)
        self.assertEqual(staging_leftovers(self.out), [])

    def test_other_descriptor_alone_is_refused(self) -> None:
        with BundleWriter(self.out) as writer:
            writer.write_text("x.txt", "x")
            writer.finalize({"kind": "one"})
        with BundleWriter(self.out) as writer:
            writer.write_text("x.txt", "x")
            self.assertRefused(errors.REPORT_BUNDLE_FINALIZED,
                               writer.finalize, {"kind": "two"})
        self.assertEqual(json.loads((self.out / "manifest.json").read_text("utf-8")),
                         {"kind": "one"})

    def test_closed_writer_refuses_every_operation(self) -> None:
        writer = BundleWriter(self.out)
        writer.finalize({"k": 1})
        blob_source = self.source("b.bin", b"b")
        for call, args in ((writer.write_text, ("a.txt", "a")),
                           (writer.write_json, ("a.json", {})),
                           (writer.add_blob, (blob_source,)),
                           (writer.copy_file, (blob_source, "c.bin")),
                           (writer.copy_tree, (self.root / "src", "tree")),
                           (writer.ensure_dir, ("d",)),
                           (writer.finalize, ({"k": 1},))):
            error = self.assertRefused(errors.REPORT_BUNDLE_FINALIZED, call, *args)
            self.assertEqual(error.message, "bundle writer is already finalized or aborted")
            self.assertTrue(error.hint)
        aborted = BundleWriter(self.root / "other")
        aborted.abort()
        aborted.abort()
        self.assertFalse(aborted.staging.exists())
        self.assertRefused(errors.REPORT_BUNDLE_FINALIZED, aborted.write_text, "a", "a")

    def test_reserved_and_escaping_paths_are_refused(self) -> None:
        with BundleWriter(self.out) as writer:
            for name in ("integrity.json", "finalized.json", ".", ""):
                error = self.assertRefused(errors.PATH_FORBIDDEN,
                                           writer.write_json, name, {})
                self.assertTrue(error.hint)
            for name in ("../escape.json", "/abs.json", "a/../../b.json"):
                error = self.assertRefused(errors.PATH_FORBIDDEN,
                                           writer.write_text, name, "x")
                self.assertEqual(error.extra, {"path": name})
            self.assertFalse((self.out.parent / "escape.json").exists())
            writer.abort()

    def test_descriptor_written_twice_is_refused(self) -> None:
        writer = BundleWriter(self.out)
        writer.write_json("manifest.json", {"kind": "own"})
        error = self.assertRefused(errors.PATH_FORBIDDEN, writer.finalize, {"kind": "x"})
        self.assertEqual(error.extra, {"path": "manifest.json"})
        self.assertTrue(error.hint)
        self.assertFalse(writer.staging.exists())
        self.assertFalse(self.out.exists())

    def test_invalid_sources_are_refused(self) -> None:
        real = self.source("real.bin", b"real")
        link = self.root / "src" / "link.bin"
        link.symlink_to(real)
        with BundleWriter(self.out) as writer:
            for call, args in ((writer.add_blob, (self.root / "missing.bin",)),
                               (writer.add_blob, (self.root / "src",)),
                               (writer.add_blob, (link,)),
                               (writer.copy_file, (self.root / "missing.bin", "m")),
                               (writer.copy_file, (link, "l")),
                               (writer.copy_tree, (real, "t")),
                               (writer.copy_tree, (self.root / "nope", "t"))):
                error = self.assertRefused(errors.INVALID_VALUE, call, *args)
                self.assertTrue(error.hint)
                self.assertIn("path", error.extra)
            writer.abort()

    def test_copy_tree_copies_nested_bundle_and_keeps_it_verifiable(self) -> None:
        nested_root = self.root / "run"
        manifest = golden_fixture(nested_root)
        nested = nested_root / "bundle"
        report_bundle.build(manifest, artifacts_root=nested_root, out=nested)
        (nested / "deep" / "empty").mkdir(parents=True)
        with BundleWriter(self.out) as writer:
            copied = writer.copy_tree(nested, "runs/fr_golden")
            writer.write_json("summary.json", {"runs": 1})
            writer.finalize({"kind": "pack"})
        nested_files = [p for p in nested.rglob("*") if p.is_file()]
        self.assertEqual(copied, {"path": "runs/fr_golden", "files": len(nested_files)})
        copy = self.out / "runs" / "fr_golden"
        self.assertTrue((copy / "deep" / "empty").is_dir())
        self.assertEqual(tree_digests(copy), tree_digests(nested))
        self.assertTrue(report_bundle.verify(copy)["ok"])
        listed = {item["path"] for item in json.loads(
            (self.out / "integrity.json").read_text("utf-8"))["files"]}
        self.assertIn("runs/fr_golden/finalized.json", listed)
        self.assertIn("runs/fr_golden/manifest.json", listed)
        self.assertEqual(verify_tree(self.out), [])
        for path in copy.rglob("*"):
            self.assertEqual(mode(path), 0o700 if path.is_dir() else 0o600, path)

    def test_copy_tree_refuses_symlink_inside(self) -> None:
        tree = self.root / "tree"
        tree.mkdir()
        (tree / "ok.txt").write_text("ok", encoding="utf-8")
        (tree / "link.txt").symlink_to(self.root / "outside.txt")
        with BundleWriter(self.out) as writer:
            error = self.assertRefused(errors.PATH_FORBIDDEN, writer.copy_tree, tree, "t")
            self.assertTrue(error.hint)
            self.assertFalse((writer.staging / "t").exists())
            writer.abort()

    def test_annotations_stay_outside_integrity(self) -> None:
        with BundleWriter(self.out) as writer:
            writer.write_text("evidence.txt", "e")
            writer.write_json("annotations/early.json", {"n": 1})
            writer.write_json("annotationsx/kept.json", {"n": 2})
            writer.finalize({"kind": "k"})
        listed = [item["path"] for item in json.loads(
            (self.out / "integrity.json").read_text("utf-8"))["files"]]
        self.assertEqual(listed, ["annotationsx/kept.json", "evidence.txt"])
        (self.out / "annotations" / "review-1.json").write_text("{}", encoding="utf-8")
        (self.out / "annotations" / "early.json").write_text("changed", encoding="utf-8")
        self.assertEqual(verify_tree(self.out), [])


class VerifyTreeTests(WriterCase):
    def setUp(self) -> None:
        super().setUp()
        with BundleWriter(self.out) as writer:
            writer.write_text("a.txt", "alpha")
            writer.write_text("dir/b.txt", "beta")
            writer.finalize({"kind": "k"})

    def rewrite_integrity(self, value) -> None:
        integrity = self.out / "integrity.json"
        integrity.write_bytes(contracts.canonical_json(value) + b"\n")
        finalized = self.out / "finalized.json"
        record = json.loads(finalized.read_text("utf-8"))
        record["integrity_sha256"] = contracts.sha256_file(integrity)
        finalized.write_bytes(contracts.canonical_json(record))

    def test_changed_byte_is_named(self) -> None:
        (self.out / "dir/b.txt").write_text("bets", encoding="utf-8")
        self.assertEqual(verify_tree(self.out),
                         [{"path": "dir/b.txt", "reason": "digest-mismatch"}])

    def test_missing_file_is_named(self) -> None:
        (self.out / "a.txt").unlink()
        self.assertEqual(verify_tree(self.out), [{"path": "a.txt", "reason": "missing"}])

    def test_extra_unlisted_file_is_ignored_as_today(self) -> None:
        (self.out / "extra.txt").write_text("x", encoding="utf-8")
        self.assertEqual(verify_tree(self.out), [])

    def test_integrity_missing_or_unreadable(self) -> None:
        (self.out / "integrity.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(verify_tree(self.out),
                         [{"path": "integrity.json", "reason": "unreadable"}])
        (self.out / "integrity.json").write_text("[1]", encoding="utf-8")
        self.assertEqual(verify_tree(self.out),
                         [{"path": "integrity.json", "reason": "unreadable"}])
        (self.out / "integrity.json").unlink()
        self.assertEqual(verify_tree(self.out),
                         [{"path": "integrity.json", "reason": "missing"}])

    def test_finalization_record_problems(self) -> None:
        integrity = self.out / "integrity.json"
        integrity.write_bytes(integrity.read_bytes() + b" ")
        self.assertEqual(verify_tree(self.out),
                         [{"path": "integrity.json", "reason": "finalization-mismatch"}])
        (self.out / "finalized.json").write_text("nope", encoding="utf-8")
        self.assertEqual(verify_tree(self.out),
                         [{"path": "finalized.json", "reason": "unreadable"}])
        (self.out / "finalized.json").unlink()
        self.assertEqual(verify_tree(self.out),
                         [{"path": "finalized.json", "reason": "missing"}])

    def test_malformed_entries_are_failures(self) -> None:
        self.rewrite_integrity({"algorithm": "sha256",
                                "files": ["bare", {"path": 3}, {"path": "a.txt"}]})
        self.assertEqual(verify_tree(self.out), [
            {"path": "bare", "reason": "malformed-entry"},
            {"path": "3", "reason": "malformed-entry"},
            {"path": "a.txt", "reason": "malformed-entry"},
        ])

    def test_escaping_listed_path_raises_path_forbidden(self) -> None:
        self.rewrite_integrity({"algorithm": "sha256", "files": [
            {"path": "../outside.txt", "sha256": "0" * 64, "size": 1}]})
        error = self.assertRefused(errors.PATH_FORBIDDEN, verify_tree, self.out)
        self.assertEqual(error.message, "artifact path escapes the session evidence root")


class ReportBundleOnWriterTests(WriterCase):
    """tp-028: Bundle v2 output and errors are unchanged by the refactor."""

    def test_tp028_s01_golden_digests_equal_pre_refactor_output(self) -> None:
        result, out = build_golden(self.root)
        self.assertEqual(tree_digests(out), GOLDEN_FILES)
        self.assertEqual(result["model_sha256"], GOLDEN_MODEL_SHA256)
        self.assertEqual(result["attachments"], 4)
        self.assertFalse(result["idempotent"])
        finalized = json.loads((out / "finalized.json").read_text("utf-8"))
        self.assertEqual(finalized["integrity_sha256"], GOLDEN_FILES["integrity.json"])
        self.assertEqual(finalized["model_sha256"], GOLDEN_MODEL_SHA256)
        self.assertTrue((out / "flow" / ".autonom").is_dir())
        verified = report_bundle.verify(out)
        self.assertEqual(verified, {"ok": True, "bundle": str(out), "files": 13,
                                    "run_id": "fr_golden"})
        self.assertEqual(verify_tree(out), [])
        self.assertNotIn(b"must-not-be-history", (out / "run.json").read_bytes())
        self.assertEqual(staging_leftovers(out), [])

    def test_rebuild_is_idempotent_and_other_run_is_refused(self) -> None:
        result, out = build_golden(self.root)
        again = report_bundle.build(golden_fixture(self.root), artifacts_root=self.root,
                                    out=out)
        self.assertEqual(again, {"bundle": str(out), "finalized": True,
                                 "idempotent": True,
                                 "model_sha256": result["model_sha256"]})
        other = golden_fixture(self.root)
        other["run_id"] = "fr_other"
        error = self.assertRefused(errors.REPORT_BUNDLE_FINALIZED, report_bundle.build,
                                   other, artifacts_root=self.root, out=out)
        self.assertEqual(error.message, "report bundle is finalized and differs from this run")
        self.assertEqual(tree_digests(out), GOLDEN_FILES)

    def test_non_empty_out_keeps_message(self) -> None:
        out = self.root / "busy"
        out.mkdir()
        (out / "x").write_text("x", encoding="utf-8")
        error = self.assertRefused(errors.REPORT_BUNDLE_FINALIZED, report_bundle.build,
                                   golden_fixture(self.root), artifacts_root=self.root,
                                   out=out)
        self.assertEqual(error.message,
                         "output directory is non-empty and not an Autonom finalized bundle")
        self.assertEqual(error.hint, "Choose an empty output directory.")
        self.assertEqual(staging_leftovers(out), [])

    def test_failure_mid_build_leaves_no_staging_and_no_out(self) -> None:
        manifest = golden_fixture(self.root)
        manifest["steps"][0]["step_id"] = "../escape"
        out = self.root / "bundle"
        with mock.patch.object(report_model, "utc_now", return_value=FIXED_NOW):
            self.assertRefused(errors.PATH_FORBIDDEN, report_bundle.build, manifest,
                               artifacts_root=self.root, out=out)
        self.assertFalse(out.exists())
        self.assertFalse((self.root / "escape.json").exists())
        self.assertEqual(staging_leftovers(out), [])

    def test_verify_names_changed_and_missing_files(self) -> None:
        _result, out = build_golden(self.root)
        (out / "streams/events.ndjson").write_bytes(b"tampered\n")
        (out / "catalog.json").unlink()
        error = self.assertRefused(errors.REPORT_INTEGRITY_FAILED, report_bundle.verify, out)
        self.assertEqual(error.message, "report bundle integrity verification failed")
        self.assertEqual(error.extra["failures"], [
            {"path": "catalog.json", "reason": "missing"},
            {"path": "streams/events.ndjson", "reason": "digest-mismatch"},
        ])

    def test_verify_keeps_finalization_messages(self) -> None:
        _result, out = build_golden(self.root)
        integrity = out / "integrity.json"
        original = integrity.read_bytes()
        integrity.write_bytes(original + b" ")
        error = self.assertRefused(errors.REPORT_INTEGRITY_FAILED, report_bundle.verify, out)
        self.assertEqual(error.message,
                         "bundle integrity catalog digest does not match finalization record")
        integrity.write_bytes(original)
        (out / "finalized.json").unlink()
        error = self.assertRefused(errors.REPORT_INTEGRITY_FAILED, report_bundle.verify, out)
        self.assertEqual(error.message,
                         "bundle is missing its integrity or finalization record")

    def test_annotation_keeps_bundle_verifiable(self) -> None:
        _result, out = build_golden(self.root)
        note = report_bundle.annotate(out, "looked fine", author="qa")
        self.assertTrue(Path(note["path"]).is_file())
        self.assertTrue(report_bundle.verify(out)["ok"])
        self.assertEqual(verify_tree(out), [])


if __name__ == "__main__":
    if sys.argv[1:] == ["--print-golden"]:
        with tempfile.TemporaryDirectory() as name:
            result, out = build_golden(Path(name))
            print(json.dumps({"result": {k: v for k, v in result.items() if k != "bundle"},
                              "files": tree_digests(out)}, indent=1, sort_keys=True))
    else:
        unittest.main()
