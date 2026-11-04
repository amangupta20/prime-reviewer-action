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
                        APP_SLUG='reviewer', PR_AUTHOR='owner', EVENT_ACTION='synchronize',
                        OPENCODE_API_KEY='model-key',
                        PRIME_JAIL=str(Path(self.temp.name) / 'jail'))
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

    def test_import_cannot_resume_after_a_later_legacy_write(self):
        fn = self.mod['verify_legacy_snapshot']
        calls = []
        def api(*args, **kwargs):
            calls.append(args)
            return {'object': {'sha': 'final-snapshot'}}
        with patch.dict(fn.__globals__, gh_api=api):
            fn(None)
            fn({'run_id': 'native-b2-run'})
            self.assertEqual(calls, [])
            fn({'legacy_source_commit': 'final-snapshot'})
            self.assertEqual(len(calls), 1)
            with self.assertRaisesRegex(self.mod['state_store'].StorageError, 'Legacy state changed'):
                fn({'legacy_source_commit': 'older-snapshot'})

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


    def test_credentials_leave_the_process_environment(self):
        self.assertEqual(self.mod['SECRETS'], {'GH_TOKEN': 'fake', 'OPENCODE_API_KEY': 'model-key'})
        fn = self.mod['git']
        seen = []
        def fake_run(cmd, **kwargs):
            seen.append((cmd, kwargs.get('env')))
            return subprocess.CompletedProcess(cmd, 0, '', '')
        with patch.dict(os.environ, {'PATH': '/usr/bin'}, clear=True), \
                patch.dict(fn.__globals__, run=fake_run):
            fn(['push', 'origin', 'HEAD'], self.work, auth=True)
            fn(['status'], self.work)
            self.assertEqual(fn.__globals__['gh_env']()['GH_TOKEN'], 'fake')
        (push, push_env), (status, status_env) = seen
        # The token travels in the environment, never on a command line.
        self.assertNotIn('fake', ' '.join(push))
        self.assertTrue(any('basic' in v for v in push_env.values()))
        self.assertIsNone(status_env)

    def test_copy_back_skips_symlinks(self):
        src, dst = self.work / 'src', self.work / 'dst'
        (src / 'sub').mkdir(parents=True)
        (src / 'sub/kept.jsonl').write_text('{}\n')
        secret = self.work / 'secret.txt'
        secret.write_text('private\n')
        (src / 'link.jsonl').symlink_to(secret)
        (src / 'linkdir').symlink_to(self.work)
        self.mod['copy_regular'](src, dst)
        self.assertEqual(sorted(str(p.relative_to(dst)) for p in dst.rglob('*') if p.is_file()),
                         ['sub/kept.jsonl'])

    def _pr_with_bundle(self, change):
        """A PR checkout and a bundle of one commit made by `change` in a copy."""
        pr = self.work / 'pr'
        def g(cwd, *args):
            return subprocess.run(['git', *args], cwd=cwd, check=True,
                                  capture_output=True, text=True).stdout.strip()
        pr.mkdir()
        g(pr, 'init', '-q', '-b', 'feature')
        g(pr, 'config', 'user.email', 'test@example.com')
        g(pr, 'config', 'user.name', 'Test')
        (pr / 'iac').mkdir()
        (pr / 'iac/main.tf').write_text('x\n')
        (pr / 'notes.md').write_text('a\n')
        g(pr, 'add', '.')
        g(pr, 'commit', '-qm', 'Base')
        head = g(pr, 'rev-parse', 'HEAD')
        work = self.work / 'copy'
        g(self.work, 'clone', '-q', str(pr), str(work))
        g(work, 'config', 'user.email', 'test@example.com')
        g(work, 'config', 'user.name', 'Test')
        change(work, g)
        g(work, 'commit', '-qam', 'Change')
        out = Path(self.env['PRIME_JAIL']) / 'out'
        out.mkdir(parents=True)
        g(work, 'bundle', 'create', '-q', str(out / 'commits.bundle'), 'HEAD', f'^{head}')
        return head, g(work, 'rev-parse', 'HEAD')

    def _check_and_push(self, head, prime_head, command=""):
        fn = self.mod['check_and_push']
        real_git = fn.__globals__['git']
        pushes = []
        def fake_git(args, cwd, **kwargs):
            if args[0] == 'push':
                pushes.append(args)
                return subprocess.CompletedProcess([], 0, '', '')
            return real_git(args, cwd, **kwargs)
        ctx = {'head': head, 'pr': {'state': 'open', 'head': {'ref': 'feature'}}}
        with patch.dict(fn.__globals__, git=fake_git):
            return fn(ctx, prime_head, command=command), pushes

    def test_commits_come_back_from_the_bundle(self):
        def edit(work, g):
            (work / 'notes.md').write_text('b\n')
        head, prime_head = self._pr_with_bundle(edit)
        (new_head, commits, note), pushes = self._check_and_push(head, prime_head)
        self.assertEqual((new_head, note, len(commits), len(pushes)), (prime_head, '', 1, 1))

    def test_moving_a_protected_file_out_is_not_pushed(self):
        def move(work, g):
            g(work, 'mv', 'iac/main.tf', 'main.tf')
        head, prime_head = self._pr_with_bundle(move)
        (new_head, commits, note), pushes = self._check_and_push(head, prime_head)
        self.assertEqual((new_head, pushes), (head, []))
        self.assertIn('iac/main.tf', note)

    def test_ask_command_cannot_push_even_with_commits_bundle(self):
        def edit(work, g):
            (work / 'notes.md').write_text('b\n')
        head, prime_head = self._pr_with_bundle(edit)
        (new_head, commits, note), pushes = self._check_and_push(head, prime_head, command="ask")
        self.assertEqual(new_head, head)
        self.assertEqual(commits, [])
        self.assertEqual(pushes, [])
        self.assertIn("ask command is read-only", note)

    def test_ask_command_does_not_propose_learning(self):
        fn = self.mod['propose_learning']
        self.assertEqual(fn(command="ask"), "")

    def test_parse_mode_routing(self):
        saved = self.work / 'state/sessions/homelab/pr-7'
        saved.mkdir(parents=True)
        (saved / 'session.jsonl').write_text('{}\n')

        def parse_with_comment(body):
            mod = self.load(EVENT_NAME='issue_comment', COMMENT_BODY=body)
            with patch.dict(os.environ, {**self.env, 'EVENT_NAME': 'issue_comment', 'COMMENT_BODY': body}):
                return mod['parse_mode']()

        # /prime review
        self.assertEqual(parse_with_comment('/prime review'), ('review', '', '', 'review'))

        # /prime review performance
        self.assertEqual(parse_with_comment('/prime review performance'),
                         ('review', '', 'performance', 'review'))

        # /prime ask why is this mutex needed?
        self.assertEqual(parse_with_comment('/prime ask why is this mutex needed?'),
                         ('instruct', 'why is this mutex needed?', '', 'ask'))

        # /prime fix add return statement
        self.assertEqual(parse_with_comment('/prime fix add return statement'),
                         ('instruct', 'add return statement', '', 'fix'))

        # /prime natural instruction
        self.assertEqual(parse_with_comment('/prime please check formatting'),
                         ('instruct', 'please check formatting', '', 'instruct'))

        # /primer should be rejected
        with self.assertRaises(ValueError):
            parse_with_comment('/primer review')

    def test_status_updates_only_our_marked_comment(self):
        fn = self.mod['update_status']
        calls = []
        marker = self.mod['review_state'].MARKER
        def api(path, method='GET', body=None):
            calls.append((path, method, body))
            if method == 'GET':
                return [
                    {'id': 1, 'user': {'login': 'owner'}, 'body': marker},
                    {'id': 2, 'user': {'login': 'reviewer[bot]'}, 'body': marker},
                ]
        with patch.dict(fn.__globals__, gh_api=api), patch.dict(os.environ, APP_SLUG='reviewer'):
            fn('Blocked', note='Changes were not pushed.')
        self.assertEqual(calls[-1][0], 'repos/owner/homelab/issues/comments/2')
        self.assertEqual(calls[-1][1], 'PATCH')
        self.assertIn('Prime: Blocked', calls[-1][2]['body'])

    def test_closed_pr_checkout_survives_deleted_source_branch(self):
        source = self.work / 'source'
        source.mkdir()
        def local_git(*args):
            return subprocess.run(['git', *args], cwd=source, check=True,
                                  capture_output=True, text=True).stdout.strip()
        local_git('init', '-b', 'main')
        local_git('config', 'user.name', 'Test')
        local_git('config', 'user.email', 'test@example.com')
        (source / 'note').write_text('base')
        local_git('add', '.')
        local_git('commit', '-m', 'base')
        local_git('checkout', '-b', 'deleted-feature')
        (source / 'note').write_text('PR change')
        local_git('commit', '-am', 'change')
        expected = local_git('rev-parse', 'HEAD')
        local_git('update-ref', 'refs/pull/7/head', expected)
        local_git('checkout', 'main')
        local_git('branch', '-D', 'deleted-feature')
        fn = self.mod['checkout_pr']
        original_git = fn.__globals__['git']
        def git(args, cwd, **kwargs):
            if args[:3] == ['remote', 'add', 'origin']:
                args = [*args[:3], str(source)]
            return original_git(args, cwd, **kwargs)
        pr = {'state': 'closed', 'head': {'ref': 'deleted-feature',
              'repo': {'full_name': 'owner/homelab'}}, 'base': {'ref': 'main'}}
        def api(path):
            return {'id': 1} if path.startswith('users/') else pr
        with patch.dict(fn.__globals__, git=git, gh_api=api), patch.dict(os.environ, self.env):
            ctx = fn()
        self.assertEqual(ctx['head'], expected)
        self.assertEqual((self.mod['PR_DIR'] / 'note').read_text(), 'PR change')
    def _session(self, folder, name, cwd, records=(), mtime=None):
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / name
        lines = [json.dumps({'type': 'session', 'id': name, 'cwd': cwd})]
        lines += [json.dumps(r) for r in records]
        f.write_text('\n'.join(lines) + '\n')
        if mtime is not None:
            os.utime(f, (mtime, mtime))
        return f

    def test_saved_session_is_pointed_at_the_jail_folder(self):
        sessions = self.work / 'sessions'
        old = self._session(sessions, 'a.jsonl', '/home/runner/pr', [{'n': 1}], mtime=1000)
        (sessions / 'a.info-cache.json').write_text('{}')
        jail = self.work / 'jail/work'
        self.mod['repoint_session_cwd'](sessions, jail)
        header, rest = old.read_text().split('\n', 1)
        self.assertEqual(json.loads(header)['cwd'], str(jail))
        self.assertEqual(rest, json.dumps({'n': 1}) + '\n')
        self.assertEqual(old.stat().st_mtime, 1000)
        self.assertFalse((sessions / 'a.info-cache.json').exists())

    def test_a_session_already_in_the_jail_folder_is_left_alone(self):
        sessions = self.work / 'sessions'
        jail = self.work / 'jail/work'
        older = self._session(sessions, 'a.jsonl', '/home/runner/pr', mtime=1000)
        self._session(sessions, 'b.jsonl', str(jail), mtime=900)
        before = older.read_text()
        self.mod['repoint_session_cwd'](sessions, jail)
        self.assertEqual(older.read_text(), before)

    def test_usage_comes_from_the_session_prime_wrote(self):
        sessions = self.work / 'sessions'
        turn = {'message': {'role': 'assistant', 'usage': {'input': 10, 'output': 2,
                                                           'cost': {'total': 0.5}}}}
        # Sorted first by name, but older: an earlier conversation.
        self._session(sessions, 'a.jsonl', '/old', [turn], mtime=1000)
        new = self._session(sessions, 'z.jsonl', '/new', [turn, turn], mtime=2000)
        fn = self.mod['usage_since']
        with patch.dict(fn.__globals__, SESSION=sessions, AGENT=self.agent):
            self.assertEqual(self.mod['state_store'].newest_session_file(sessions), new)
            u = fn(0)
        self.assertEqual((u['turns'], u['cost']), (2, 1.0))

if __name__ == '__main__':
    unittest.main()
