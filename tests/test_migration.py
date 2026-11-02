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


if __name__ == '__main__':
    unittest.main()
