"""Offline regression tests for configuration, isolation and proposed lessons."""
import json
import os
import sys
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SCRIPT = REPO_ROOT / 'prime-review.py'


class ReviewerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.agent = self.work / 'agent'
        self.repo = self.agent / 'repos/homelab'
        (self.repo / 'playbook').mkdir(parents=True)
        (self.repo / 'playbook/SKILL.md').write_text('original playbook\n')
        (self.agent / 'skills/common-review').mkdir(parents=True)
        (self.agent / 'skills/common-review/SKILL.md').write_text('original common\n')
        (self.agent / 'APPEND_SYSTEM.md').write_text('fixed core\n')
        (self.repo / 'config.toml').write_text('''rules = ["AGENTS.md"]
protected = ['^iac/', '^AGENTS\\.md$']
blocking_sections = [1, 5]
fix_check = ""
''')
        self.env = dict(REPO='owner/homelab', OWNER='owner', PR_NUMBER='7',
                        RUN_ID='test-1', RUN_URL='https://example.com/run',
                        EVENT_NAME='pull_request_target', GH_TOKEN='fake',
                        APP_SLUG='reviewer', PR_AUTHOR='owner', EVENT_ACTION='synchronize')
        self.mod = self.load()

    def load(self, **env):
        prior = Path.cwd()
        os.chdir(self.work)
        try:
            with patch.dict(os.environ, {**self.env, **env}, clear=True):
                return runpy.run_path(str(SCRIPT), run_name='review_tests')
        finally:
            os.chdir(prior)

    def call(self, name, *args):
        with patch.dict(os.environ, self.env, clear=True):
            return self.mod[name](*args)

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.agent, check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_legacy_session_restores_only_for_homelab(self):
        legacy = self.work / 'state/sessions/pr-7'
        legacy.mkdir(parents=True)
        (legacy / 'session.jsonl').write_text('{}\n')
        self.assertEqual(self.call('saved_session'), legacy)
        self.assertEqual(self.call('parse_mode')[0], 'update')
        # Another repo with the same PR number must begin a fresh review.
        other = self.agent / 'repos/other'
        (other / 'playbook').mkdir(parents=True)
        (other / 'playbook/SKILL.md').write_text('other\n')
        (other / 'config.toml').write_text((self.repo / 'config.toml').read_text())
        mod = self.load(REPO='owner/other')
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(mod['parse_mode']()[0], 'review')
        current = self.work / 'state/sessions/homelab/pr-7'
        current.mkdir(parents=True)
        self.assertEqual(self.call('saved_session'), current)

    def test_traces_do_not_carry_another_repos_verdict(self):
        traces = self.work / 'state/traces'
        for run, repo in [('a', 'owner/homelab'), ('b', 'owner/other'), ('c', None)]:
            folder = traces / run
            folder.mkdir(parents=True)
            (folder / 'meta.json').write_text(json.dumps(dict(pr=7, repo=repo, run_id=run)))
        self.assertEqual([m['run_id'] for m in self.call('pr_traces')], ['a'])

    def test_gate_uses_configured_sections(self):
        findings = [dict(section=n, blocker=True) for n in [1, 2, 4, 5]]
        self.assertEqual([f['section'] for f in self.call('gate_blockers', dict(findings=findings))], [1, 5])
        self.assertTrue(self.call('gate_blockers', None))
        self.assertFalse(self.call('gate_blockers', dict(findings=[])))

    def test_config_cannot_silently_disable_gate(self):
        config = self.repo / 'config.toml'
        config.write_text(config.read_text().replace('blocking_sections = [1, 5]\n', ''))
        with self.assertRaisesRegex(SystemExit, 'blocking_sections'):
            self.load()

    def test_protected_paths_always_include_workflows(self):
        deny = self.mod['DENY']
        for path in ['.github/workflows/test.yml', 'iac/vm.tf', 'AGENTS.md']:
            self.assertIsNotNone(deny.match(path))
        self.assertIsNone(deny.match('plans/change.md'))

    def test_playbook_cannot_point_at_another_repo(self):
        config = self.repo / 'config.toml'
        config.write_text(config.read_text() + 'playbook = "skills/common-review"\n')
        with self.assertRaisesRegex(SystemExit, 'inside this repository'):
            self.load()

    def test_learning_proposal_excludes_core_config_and_other_playbooks(self):
        other = self.agent / 'repos/other/playbook/SKILL.md'
        other.parent.mkdir(parents=True)
        other.write_text('other\n')
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'user.name', 'Test')
        self.git('add', '.')
        self.git('commit', '-qm', 'Initial configuration')
        (self.agent / 'APPEND_SYSTEM.md').write_text('unwanted change\n')
        other.write_text('unwanted other change\n')
        (self.repo / 'config.toml').write_text('unwanted config change\n')
        (self.repo / 'playbook/SKILL.md').write_text('lesson\n')
        (self.agent / 'skills/common-review/SKILL.md').write_text('shared lesson\n')
        self.git('add', '.')  # Agent staging must not bypass the allowed list.
        fn = self.mod['propose_learning']
        real_git = fn.__globals__['git']
        pushes = []
        def fake_git(args, cwd, **kwargs):
            if args[0] == 'push':
                pushes.append(args)
                return subprocess.CompletedProcess([], 0, '', '')
            return real_git(args, cwd, **kwargs)
        with patch.dict(fn.__globals__, git=fake_git,
                        gh_api=lambda *a, **kw: {'html_url': 'https://example.com/pr'}):
            self.assertEqual(fn(), 'https://example.com/pr')
        changed = self.git('show', '--format=', '--name-only', 'HEAD').splitlines()
        self.assertEqual(sorted(changed), ['repos/homelab/playbook/SKILL.md', 'skills/common-review/SKILL.md'])
        self.assertIn('learn/homelab-pr-7-test-1', pushes[0][-1])


if __name__ == '__main__':
    unittest.main()
