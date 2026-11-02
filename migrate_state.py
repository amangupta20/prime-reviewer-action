#!/usr/bin/env python3
"""Import a frozen legacy Git state checkout into B2 without deleting its source."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from state_store import B2StateStore, StorageError, create_trace_bundle, safe_extract_tar


def hashes(path):
    return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob('*') if p.is_file()}


def migrate(source, repo, apply=False, refresh_imports=False):
    """Return verified PR count. --apply writes additive objects and latest pointers."""
    commit = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if subprocess.check_output(['git', '-C', str(source), 'status', '--porcelain'], text=True).strip():
        raise RuntimeError('Legacy source checkout must be clean')
    owner, name = repo.split('/', 1)
    records = []
    for path in (source / 'traces').glob('*/meta.json'):
        meta = json.loads(path.read_text())
        if meta.get('repo') == repo:
            records.append((meta, path.parent))
    groups = {}
    for meta, path in sorted(records, key=lambda x: x[0].get('started', '')):
        groups.setdefault(str(meta['pr']), []).append((meta, path))
    if not groups:
        raise RuntimeError('No matching legacy PR traces found')
    print(f'Legacy snapshot {commit}: {len(groups)} PRs, {len(records)} trace bundles')
    verified = 0
    for pr, traces in groups.items():
        env = {**os.environ, 'OWNER': owner, 'REPO': repo, 'PR_NUMBER': pr}
        store = B2StateStore.from_env(env)
        existing = store.get_latest_metadata()
        if existing and existing.get('legacy_source_commit') != commit and not (refresh_imports and existing.get('legacy_source_commit')):
            raise RuntimeError('Destination already has different state; refusing to overwrite')
        session = source / 'sessions' / name / f'pr-{pr}'
        if not session.exists() and name == 'homelab':
            session = source / 'sessions' / f'pr-{pr}'
        if not session.exists() or not any(session.glob('*.jsonl')):
            raise RuntimeError('Legacy PR session missing; no upload performed')
        if not apply:
            print(f'PR {pr}: ready to import {len(traces)} traces and restore-test its session')
            continue
        for meta, path in traces[:-1]:
            with tempfile.TemporaryDirectory() as tmp:
                td = Path(tmp)
                shutil.copy(path / 'session.jsonl', td / 'session.jsonl')
                result = json.loads((path / 'result.json').read_text()) if (path / 'result.json').exists() else None
                manifest = {'schema_version': 1, 'source': 'prime-reviewer-git-import',
                            'repo': repo, 'pr': int(pr), 'run_id': meta['run_id'],
                            'legacy_source_commit': commit,
                            'session_lines_before': meta.get('session_lines_before', 0)}
                bundle = create_trace_bundle(meta, result, td, [path / 'subagents'], manifest)
                store._client.put_object(Bucket=store.bucket, Key=store.trace_key(meta['run_id']),
                                         Body=bundle, ContentType='application/gzip')
        meta, trace = traces[-1]
        meta = {**meta, 'legacy_source_commit': commit}
        result = json.loads((trace / 'result.json').read_text()) if (trace / 'result.json').exists() else None
        store.save_state(meta['run_id'], meta, result, session, [trace / 'subagents'],
                         config_commit=meta.get('config_commit', ''),
                         session_lines_before=meta.get('session_lines_before', 0))
        with tempfile.TemporaryDirectory() as tmp:
            restored = Path(tmp) / 'session'
            actual = store.restore_session(restored)
            if hashes(restored) != hashes(session) or actual.get('run_id') != meta['run_id']:
                raise RuntimeError('Restored session differs from legacy source')
        # Verify every archived root/child transcript, including older runs.
        for record, original in traces:
            data = store._get_object(store.trace_key(record['run_id']))
            if data is None:
                raise RuntimeError('Uploaded historical trace is missing')
            with tempfile.TemporaryDirectory() as tmp:
                dest = Path(tmp)
                safe_extract_tar(data, dest)
                if (dest / 'session.jsonl').read_bytes() != (original / 'session.jsonl').read_bytes():
                    raise RuntimeError('Archived root transcript differs from legacy source')
                if (original / 'subagents').exists() and hashes(dest / 'subagents') != hashes(original / 'subagents'):
                    raise RuntimeError('Archived child transcripts differ from legacy source')
        verified += 1
        print(f'PR {pr}: imported and verified session plus {len(traces)} trace bundles')
    return verified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--apply', action='store_true', help='Upload and restore-test; default is read-only preflight')
    parser.add_argument('--refresh-imports', action='store_true', help='Refresh imported state only; never overwrite a native B2 review')
    args = parser.parse_args()
    migrate(args.source, args.repo, args.apply, args.refresh_imports)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # SDK messages may include private endpoints or object names.
        print(f'Migration failed ({type(error).__name__}); source state was not deleted.', file=sys.stderr)
        sys.exit(1)
