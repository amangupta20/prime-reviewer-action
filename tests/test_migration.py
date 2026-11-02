"""Migration must preserve real session bytes and refuse native B2 state."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from migrate_state import migrate
from state_store import B2StateStore
from test_state_store import FakeS3Client


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = Path(self.tmp.name)
        self.session = self.source / 'sessions/pr-7'
        self.session.mkdir(parents=True)
        (self.session / 'original-id.jsonl').write_text('{"history":"preserve"}\n')
        trace = self.source / 'traces/old-run'
        trace.mkdir(parents=True)
        self.meta = {'repo': 'owner/homelab', 'pr': 7, 'run_id': 'old-run',
                     'started': '2026-11-03T00:00:00Z', 'head_after': 'original'}
        (trace / 'meta.json').write_text(json.dumps(self.meta))
        (trace / 'session.jsonl').write_bytes((self.session / 'original-id.jsonl').read_bytes())
        self.git('init', '-q', '-b', 'state')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.com')
        self.git('add', '.')
        self.git('commit', '-qm', 'Legacy session')
        self.store = B2StateStore('test-bucket', 'owner', 'owner/homelab', 7, client=FakeS3Client())

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.source, capture_output=True,
                              text=True, timeout=10, check=True)

    def run_migration(self, **kwargs):
        with patch('migrate_state.B2StateStore.from_env', return_value=self.store):
            with contextlib.redirect_stdout(io.StringIO()):
                return migrate(self.source, 'owner/homelab', **kwargs)

    def test_import_roundtrip_preserves_original_filename_and_bytes(self):
        self.assertEqual(self.run_migration(apply=False), 0)
        self.assertFalse(self.store.has_saved_session())
        self.assertEqual(self.run_migration(apply=True), 1)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            self.store.restore_session(target)
            self.assertEqual((target / 'original-id.jsonl').read_bytes(),
                             (self.session / 'original-id.jsonl').read_bytes())
        self.assertEqual(self.git('status', '--porcelain').stdout, '')

    def test_native_review_cannot_be_overwritten_even_with_refresh(self):
        self.store.save_state('native-run', self.meta, None, self.session, [])
        with self.assertRaisesRegex(RuntimeError, 'refusing to overwrite'):
            self.run_migration(apply=True, refresh_imports=True)
        self.assertEqual(self.store.get_latest_metadata()['trace_id'], 'owner/homelab#7/native-run')

    def test_repeated_import_and_later_legacy_run(self):
        # 1. Initial migration
        self.assertEqual(self.run_migration(apply=True), 1)
        trace_key_old = self.store.trace_key('old-run')
        old_archive_bytes = self.store._get_object(trace_key_old)
        self.assertIsNotNone(old_archive_bytes)
        latest_1 = self.store.get_latest_metadata()
        self.assertEqual(latest_1['run_id'], 'old-run')

        # 2. Repeated import (same snapshot / refresh)
        self.assertEqual(self.run_migration(apply=True, refresh_imports=True), 1)
        # Old archive bytes remain strictly equal
        self.assertEqual(self.store._get_object(trace_key_old), old_archive_bytes)
        self.assertEqual(self.store.get_latest_metadata()['run_id'], 'old-run')

        # 3. Later legacy run: add new-run in source repo
        trace2 = self.source / 'traces/new-run'
        trace2.mkdir(parents=True)
        (self.session / 'original-id.jsonl').write_text('{"history":"preserve"}\n{"history":"second"}\n')
        (trace2 / 'session.jsonl').write_bytes((self.session / 'original-id.jsonl').read_bytes())
        (trace2 / 'result.json').write_text(json.dumps({'summary': 'second review', 'verdict': 'approve'}))
        sub_dir = trace2 / 'subagents'
        sub_dir.mkdir(parents=True)
        (sub_dir / 'agent-1.jsonl').write_text('{"agent": 1}\n')
        meta2 = {'repo': 'owner/homelab', 'pr': 7, 'run_id': 'new-run',
                 'started': '2026-11-03T01:00:00Z', 'head_after': 'second'}
        (trace2 / 'meta.json').write_text(json.dumps(meta2))
        self.git('add', '.')
        self.git('commit', '-qm', 'Later legacy run')

        # Migrate with refresh_imports
        self.assertEqual(self.run_migration(apply=True, refresh_imports=True), 1)

        # Old archive bytes STILL remain equal
        self.assertEqual(self.store._get_object(trace_key_old), old_archive_bytes)

        # Only the new archive is added
        trace_key_new = self.store.trace_key('new-run')
        new_archive_bytes = self.store._get_object(trace_key_new)
        self.assertIsNotNone(new_archive_bytes)
        self.assertNotEqual(trace_key_old, trace_key_new)

        # Latest pointer advances
        latest_2 = self.store.get_latest_metadata()
        self.assertEqual(latest_2['run_id'], 'new-run')
        self.assertEqual(latest_2['head_after'], 'second')

        # 4. Changed source transcript at an existing run ID is rejected rather than overwritten
        (self.source / 'traces/old-run/session.jsonl').write_text('{"history":"tampered"}\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Tamper old run transcript')

        # Must reject in preflight (apply=False)
        with self.assertRaisesRegex(RuntimeError, 'Archived root transcript differs'):
            self.run_migration(apply=False, refresh_imports=True)

        # Must reject in apply (apply=True)
        with self.assertRaisesRegex(RuntimeError, 'Archived root transcript differs'):
            self.run_migration(apply=True, refresh_imports=True)

        # Ensure old archive in B2 was NOT overwritten
        self.assertEqual(self.store._get_object(trace_key_old), old_archive_bytes)

    def test_changed_structured_result_rejected(self):
        trace = self.source / 'traces/old-run'
        (trace / 'result.json').write_text(json.dumps({'summary': 'orig', 'verdict': 'approve'}))
        self.git('add', '.')
        self.git('commit', '-qm', 'Add result')

        self.assertEqual(self.run_migration(apply=True), 1)

        # Tamper result.json in source
        (trace / 'result.json').write_text(json.dumps({'summary': 'tampered', 'verdict': 'approve'}))
        self.git('add', '.')
        self.git('commit', '-qm', 'Tamper result')

        with self.assertRaisesRegex(RuntimeError, 'Archived structured result differs'):
            self.run_migration(apply=True, refresh_imports=True)

    def test_changed_child_transcripts_rejected(self):
        trace = self.source / 'traces/old-run'
        sub = trace / 'subagents'
        sub.mkdir(parents=True)
        (sub / 'child.jsonl').write_text('{"turn": 1}\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Add subagent')

        self.assertEqual(self.run_migration(apply=True), 1)

        # Tamper subagent in source
        (sub / 'child.jsonl').write_text('{"turn": 999}\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'Tamper subagent')

        with self.assertRaisesRegex(RuntimeError, 'Archived child transcripts differ'):
            self.run_migration(apply=True, refresh_imports=True)


if __name__ == '__main__':
    unittest.main()
