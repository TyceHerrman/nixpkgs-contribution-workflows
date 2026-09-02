"""Entrypoints for isolated Actions jobs and explicit operator reconciliation."""

import argparse
import html
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from . import core
from .core import Error, require
from .github import API, ContentsStore, MAX_BYTES, Runner

ROOT = Path(__file__).resolve().parents[1]


def configuration():
    owner = os.environ['GITHUB_REPOSITORY_OWNER']
    repository = os.environ['GITHUB_REPOSITORY']
    coordinator = os.environ.get('COORDINATION_REPOSITORY') or f'{owner}/nixpkgs-contribution-workflows'
    require(repository.lower() == coordinator.lower(),
            'Run this workflow in its installation repository; remote callers must dispatch review.yml there')
    config = {'fork': os.environ.get('NIXPKGS_FORK_REPOSITORY') or f'{owner}/nixpkgs',
              'runner': os.environ.get('NIXPKGS_REVIEW_GHA_REPOSITORY') or f'{owner}/nixpkgs-review-gha',
              'upstream': 'NixOS/nixpkgs', 'base_ref': 'master'}
    core.validate_config(config)
    return config


def identity():
    return {key: os.environ[key] for key in ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_WORKFLOW_SHA')}


def write_snapshot(path, snapshot):
    Path(path).write_text(core.canonical({'schema': 1, 'identity': identity(), 'snapshot': snapshot}) + '\n')


def read_snapshot(path):
    require(Path(path).stat().st_size <= MAX_BYTES, 'Snapshot is too large')
    try:
        envelope = json.loads(Path(path).read_text())
    except (ValueError, UnicodeError):
        raise Error('Invalid snapshot JSON') from None
    require(isinstance(envelope, dict) and set(envelope) == {'schema', 'identity', 'snapshot'} and
            type(envelope['schema']) is int and envelope['schema'] == 1, 'Invalid snapshot envelope')
    require(envelope['identity'] == identity(), 'Snapshot is from a different workflow run or revision')
    return envelope['snapshot']


def evaluation_environment():
    # Do not expose GitHub file commands, runtime credentials, read tokens, or
    # caller-defined environment variables to untrusted Nix evaluation.
    env = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'TMPDIR', 'SSL_CERT_FILE', 'NIX_SSL_CERT_FILE') if key in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
    return env


class Evaluator:
    def __init__(self, repositories):
        self.repositories = repositories

    def __call__(self, sha, attribute):
        core.validate_sha(sha)
        core.validate_attribute(attribute)
        env = evaluation_environment()
        with tempfile.TemporaryDirectory(prefix='nixpkgs-metadata-') as directory:
            subprocess.run(['git', 'init', '--quiet', directory], check=True, env=env, capture_output=True)
            for repository in self.repositories:
                core.validate_repo(repository)
                fetched = subprocess.run(['git', '-C', directory, '-c', 'credential.helper=', 'fetch', '--quiet', '--depth=1',
                                          f'https://github.com/{repository}.git', sha], env=env, capture_output=True, timeout=600)
                if fetched.returncode == 0:
                    break
            else:
                raise Error('Could not fetch the resolved immutable commit from the configured repositories')
            subprocess.run(['git', '-C', directory, '-c', 'core.hooksPath=/dev/null', 'checkout', '--quiet', '--detach', sha],
                           check=True, env=env, capture_output=True, timeout=600)
            result = subprocess.run(['nix-instantiate', '--eval', '--strict', '--json', '--readonly-mode',
                                     '--option', 'allow-import-from-derivation', 'false', str(ROOT / 'metadata.nix'),
                                     '--argstr', 'nixpkgs', directory, '--argstr', 'attribute', attribute],
                                    capture_output=True, text=True, env=env, timeout=900)
            if result.returncode:
                # Render evaluator diagnostics as ordinary text, not Actions commands.
                print('Nix evaluator diagnostics: ' + repr(result.stderr[-6000:]))
                raise Error('Nix metadata evaluation failed; missing attributes and unexpected evaluation errors are not unsupported platforms')
            require(len(result.stdout) <= MAX_BYTES, 'Nix metadata result is too large')
            return core.validate_metadata(json.loads(result.stdout))


def output(**values):
    lines = []
    for key, value in values.items():
        value = str(value)
        require('\n' not in value and '\r' not in value, 'Multiline Actions output rejected')
        lines.append(f'{key}={value}\n')
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            stream.writelines(lines)


def summary(result):
    lines = [f"**{html.escape(result['action'])}**"]
    if result.get('pr_url'):
        lines.append(f"[Pull request]({result['pr_url']})")
    if result.get('run_url'):
        lines.append(f"[Review run]({result['run_url']})")
    if result.get('reason'):
        lines.append(html.escape(result['reason']))
    if result.get('coverage'):
        lines.append(f"Coverage: {html.escape(result['coverage'])}.")
    text = ' · '.join(lines) + '\n'
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write(text)
    print(core.canonical(result))


def metadata_summary(snapshot):
    metadata = snapshot.get('new', snapshot.get('metadata'))
    rows = ['\n| System | Eligible | Reason |', '| --- | --- | --- |']
    for system, row in metadata['systems'].items():
        rows.append(f"| {system} | {'yes' if row['eligible'] else 'no'} | {html.escape(row['reason'])} |")
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as stream:
            stream.write('\n'.join(rows) + '\n')


def inputs():
    value = json.loads(os.environ.get('INPUT_JSON', '{}'))
    require(isinstance(value, dict), 'Invalid workflow inputs')
    return value


def submission_request(value):
    return {key: value.get(key, default) for key, default in
            [('branch', ''), ('attribute', ''), ('body', ''), ('title', ''), ('draft', True)]}


def required_token(name):
    token = os.environ.get(name, '')
    require(bool(token), f'{name} is required for this action')
    return token


def execute(command, path):
    config = configuration()
    values = inputs()
    reader = API(os.environ.get('GITHUB_READ_TOKEN', ''))
    if command == 'preflight':
        require(values.get('mode', 'preflight') in {'preflight', 'submit'}, 'Invalid submission mode')
        snapshot = core.preflight(reader, config, submission_request(values), Evaluator([config['fork'], config['upstream']]))
        write_snapshot(path, snapshot)
        summary({'action': 'preflight', 'reason': f"{snapshot['title']}; head {snapshot['head']}; base {snapshot['base']}"})
        metadata_summary(snapshot)
        return
    if command == 'publish':
        require(values.get('mode') == 'submit', 'Preflight cannot publish a pull request')
        snapshot = read_snapshot(path)
        publisher = API(required_token('NIXPKGS_PR_TOKEN'))
        result = core.publish(publisher, config, submission_request(values), snapshot)
        output(pr=result['pr'], head=result['head'])
        summary(result)
        return
    if command == 'review-evaluate':
        snapshot = core.review_snapshot(reader, config, values.get('pr', ''), values.get('attribute', ''),
                                        values.get('platform-scope', 'auto'), Evaluator([config['fork'], config['upstream']]),
                                        values.get('expected-head', ''))
        write_snapshot(path, snapshot)
        output(eligible='true' if snapshot['systems'] else 'false')
        summary({'action': 'evaluated', 'pr_url': snapshot['pr_url'], 'reason': ', '.join(snapshot['systems']) or 'No eligible systems'})
        metadata_summary(snapshot)
        return
    require(command == 'review-dispatch', 'Unknown workflow command')
    snapshot = read_snapshot(path)
    core.validate_review(snapshot)
    require(snapshot['config'] == config and snapshot['pr'] == values.get('pr') and
            snapshot['attribute'] == values.get('attribute') and snapshot['scope'] == values.get('platform-scope', 'auto'),
            'Review artifact does not match workflow inputs')
    if values.get('expected-head'):
        require(snapshot['head'] == values['expected-head'], 'Review artifact head does not match submission')
    force = values.get('force', False)
    require(type(force) is bool, 'force must be boolean')
    if not snapshot['systems']:
        summary(core.review(reader, None, None, snapshot, force=force))
        return
    runner = Runner(API(required_token('NIXPKGS_REVIEW_GHA_TOKEN')), config['runner'])
    store = ContentsStore(API(required_token('LEDGER_TOKEN')), os.environ['GITHUB_REPOSITORY'])
    store.initialize()
    summary(core.review(reader, runner, store, snapshot, force=force))


def reconcile_command(args):
    repository = core.validate_repo(args.repository)
    core.validate_pr_number(args.pr)
    store = ContentsStore(API(required_token('LEDGER_TOKEN')), repository)
    key = f'reviews/NixOS/nixpkgs/{args.pr}.json'
    if args.run_id:
        record, _ = store.read(key)
        require(record is not None and 0 <= args.attempt < len(record['attempts']), 'Attempt not found')
        attempt = record['attempts'][args.attempt]
        snapshot = attempt['snapshot']
        runner = Runner(API(required_token('NIXPKGS_REVIEW_GHA_TOKEN')), snapshot['config']['runner'])
        run = runner.get_run(args.run_id)
        url = f"https://github.com/{snapshot['config']['runner']}/actions/runs/{args.run_id}"
        core.validate_run(run, {'run_id': args.run_id, 'run_url': url}, snapshot['config']['runner'])
        # Attaching an operator-selected run requires independent artifact proof;
        # active runs do not establish the lost request's association/settings.
        require(run.get('status') == 'completed', 'Wait for the candidate run to finish before attaching it')
        core.report_outcome(runner.reports(args.run_id), snapshot)
        core.reconcile(store, key, args.attempt, reason=args.reason, run_id=args.run_id, run_url=url)
    else:
        core.reconcile(store, key, args.attempt, no_run=args.no_run, reason=args.reason)
    summary({'action': 'reconciled', 'reason': f'{key} attempt {args.attempt}'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('preflight', 'publish', 'review-evaluate', 'review-dispatch'):
        child = sub.add_parser(command)
        child.add_argument('--snapshot', required=True)
    recovery = sub.add_parser('reconcile', help='Explicit operator resolution; pause review dispatches first')
    recovery.add_argument('--repository', required=True)
    recovery.add_argument('--pr', required=True)
    recovery.add_argument('--attempt', required=True, type=int, help='Zero-based attempt index')
    recovery.add_argument('--reason', required=True)
    choice = recovery.add_mutually_exclusive_group(required=True)
    choice.add_argument('--no-run', action='store_true', help='Operator has proven that the dispatch created no run')
    choice.add_argument('--run-id', type=int)
    args = parser.parse_args()
    try:
        if args.command == 'reconcile':
            reconcile_command(args)
        else:
            execute(args.command, args.snapshot)
    except Error as exc:
        summary({'action': 'error', 'reason': str(exc)})
        return 1
    except (KeyError, ValueError, TypeError, OSError, subprocess.SubprocessError):
        # Avoid a traceback that might include an authentication-bearing request.
        summary({'action': 'error', 'reason': 'Invalid input or unavailable runtime; no automatic retry was attempted'})
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
