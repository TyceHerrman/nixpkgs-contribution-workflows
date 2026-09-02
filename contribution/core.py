"""Pure orchestration; external effects are injected API clients.

All dispatch callers must hold the installation repository's per-PR Actions
concurrency group. Contents CAS additionally protects independent record writes.
"""

import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

SYSTEMS = ('x86_64-linux', 'aarch64-linux', 'x86_64-darwin', 'aarch64-darwin')
SETTINGS = {'riscv64-linux': 'false', 'builders': 'gha', 'push-to-cache': 'true',
            'post-result': 'true', 'upterm': 'false', 'on-success': 'nothing', 'extra-args': ''}
ACTIVE = {'queued', 'in_progress', 'waiting', 'pending', 'requested'}
RETRYABLE = {'failure', 'cancelled', 'timed_out', 'startup_failure', 'action_required', 'stale'}


class Error(Exception):
    """A safe, user-facing validation or coordination failure."""


class Conflict(Error):
    """A GitHub Contents compare-and-swap conflict."""


def require(condition, message):
    if not condition:
        raise Error(message)


def validate_repo(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value), 'Invalid repository')
    return value


def validate_sha(value):
    require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40}', value), 'Invalid commit SHA')
    return value


def validate_branch(value):
    require(isinstance(value, str) and 0 < len(value) <= 240, 'Invalid branch')
    require(re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_./-]*', value) and not value.startswith('refs/'), 'Invalid branch')
    require('..' not in value and all(p and not p.startswith('.') and not p.endswith(('.', '.lock'))
                                    for p in value.split('/')), 'Invalid branch')
    return value


def validate_attribute(value):
    require(isinstance(value, str) and len(value) <= 300 and
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'-]*(\.[A-Za-z_][A-Za-z0-9_'-]*)*", value), 'Invalid attribute path')
    return value


def validate_pr_number(value):
    require(isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,9}', value), 'PR must be a positive decimal number')
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate_config(config):
    require(set(config) == {'fork', 'upstream', 'runner', 'base_ref'}, 'Invalid repository configuration')
    for field in ('fork', 'upstream', 'runner'):
        validate_repo(config[field])
    require(config['upstream'] == 'NixOS/nixpkgs' and config['base_ref'] == 'master', 'Only NixOS/nixpkgs master is supported')


def validate_request(request):
    require(set(request) == {'branch', 'attribute', 'title', 'body', 'draft'}, 'Invalid submission request')
    validate_branch(request['branch'])
    validate_attribute(request['attribute'])
    require(type(request['draft']) is bool, 'draft must be boolean')
    require(isinstance(request['body'], str) and 0 < len(request['body']) <= 65536, 'A PR body is required (maximum 65536 characters)')
    require(isinstance(request['title'], str) and len(request['title']) <= 256 and
            '\n' not in request['title'] and '\r' not in request['title'], 'Invalid PR title')


def validate_metadata(value):
    require(isinstance(value, dict) and set(value) == {'version', 'systems'}, 'Invalid metadata schema')
    require(isinstance(value['version'], str) and 0 < len(value['version']) <= 200 and
            not any(ord(ch) < 32 for ch in value['version']), 'Package version must be a nonempty printable string')
    require(isinstance(value['systems'], dict) and set(value['systems']) == set(SYSTEMS), 'Incomplete system metadata')
    for row in value['systems'].values():
        require(isinstance(row, dict) and set(row) == {'eligible', 'reason'} and type(row['eligible']) is bool
                and isinstance(row['reason'], str) and 0 < len(row['reason']) <= 200, 'Invalid system metadata')
    return value


def preflight(api, config, request, evaluate):
    validate_config(config)
    validate_request(request)
    head = validate_sha(api.branch_sha(config['fork'], request['branch']))
    base = validate_sha(api.branch_sha(config['upstream'], config['base_ref']))
    old = validate_metadata(evaluate(base, request['attribute']))
    new = validate_metadata(evaluate(head, request['attribute']))
    require(old['version'] != new['version'], 'No version change; PR submission requires a version update')
    return {'schema': 1, 'kind': 'submission', 'config': copy.deepcopy(config),
            'request_digest': digest(request), 'branch': request['branch'], 'attribute': request['attribute'],
            'head': head, 'base': base, 'old': old, 'new': new, 'body': request['body'],
            'draft': request['draft'], 'title': request['title'] or f"{request['attribute']}: {old['version']} -> {new['version']}"}


def validate_submission(snapshot, config, request):
    validate_config(config)
    validate_request(request)
    require(isinstance(snapshot, dict) and set(snapshot) == {
        'schema', 'kind', 'config', 'request_digest', 'branch', 'attribute', 'head', 'base', 'old', 'new', 'body', 'draft', 'title'},
        'Invalid preflight artifact schema')
    require(type(snapshot['schema']) is int and snapshot['schema'] == 1 and snapshot['kind'] == 'submission', 'Unknown preflight schema')
    require(snapshot['config'] == config and snapshot['request_digest'] == digest(request), 'Preflight request/configuration mismatch')
    validate_sha(snapshot['head'])
    validate_sha(snapshot['base'])
    validate_metadata(snapshot['old'])
    validate_metadata(snapshot['new'])
    require(snapshot['old']['version'] != snapshot['new']['version'], 'No version change')
    for field in ('branch', 'attribute', 'body', 'draft'):
        require(snapshot[field] == request[field], f'Preflight {field} mismatch')
    title = request['title'] or f"{request['attribute']}: {snapshot['old']['version']} -> {snapshot['new']['version']}"
    require(snapshot['title'] == title and len(title) <= 256, 'Preflight title mismatch or too long')


def validate_pr(value, config, number=None):
    require(isinstance(value, dict) and value.get('state') == 'open', 'PR must remain open')
    require(value.get('head', {}).get('repo', {}).get('full_name', '').lower() == config['fork'].lower(), 'PR does not belong to the configured fork')
    require(value.get('base', {}).get('repo', {}).get('full_name') == config['upstream'] and
            value['base']['ref'] == config['base_ref'], 'PR does not target NixOS/nixpkgs master')
    validate_sha(value['head']['sha'])
    validate_sha(value['base']['sha'])
    require(type(value.get('number')) is int and value['number'] > 0, 'Invalid PR number')
    require(number is None or str(value['number']) == str(number), 'PR number mismatch')
    require(value.get('html_url') == f"https://github.com/{config['upstream']}/pull/{value['number']}", 'Invalid PR URL')
    return value


def publish(api, config, request, snapshot):
    validate_submission(snapshot, config, request)
    require(api.branch_sha(config['fork'], request['branch']) == snapshot['head'], 'Fork branch moved after preflight; rerun submission')
    require(api.branch_sha(config['upstream'], config['base_ref']) == snapshot['base'], 'Upstream master moved after preflight; rerun submission')
    existing = api.find_prs(config['upstream'], config['fork'], request['branch'], config['base_ref'])
    exact = [p for p in existing if p.get('head', {}).get('repo', {}).get('full_name', '').lower() == config['fork'].lower()
             and p.get('head', {}).get('ref') == request['branch'] and p.get('base', {}).get('ref') == config['base_ref']
             and p.get('state') == 'open']
    require(len(exact) <= 1, 'Multiple matching open PRs; resolve manually')
    if exact:
        result, action = exact[0], 'reused'
    else:
        result = api.create_pr(config['upstream'], {'head': f"{config['fork'].split('/')[0]}:{request['branch']}",
                                                    'head_repo': config['fork'].split('/')[1], 'base': config['base_ref'],
                                                    'title': snapshot['title'], 'body': snapshot['body'], 'draft': snapshot['draft']})
        action = 'created'
    validate_pr(result, config)
    latest = validate_pr(api.get_pr(config['upstream'], result['number']), config, result['number'])
    require(api.branch_sha(config['fork'], request['branch']) == snapshot['head'] and latest['head']['sha'] == snapshot['head'],
            f"Fork branch moved during publication; PR {result['html_url']} exists, but no review was started. Rerun preflight.")
    return {'action': action, 'pr': str(result['number']), 'pr_url': result['html_url'], 'head': snapshot['head']}


def review_snapshot(api, config, number, attribute, scope, evaluate, expected_head=''):
    validate_config(config)
    validate_pr_number(number)
    validate_attribute(attribute)
    require(scope in {'auto', 'darwin'}, 'platform-scope must be auto or darwin')
    value = validate_pr(api.get_pr(config['upstream'], number), config, number)
    if expected_head:
        require(validate_sha(expected_head) == value['head']['sha'], 'PR head changed since submission')
    metadata = validate_metadata(evaluate(value['head']['sha'], attribute))
    selected = sorted(s for s, row in metadata['systems'].items() if row['eligible'] and (scope == 'auto' or s.endswith('-darwin')))
    return {'schema': 1, 'kind': 'review', 'config': copy.deepcopy(config), 'pr': number, 'pr_url': value['html_url'],
            'head': value['head']['sha'], 'base': value['base']['sha'], 'attribute': attribute, 'scope': scope,
            'metadata': metadata, 'systems': selected, 'settings': dict(SETTINGS)}


def validate_review(snapshot):
    require(isinstance(snapshot, dict) and set(snapshot) == {'schema', 'kind', 'config', 'pr', 'pr_url', 'head', 'base',
                                                           'attribute', 'scope', 'metadata', 'systems', 'settings'}, 'Invalid review artifact schema')
    require(type(snapshot['schema']) is int and snapshot['schema'] == 1 and snapshot['kind'] == 'review', 'Unknown review artifact schema')
    validate_config(snapshot['config'])
    validate_pr_number(snapshot['pr'])
    validate_attribute(snapshot['attribute'])
    validate_sha(snapshot['head'])
    validate_sha(snapshot['base'])
    require(snapshot['scope'] in {'auto', 'darwin'}, 'Invalid scope')
    require(isinstance(snapshot['systems'], list) and len(set(snapshot['systems'])) == len(snapshot['systems'])
            and set(snapshot['systems']) <= set(SYSTEMS), 'Invalid selected systems')
    require(snapshot['settings'] == SETTINGS, 'Unsupported runner settings')
    validate_metadata(snapshot['metadata'])
    selected = sorted(s for s, row in snapshot['metadata']['systems'].items() if row['eligible'] and
                      (snapshot['scope'] == 'auto' or s.endswith('-darwin')))
    require(sorted(snapshot['systems']) == selected, 'Selected systems do not match evaluated metadata and scope')
    require(snapshot['pr_url'] == f"https://github.com/{snapshot['config']['upstream']}/pull/{snapshot['pr']}", 'Invalid PR URL')


def fingerprint(snapshot):
    return digest({'upstream': snapshot['config']['upstream'], 'runner': snapshot['config']['runner'],
                   'pr': snapshot['pr'], 'head': snapshot['head'], 'base': snapshot['base'],
                   'systems': sorted(snapshot['systems']), 'settings': snapshot['settings']})


def record_key(snapshot):
    return f"reviews/{snapshot['config']['upstream']}/{snapshot['pr']}.json"


def dispatch_inputs(snapshot):
    return {'pr': snapshot['pr'], **snapshot['settings'],
            **{s: ('yes_sandbox_relaxed' if s.endswith('-darwin') else 'true') if s in snapshot['systems']
               else ('no' if s.endswith('-darwin') else 'false') for s in SYSTEMS}}


def validate_dispatch(reply, repository):
    require(isinstance(reply, dict) and type(reply.get('workflow_run_id')) is int and reply['workflow_run_id'] > 0,
            'Dispatch reply did not identify a run; reconciliation required')
    run_id = reply['workflow_run_id']
    require(reply.get('run_url') == f'https://api.github.com/repos/{repository}/actions/runs/{run_id}' and
            reply.get('html_url') == f'https://github.com/{repository}/actions/runs/{run_id}', 'Dispatch returned an unexpected run identity; reconciliation required')
    return run_id, reply['html_url']


def validate_run(run, attempt, repository):
    require(isinstance(run, dict) and type(run.get('id')) is int and run['id'] == attempt['run_id'] and
            run.get('repository', {}).get('full_name', '').lower() == repository.lower() and
            run.get('html_url') == attempt['run_url'], 'Recorded run identity does not match the target repository')


def report_outcome(reports, snapshot):
    require(isinstance(reports, list) and len(reports) == len(snapshot['systems']), 'Missing or incomplete reports.json; coverage unverified')
    seen = set()
    failed = False
    for report in reports:
        require(isinstance(report, dict), 'Invalid reports.json')
        system = report.get('system')
        require(system in snapshot['systems'] and system not in seen, 'Report system mismatch; coverage unverified')
        seen.add(system)
        require(report.get('head') == snapshot['head'] and report.get('base') == snapshot['base'],
                'Runner prepared a different head/base snapshot; coverage unverified')
        require(report.get('nixConfig', {}).get('sandbox') == ('relaxed' if system.endswith('-darwin') else 'true'),
                'Runner sandbox setting mismatch; coverage unverified')
        result = report.get('result')
        require(isinstance(result, dict) and all(isinstance(result.get(k), list) for k in
                ('failed', 'still_failing', 'built', 'tests', 'broken', 'non_existent', 'blacklisted', 'unsupported')), 'Invalid build result schema')
        failed = failed or bool(result['failed'] or result['still_failing'])
    return 'failed' if failed else 'success'


def update_record(store, key, change):
    for _ in range(5):
        value, revision = store.read(key)
        value = value or {'schema': 1, 'attempts': []}
        require(value.get('schema') == 1 and isinstance(value.get('attempts'), list), 'Invalid ledger record')
        new = change(copy.deepcopy(value))
        try:
            store.cas(key, new, revision)
            return new
        except Conflict:
            continue
    raise Error('Ledger changed repeatedly; no dispatch performed by this attempt')


def review(api, runner, store, snapshot, force=False):
    validate_review(snapshot)
    require(type(force) is bool, 'force must be boolean')
    current = validate_pr(api.get_pr(snapshot['config']['upstream'], snapshot['pr']), snapshot['config'], snapshot['pr'])
    require(current['head']['sha'] == snapshot['head'] and current['base']['sha'] == snapshot['base'],
            'PR head/base changed after evaluation; rerun review')
    if not snapshot['systems']:
        return {'action': 'skipped', 'pr_url': snapshot['pr_url'], 'reason': 'No eligible systems in the requested scope', 'coverage': 'none'}
    key, fp = record_key(snapshot), fingerprint(snapshot)
    record, _ = store.read(key)
    attempts = (record or {}).get('attempts', [])
    require(not any(a.get('state') in {'intent', 'needs-reconciliation'} for a in attempts),
            f'An earlier dispatch needs reconciliation in {key}; force cannot override uncertainty')
    for prior in attempts:
        if prior.get('state') == 'dispatched' and prior.get('fingerprint') != fp:
            run = runner.get_run(prior['run_id'])
            validate_run(run, prior, snapshot['config']['runner'])
            require(run.get('status') == 'completed',
                    f"A different snapshot is still active or unknown: {prior['run_url']}. Resolve it before dispatching this snapshot.")
    matching = [a for a in attempts if a.get('fingerprint') == fp and a.get('state') == 'dispatched']
    if matching:
        attempt = matching[-1]
        require(attempt.get('inputs') == dispatch_inputs(snapshot), 'Recorded dispatch settings mismatch')
        run = runner.get_run(attempt['run_id'])
        validate_run(run, attempt, snapshot['config']['runner'])
        if not force:
            if run.get('status') in ACTIVE:
                return {'action': 'reused', 'pr_url': snapshot['pr_url'], 'run_url': attempt['run_url'], 'coverage': 'pending'}
            require(run.get('status') == 'completed', 'Unknown run status; inspect before retrying')
            if run.get('conclusion') == 'success':
                if report_outcome(runner.reports(attempt['run_id']), snapshot) == 'success':
                    return {'action': 'reused', 'pr_url': snapshot['pr_url'], 'run_url': attempt['run_url'], 'coverage': 'verified'}
            else:
                require(run.get('conclusion') in RETRYABLE, 'Unknown terminal run conclusion; inspect or use force')
    attempt_id = str(uuid.uuid4())
    attempt = {'id': attempt_id, 'state': 'intent', 'fingerprint': fp, 'snapshot': copy.deepcopy(snapshot),
               'inputs': dispatch_inputs(snapshot), 'created_at': datetime.now(timezone.utc).isoformat()}

    def add_intent(value):
        require(not any(a.get('state') in {'intent', 'needs-reconciliation'} for a in value['attempts']), 'Another dispatch needs reconciliation')
        # A competing completed write is not an independent-record conflict.
        require(value['attempts'] == attempts, 'PR ledger changed; rerun to reconcile the latest attempt')
        value['attempts'].append(attempt)
        return value

    update_record(store, key, add_intent)

    def finish(value, state, **fields):
        entry = next((a for a in value['attempts'] if a['id'] == attempt_id), None)
        require(entry is not None and entry['state'] in {'intent', 'needs-reconciliation'}, 'Dispatch intent changed unexpectedly')
        entry.update(state=state, **fields)
        return value

    try:
        latest = validate_pr(api.get_pr(snapshot['config']['upstream'], snapshot['pr']), snapshot['config'], snapshot['pr'])
        require(latest['head']['sha'] == snapshot['head'] and latest['base']['sha'] == snapshot['base'],
                'PR head/base changed during ledger reconciliation; no review was dispatched')
    except Exception as exc:
        # No external POST has happened, so this intent can be resolved without
        # an operator. If this write fails, retain the conservative blocking intent.
        try:
            update_record(store, key, lambda v: finish(v, 'not-dispatched', reconciliation='Snapshot recheck failed before POST'))
        except Exception:
            raise Error(f'Pre-dispatch snapshot check failed and intent could not be resolved in {key}') from exc
        raise Error('PR snapshot could not be reconfirmed; no review was dispatched') from exc

    try:
        reply = runner.dispatch(attempt['inputs'])
        run_id, run_url = validate_dispatch(reply, snapshot['config']['runner'])
        update_record(store, key, lambda v: finish(v, 'dispatched', run_id=run_id, run_url=run_url))
    except Exception as exc:
        # No exception from a dispatch POST authorizes retry: an HTTP response can
        # be lost after the server commits its side effect.
        try:
            update_record(store, key, lambda v: finish(v, 'needs-reconciliation'))
        except Exception:
            pass  # Durable pre-dispatch intent still blocks all automatic retry.
        raise Error(f'Dispatch outcome needs reconciliation in {key}; no automatic retry was made') from exc
    return {'action': 'dispatched', 'pr_url': snapshot['pr_url'], 'run_url': run_url, 'coverage': 'pending'}


def reconcile(store, key, index, *, no_run=False, reason='', run_id=None, run_url=None):
    """Operator-only resolution. The CLI verifies attached run identity first."""
    require(reason.strip(), 'A reconciliation explanation is required')
    require(no_run != (run_id is not None), 'Choose a known run or explicitly confirm no run exists')

    def change(value):
        require(type(index) is int and 0 <= index < len(value['attempts']), 'Invalid attempt index')
        attempt = value['attempts'][index]
        require(attempt['state'] in {'intent', 'needs-reconciliation'}, 'Attempt is already resolved')
        attempt.update(state='not-dispatched' if no_run else 'dispatched', reconciliation=reason)
        if not no_run:
            reply = {'workflow_run_id': run_id, 'run_url': f"https://api.github.com/repos/{attempt['snapshot']['config']['runner']}/actions/runs/{run_id}", 'html_url': run_url}
            validate_dispatch(reply, attempt['snapshot']['config']['runner'])
            attempt.update(run_id=run_id, run_url=run_url)
        return value

    return update_record(store, key, change)
