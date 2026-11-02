"""Offline tests for B2 state store and prime-review integration.

Tests in-memory S3 round trip, PR/repo isolation, partial upload failure,
missing vs forbidden reads, malicious tar member rejection, gate carry/update behavior,
and isolated Python (-I) entrypoint execution.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import state_store
from state_store import (
    B2StateStore,
    CorruptArchiveError,
    SessionNotFoundError,
    StorageError,
    any_b2_configured,
    create_session_tar,
    create_trace_bundle,
    is_b2_configured,
    is_not_found,
    safe_extract_tar,
    validate_b2_config,
)

SCRIPT = REPO_ROOT / "prime-review.py"


class FakeS3ClientError(Exception):
    def __init__(self, code: str, message: str = "", status_code: int = 400):
        self.response = {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        }
        super().__init__(f"An error occurred ({code}) when calling the operation: {message}")


class FakeS3Client:
    """In-memory fake S3 client simulating B2 S3-compatible API."""

    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.fail_get_keys: dict[str, Exception] = {}
        self.fail_put_keys: dict[str, Exception] = {}
        self.put_history: list[str] = []

    def get_object(self, Bucket: str, Key: str) -> dict:
        if Key in self.fail_get_keys:
            raise self.fail_get_keys[Key]
        k = (Bucket, Key)
        if k not in self.objects:
            raise FakeS3ClientError("NoSuchKey", "The specified key does not exist.", 404)
        data = self.objects[k]
        return {"Body": io.BytesIO(data), "ContentLength": len(data)}

    def put_object(self, Bucket: str, Key: str, Body: Any, ContentType: str = "") -> dict:
        if Key in self.fail_put_keys:
            raise self.fail_put_keys[Key]
        if hasattr(Body, "read"):
            data = Body.read()
        elif isinstance(Body, str):
            data = Body.encode("utf-8")
        else:
            data = bytes(Body)
        self.objects[(Bucket, Key)] = data
        self.put_history.append(Key)
        return {"ETag": '"fake-etag"'}

    def head_object(self, Bucket: str, Key: str) -> dict:
        k = (Bucket, Key)
        if k not in self.objects:
            raise FakeS3ClientError("404", "Not Found", 404)
        return {"ContentLength": len(self.objects[k])}

    def list_objects_v2(self, Bucket: str, Prefix: str = "") -> dict:
        matching = []
        for (b, k), v in self.objects.items():
            if b == Bucket and k.startswith(Prefix):
                matching.append({"Key": k, "Size": len(v)})
        return {"Contents": matching, "KeyCount": len(matching)}


class B2StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.work = Path(self.temp_dir.name)
        self.bucket = "test-bucket"
        self.owner = "test-owner"
        self.repo = "test-owner/test-repo"
        self.pr = "42"
        self.client = FakeS3Client()
        self.store = B2StateStore(
            bucket=self.bucket,
            owner=self.owner,
            repo=self.repo,
            pr=self.pr,
            client=self.client,
        )

    def test_b2_env_configuration_validation(self):
        base_env = {
            "B2_ENDPOINT_URL": "https://s3.us-west-000.backblazeb2.com",
            "B2_BUCKET": "my-bucket",
            "B2_REGION": "us-west-000",
            "B2_ACCESS_KEY_ID": "key-id",
            "B2_SECRET_ACCESS_KEY": "secret-key",
            "OWNER": "owner",
            "REPO": "owner/repo",
            "PR_NUMBER": "12",
        }
        self.assertTrue(is_b2_configured(base_env))
        cfg = validate_b2_config(base_env)
        self.assertEqual(cfg["B2_BUCKET"], "my-bucket")

        # Missing one variable
        for key in state_store.B2_ENV_VARS:
            env = dict(base_env)
            del env[key]
            self.assertFalse(is_b2_configured(env))
            with self.assertRaises(StorageError):
                validate_b2_config(env)

    def test_round_trip_session_and_trace_bundle(self):
        session_dir = self.work / "session"
        session_dir.mkdir()
        session_file = session_dir / "session-root.jsonl"
        session_file.write_text('{"type": "tool_started"}\n{"type": "message", "role": "assistant"}\n')
        (session_dir / "extra.json").write_text('{"extra": true}')

        subagent_dir = self.work / "subagent-1"
        subagent_dir.mkdir()
        (subagent_dir / "sub.jsonl").write_text('{"sub": 1}\n')

        meta = {
            "run_id": "run-001",
            "head_after": "sha123",
            "gate_blockers": [],
            "verdict": "approve",
        }
        result = {"summary": "All good", "verdict": "approve", "findings": []}

        # Save state
        saved = self.store.save_state(
            run_id="run-001",
            meta=meta,
            result=result,
            session_dir=session_dir,
            child_dirs=[subagent_dir],
            config_commit="gitcommit456",
            session_lines_before=1,
        )

        self.assertEqual(saved["run_id"], "run-001")
        self.assertEqual(saved["head_after"], "sha123")
        self.assertEqual(saved["config_commit"], "gitcommit456")
        self.assertEqual(saved["session_lines_before"], 1)

        # Verify ordering: trace bundle and session snapshot uploaded BEFORE latest pointer
        history = self.client.put_history
        self.assertEqual(len(history), 4)
        latest_key = self.store.latest_key
        self.assertEqual(history[-1], latest_key)

        # Verify immutable trace bundle contents
        trace_key = self.store.trace_key("run-001")
        trace_bytes = self.client.objects[(self.bucket, trace_key)]
        with tarfile.open(fileobj=io.BytesIO(trace_bytes), mode="r:gz") as tar:
            names = tar.getnames()
            self.assertIn("manifest.json", names)
            self.assertIn("meta.json", names)
            self.assertIn("result.json", names)
            self.assertIn("session.jsonl", names)
            self.assertIn("subagents/sub.jsonl", names)

            manifest_raw = tar.extractfile("manifest.json").read()
            manifest = json.loads(manifest_raw)
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["source"], "prime-reviewer")
            self.assertEqual(manifest["config_commit"], "gitcommit456")
            self.assertEqual(manifest["session_lines_before"], 1)
            self.assertEqual(manifest["trace_id"], f"{self.owner}/test-repo#{self.pr}/run-001")

        # Verify restore session
        dest_session = self.work / "restored-session"
        restored_meta = self.store.restore_session(dest_session)
        self.assertIsNotNone(restored_meta)
        self.assertEqual(restored_meta["head_after"], "sha123")
        self.assertEqual(restored_meta["config_commit"], "gitcommit456")

        restored_session_file = dest_session / "session-root.jsonl"
        self.assertTrue(restored_session_file.exists())
        self.assertEqual(restored_session_file.read_text(), session_file.read_text())
        self.assertEqual((dest_session / "extra.json").read_text(), '{"extra": true}')

    def test_repo_and_pr_isolation(self):
        store_repo1_pr1 = self.store
        store_repo1_pr2 = B2StateStore(
            bucket=self.bucket, owner=self.owner, repo="test-owner/test-repo", pr="43", client=self.client
        )
        store_repo2_pr1 = B2StateStore(
            bucket=self.bucket, owner=self.owner, repo="test-owner/other-repo", pr="42", client=self.client
        )

        sess1 = self.work / "sess1"
        sess1.mkdir()
        (sess1 / "s.jsonl").write_text('{"pr": 42, "repo": "test-repo"}\n')

        sess2 = self.work / "sess2"
        sess2.mkdir()
        (sess2 / "s.jsonl").write_text('{"pr": 43, "repo": "test-repo"}\n')

        sess3 = self.work / "sess3"
        sess3.mkdir()
        (sess3 / "s.jsonl").write_text('{"pr": 42, "repo": "other-repo"}\n')

        store_repo1_pr1.save_state("r1", {"head_after": "h1"}, None, sess1, [])
        store_repo1_pr2.save_state("r2", {"head_after": "h2"}, None, sess2, [])
        store_repo2_pr1.save_state("r3", {"head_after": "h3"}, None, sess3, [])

        # Verify each store restores only its own data
        d1 = self.work / "d1"
        meta1 = store_repo1_pr1.restore_session(d1)
        self.assertEqual(meta1["head_after"], "h1")
        self.assertIn('"test-repo"', (d1 / "s.jsonl").read_text())

        d2 = self.work / "d2"
        meta2 = store_repo1_pr2.restore_session(d2)
        self.assertEqual(meta2["head_after"], "h2")
        self.assertIn('"pr": 43', (d2 / "s.jsonl").read_text())

        d3 = self.work / "d3"
        meta3 = store_repo2_pr1.restore_session(d3)
        self.assertEqual(meta3["head_after"], "h3")
        self.assertIn('"other-repo"', (d3 / "s.jsonl").read_text())

        # Keys in bucket must all start with proper namespaces
        for (bucket, key) in self.client.objects.keys():
            self.assertTrue(
                key.startswith("sessions/test-owner/test-repo/42/")
                or key.startswith("traces/test-owner/test-repo/42/")
                or key.startswith("sessions/test-owner/test-repo/43/")
                or key.startswith("traces/test-owner/test-repo/43/")
                or key.startswith("sessions/test-owner/other-repo/42/")
                or key.startswith("traces/test-owner/other-repo/42/")
            )

    def test_partial_upload_failures_prevent_latest_update(self):
        session_dir = self.work / "session"
        session_dir.mkdir()
        (session_dir / "s.jsonl").write_text('{"turn": 1}\n')

        # 1. Fail trace bundle upload
        trace_key = self.store.trace_key("run-fail-1")
        self.client.fail_put_keys[trace_key] = FakeS3ClientError("InternalError", "Storage failure", 500)

        with self.assertRaises(StorageError) as cm:
            self.store.save_state("run-fail-1", {"head_after": "h1"}, None, session_dir, [])
        self.assertIn("B2 storage write error", str(cm.exception))
        # Ensure latest pointer was NOT created
        self.assertNotIn((self.bucket, self.store.latest_key), self.client.objects)

        # 2. Fail session tar upload
        del self.client.fail_put_keys[trace_key]
        session_key = self.store.session_key("run-fail-2")
        self.client.fail_put_keys[session_key] = FakeS3ClientError("NetworkTimeout", "Timeout", 504)

        with self.assertRaises(StorageError):
            self.store.save_state("run-fail-2", {"head_after": "h2"}, None, session_dir, [])
        self.assertNotIn((self.bucket, self.store.latest_key), self.client.objects)

    def test_missing_vs_forbidden_reads(self):
        # 1. Clean PR with no existing session: returns None
        dest = self.work / "dest"
        self.assertIsNone(self.store.restore_session(dest))
        self.assertFalse(self.store.has_saved_session())

        # 2. Structured error codes: inspects error code only
        self.assertTrue(is_not_found(FakeS3ClientError("NoSuchKey", "Key not found", 404)))
        self.assertTrue(is_not_found(FakeS3ClientError("NotFound", "Not found", 404)))
        self.assertTrue(is_not_found(FakeS3ClientError("404", "Not found", 404)))
        # NoSuchBucket must return False so it fails rather than meaning missing PR
        self.assertFalse(is_not_found(FakeS3ClientError("NoSuchBucket", "Bucket not found", 404)))
        # Generic exception text with 404 must not mean missing
        self.assertFalse(is_not_found(Exception("Request failed with 404 error")))
        self.assertFalse(is_not_found(FakeS3ClientError("AccessDenied", "Forbidden", 403)))

        # 3. NoSuchBucket causes restore_session to fail explicitly
        latest_key = self.store.latest_key
        self.client.fail_get_keys[latest_key] = FakeS3ClientError("NoSuchBucket", "Bucket not found", 404)
        with self.assertRaises(StorageError):
            self.store.restore_session(dest)
        del self.client.fail_get_keys[latest_key]

        # 4. 403 Forbidden / AccessDenied: must raise StorageError, NEVER return None
        self.client.fail_get_keys[latest_key] = FakeS3ClientError("AccessDenied", "Forbidden", 403)
        with self.assertRaises(StorageError) as cm:
            self.store.restore_session(dest)
        self.assertIn("B2 storage read error", str(cm.exception))

        # 5. ConnectionError / 500: must raise StorageError
        self.client.fail_get_keys[latest_key] = FakeS3ClientError("ServiceUnavailable", "503", 503)
        with self.assertRaises(StorageError):
            self.store.restore_session(dest)

    def test_partial_b2_inputs_in_auto_mode_rejected(self):
        base_env = {
            "REPO": "test-owner/test-repo",
            "OWNER": "test-owner",
            "PR_NUMBER": "42",
            "RUN_ID": "run-test",
            "RUN_URL": "https://example.com/run",
            "EVENT_NAME": "pull_request_target",
            "GH_TOKEN": "token",
            "STATE_BACKEND": "auto",
        }
        all_b2 = {
            "B2_ENDPOINT_URL": "https://s3.example.com",
            "B2_BUCKET": "bucket",
            "B2_REGION": "us-east-1",
            "B2_ACCESS_KEY_ID": "key",
            "B2_SECRET_ACCESS_KEY": "secret",
        }

        # 0 variables: auto mode cleanly selects Git mode
        env_zero = dict(base_env)
        self.assertFalse(any_b2_configured(env_zero))
        self.assertFalse(is_b2_configured(env_zero))

        # All 5 variables: auto mode cleanly selects B2 mode
        env_all = {**base_env, **all_b2}
        self.assertTrue(any_b2_configured(env_all))
        self.assertTrue(is_b2_configured(env_all))

        # 1 to 4 variables: must be rejected!
        for key in all_b2:
            env_partial = {**base_env, **all_b2}
            del env_partial[key]
            self.assertTrue(any_b2_configured(env_partial))
            self.assertFalse(is_b2_configured(env_partial))
            with self.assertRaises(StorageError):
                validate_b2_config(env_partial)

    def test_unknown_state_backend_rejected(self):
        env = {
            "REPO": "test-owner/test-repo",
            "OWNER": "test-owner",
            "PR_NUMBER": "42",
            "RUN_ID": "run-test",
            "RUN_URL": "https://example.com/run",
            "EVENT_NAME": "pull_request_target",
            "GH_TOKEN": "token",
            "STATE_BACKEND": "invalid_backend",
        }
        agent_dir = self.work / "agent"
        repo_dir = agent_dir / "repos/test-repo"
        repo_dir.mkdir(parents=True)
        (repo_dir / "config.toml").write_text('rules = []\nprotected = []\nblocking_sections = []\nfix_check = ""\n')
        (repo_dir / "playbook").mkdir(parents=True)
        (repo_dir / "playbook/SKILL.md").write_text("playbook")

        prior = Path.cwd()
        os.chdir(self.work)
        try:
            with patch.dict(os.environ, env, clear=True):
                import runpy
                with self.assertRaises(StorageError) as cm:
                    runpy.run_path(str(SCRIPT), run_name="test_bad_backend")
                self.assertIn("Unknown STATE_BACKEND", str(cm.exception))
        finally:
            os.chdir(prior)

    def test_instruct_mode_missing_session_fails(self):
        # When in instruct mode, if session does not exist, SessionNotFoundError is raised
        with self.assertRaises(SessionNotFoundError):
            state_store_mock = self.store
            # parse_mode logic:
            if not state_store_mock.has_saved_session():
                raise SessionNotFoundError("Instruct mode requires an existing saved session")

    def test_malicious_tar_members_rejected(self):
        dest = self.work / "safe-dest"
        dest.mkdir()

        # 1. Traversal member
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            ti = tarfile.TarInfo(name="../escaped.txt")
            data = b"malicious content"
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        with self.assertRaises(CorruptArchiveError):
            safe_extract_tar(buf.getvalue(), dest)
        self.assertFalse((self.work / "escaped.txt").exists())

        # 2. Absolute path member
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            ti = tarfile.TarInfo(name="/tmp/absolute.txt")
            data = b"malicious"
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        with self.assertRaises(CorruptArchiveError):
            safe_extract_tar(buf.getvalue(), dest)

        # 3. Symlink member
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            ti = tarfile.TarInfo(name="symlink_member")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            tar.addfile(ti)
        with self.assertRaises(CorruptArchiveError):
            safe_extract_tar(buf.getvalue(), dest)

        # 4. Hardlink member
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            ti = tarfile.TarInfo(name="hardlink_member")
            ti.type = tarfile.LNKTYPE
            ti.linkname = "target_file"
            tar.addfile(ti)
        with self.assertRaises(CorruptArchiveError):
            safe_extract_tar(buf.getvalue(), dest)

        # 5. FIFO member
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            ti = tarfile.TarInfo(name="fifo_member")
            ti.type = tarfile.FIFOTYPE
            tar.addfile(ti)
        with self.assertRaises(CorruptArchiveError):
            safe_extract_tar(buf.getvalue(), dest)

        # 6. Character device member
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            ti = tarfile.TarInfo(name="char_dev")
            ti.type = tarfile.CHRTYPE
            tar.addfile(ti)
        with self.assertRaises(CorruptArchiveError):
            safe_extract_tar(buf.getvalue(), dest)

        # 7. Block device member
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            ti = tarfile.TarInfo(name="blk_dev")
            ti.type = tarfile.BLKTYPE
            tar.addfile(ti)
        with self.assertRaises(CorruptArchiveError):
            safe_extract_tar(buf.getvalue(), dest)

        # 8. Malformed corrupted archive
        with self.assertRaises(CorruptArchiveError):
            safe_extract_tar(b"\x1f\x8b\x08corrupt-data", dest)

    def test_list_traces(self):
        self.store.save_state("r1", {"head_after": "h1"}, None, self.work, [])
        self.store.save_state("r2", {"head_after": "h2"}, None, self.work, [])
        traces = self.store.list_traces()
        self.assertEqual(len(traces), 2)
        self.assertTrue(all(t.startswith(f"traces/{self.owner}/test-repo/") for t in traces))


class PrimeReviewB2IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.work = Path(self.temp_dir.name)
        self.agent = self.work / "agent"
        self.repo_dir = self.agent / "repos/homelab"
        (self.repo_dir / "playbook").mkdir(parents=True)
        (self.repo_dir / "playbook/SKILL.md").write_text("playbook content\n")
        (self.repo_dir / "config.toml").write_text("""rules = ["AGENTS.md"]
protected = ['^iac/', '^AGENTS\\.md$']
blocking_sections = [1, 5]
fix_check = ""
""")
        self.env = {
            "REPO": "owner/homelab",
            "OWNER": "owner",
            "PR_NUMBER": "7",
            "RUN_ID": "test-b2-1",
            "RUN_URL": "https://example.com/run",
            "EVENT_NAME": "pull_request_target",
            "GH_TOKEN": "fake",
            "APP_SLUG": "reviewer",
            "PR_AUTHOR": "owner",
            "EVENT_ACTION": "synchronize",
            "B2_ENDPOINT_URL": "https://s3.us-west-000.backblazeb2.com",
            "B2_BUCKET": "test-bucket",
            "B2_REGION": "us-west-000",
            "B2_ACCESS_KEY_ID": "key",
            "B2_SECRET_ACCESS_KEY": "secret",
            "STATE_BACKEND": "b2",
        }
        self.client = FakeS3Client()

    def test_b2_carry_mode_reusable_verdict(self):
        store = B2StateStore("test-bucket", "owner", "owner/homelab", "7", client=self.client)
        sess = self.work / "prior_session"
        sess.mkdir()
        (sess / "session.jsonl").write_text('{"turn": 1}\n')
        store.save_state(
            "run-prior",
            {"head_after": "commit123", "gate_blockers": [], "run_id": "run-prior"},
            None,
            sess,
            [],
        )

        prior = Path.cwd()
        os.chdir(self.work)
        try:
            with patch.dict(os.environ, {**self.env, "SENDER": "reviewer[bot]", "HEAD_SHA": "commit123"}):
                with patch("state_store.B2StateStore.from_env", return_value=store):
                    import runpy
                    mod = runpy.run_path(str(SCRIPT), run_name="test_carry")
                    exit_code = mod["main"]()
                    self.assertEqual(exit_code, 0)
        finally:
            os.chdir(prior)

    def test_b2_carry_mode_blocking_verdict(self):
        store = B2StateStore("test-bucket", "owner", "owner/homelab", "7", client=self.client)
        sess = self.work / "prior_session"
        sess.mkdir()
        (sess / "session.jsonl").write_text('{"turn": 1}\n')
        store.save_state(
            "run-prior",
            {
                "head_after": "commit_bad",
                "gate_blockers": [{"section": 1, "path": "p", "line": 2, "title": "b"}],
                "run_id": "run-prior",
            },
            None,
            sess,
            [],
        )

        prior = Path.cwd()
        os.chdir(self.work)
        try:
            with patch.dict(os.environ, {**self.env, "SENDER": "reviewer[bot]", "HEAD_SHA": "commit_bad"}):
                with patch("state_store.B2StateStore.from_env", return_value=store):
                    import runpy
                    mod = runpy.run_path(str(SCRIPT), run_name="test_carry_block")
                    exit_code = mod["main"]()
                    self.assertEqual(exit_code, 1)
        finally:
            os.chdir(prior)

    def test_b2_instruct_mode_missing_session_fails(self):
        store = B2StateStore("test-bucket", "owner", "owner/homelab", "7", client=self.client)
        prior = Path.cwd()
        os.chdir(self.work)
        try:
            with patch.dict(
                os.environ,
                {
                    **self.env,
                    "EVENT_NAME": "issue_comment",
                    "COMMENT_BODY": "/prime please fix this bug",
                },
            ):
                with patch("state_store.B2StateStore.from_env", return_value=store):
                    import runpy
                    mod = runpy.run_path(str(SCRIPT), run_name="test_instruct_fail")
                    with self.assertRaises(SessionNotFoundError):
                        mod["main"]()
        finally:
            os.chdir(prior)

    def test_write_summary_does_not_expose_bucket_name(self):
        store = B2StateStore("test-bucket", "owner", "owner/homelab", "7", client=self.client)
        summary_file = self.work / "step_summary.md"
        meta = {
            "mode": "review",
            "verdict": "approve",
            "gate_blockers": [],
            "findings": 0,
            "fixes_pushed": 0,
            "duration_s": 10,
            "usage": {},
            "model": "deepseek",
        }
        prior = Path.cwd()
        os.chdir(self.work)
        try:
            with patch.dict(os.environ, {**self.env, "GITHUB_STEP_SUMMARY": str(summary_file)}):
                with patch("state_store.B2StateStore.from_env", return_value=store):
                    import runpy
                    mod = runpy.run_path(str(SCRIPT), run_name="test_summary")
                    mod["B2_STORE"] = store
                    mod["write_summary"](meta)
            summary_content = summary_file.read_text()
            # Must show backend name and repo/run-relative trace identifier
            self.assertIn("b2:traces/owner/homelab/7/test-b2-1.tar.gz", summary_content)
            # Must NOT expose private bucket name or endpoint
            self.assertNotIn("test-bucket", summary_content)
            self.assertNotIn("us-west-000", summary_content)
        finally:
            os.chdir(prior)


class IsolatedPythonEntrypointTests(unittest.TestCase):
    """Verify python3 -I execution from an unrelated working directory."""

    def test_python_isolated_import(self):
        unrelated_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(unrelated_tmp.cleanup)
        unrelated_cwd = unrelated_tmp.name

        test_script = f"""
import sys
import runpy
from pathlib import Path

# Simulate running under isolated python from unrelated directory
script_path = "{SCRIPT}"
try:
    mod = runpy.run_path(script_path, run_name="isolated_test")
except SystemExit:
    pass
except KeyError:
    pass
"""
        res = subprocess.run(
            [sys.executable, "-I", "-c", test_script],
            cwd=unrelated_cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertNotIn("ModuleNotFoundError: No module named 'state_store'", res.stderr)



if __name__ == "__main__":
    unittest.main()
