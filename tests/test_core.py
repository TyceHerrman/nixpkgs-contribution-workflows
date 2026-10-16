import copy
import unittest

try:
    from contribution import core as c
except ImportError:
    c = None

HEAD, BASE = 'a' * 40, 'b' * 40
SYSTEMS = ['x86_64-linux', 'aarch64-linux', 'x86_64-darwin', 'aarch64-darwin']


def pr(head=HEAD, base=BASE):
    return {'number': 123, 'state': 'open', 'html_url': 'https://github.com/NixOS/nixpkgs/pull/123',
            'head': {'sha': head, 'ref': 'update/pkg', 'repo': {'full_name': 'alice/nixpkgs'}},
            'base': {'sha': base, 'ref': 'master', 'repo': {'full_name': 'NixOS/nixpkgs'}}}


def metadata(version='2'):
    return {'version': version, 'systems': {s: {'eligible': True, 'reason': 'available'} for s in SYSTEMS}}


class PublicAPI:
    def __init__(self):
        self.current = pr()
        self.existing = []
        self.created = []
        self.reads = 0
        self.move_after_create = False
        self.find_queries = []

    def branch_sha(self, repo, branch):
        return self.current['head' if repo == 'alice/nixpkgs' else 'base']['sha']

    def get_pr(self, upstream, number):
        return copy.deepcopy(self.current)

    def find_prs(self, upstream, fork, branch, base):
        self.find_queries.append((upstream, fork, branch, base))
        return copy.deepcopy(self.existing)

    def create_pr(self, upstream, payload):
        self.created.append(copy.deepcopy(payload))
        if self.move_after_create:
            self.current['head']['sha'] = 'c' * 40
        return pr()


class Store:
    def __init__(self):
        self.records = {}
        self.revision = 0
        self.conflicts = 0
        self.fail_after = None
        self.writes = 0

    def read(self, key):
        return copy.deepcopy(self.records.get(key)), self.revision

    def cas(self, key, value, revision):
        self.writes += 1
        if self.fail_after is not None and self.writes > self.fail_after:
            raise OSError('storage unavailable')
        if self.conflicts:
            self.conflicts -= 1
            self.revision += 1
            raise c.Conflict('concurrent write')
        if revision != self.revision:
            raise c.Conflict('stale revision')
        self.records[key] = copy.deepcopy(value)
        self.revision += 1


class Runner:
    repository = 'alice/nixpkgs-review-gha'
    def __init__(self):
        self.posts = []
        self.runs = {}
        self.artifacts = {}
        self.reply = None
        self.error = None
        self.historical = {}
        self.lookups = []

    def for_repository(self, repository):
        return self if repository == self.repository else self.historical[repository]

    def dispatch(self, inputs):
        self.posts.append(copy.deepcopy(inputs))
        if self.error:
            raise self.error
        run_id = len(self.posts)
        self.runs[run_id] = {'id': run_id, 'status': 'queued', 'conclusion': None,
                             'repository': {'full_name': self.repository},
                             'html_url': f'https://github.com/{self.repository}/actions/runs/{run_id}'}
        return self.reply or {'workflow_run_id': run_id,
                             'run_url': f'https://api.github.com/repos/{self.repository}/actions/runs/{run_id}',
                             'html_url': f'https://github.com/{self.repository}/actions/runs/{run_id}'}

    def get_run(self, run_id):
        self.lookups.append(run_id)
        if run_id not in self.runs:
            raise c.Error(f'Run unavailable in {self.repository}')
        return copy.deepcopy(self.runs[run_id])

    def reports(self, run_id):
        return copy.deepcopy(self.artifacts.get(run_id))


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(c, 'the contribution core must be implemented')
        self.api, self.store, self.runner = PublicAPI(), Store(), Runner()
        self.request = {'branch': 'update/pkg', 'attribute': 'pkg', 'body': '## Change\n\nText  \n',
                        'title': '', 'draft': True}
        self.config = {'upstream': 'NixOS/nixpkgs', 'fork': 'alice/nixpkgs',
                       'runner': self.runner.repository, 'base_ref': 'master'}

    def snapshot(self):
        return c.review_snapshot(self.api, self.config, '123', 'pkg', 'auto',
                                 lambda sha, attr: metadata())

    def run_review(self, snapshot=None, force=False):
        return c.review(self.api, self.runner, self.store, snapshot or self.snapshot(), force=force)

    def test_invalid_branch_and_attribute_are_rejected_before_lookup(self):
        for branch in ['main..evil', '-bad', 'refs/heads/a', 'a.lock', 'a\nvalue', 'a@{x}', 'a//b']:
            with self.subTest(branch=branch), self.assertRaises(c.Error):
                c.validate_branch(branch)
        for attr in ['', 'x;builtins.abort', 'a..b', 'foo.${bar}', 'x\ny']:
            with self.subTest(attr=attr), self.assertRaises(c.Error):
                c.validate_attribute(attr)

    def test_preflight_requires_changed_version_and_preserves_body(self):
        evaluated = []
        def evaluate(sha, attr):
            evaluated.append(sha)
            return metadata('1' if sha == BASE else '2')
        snap = c.preflight(self.api, self.config, self.request, evaluate)
        self.assertEqual(set(evaluated), {HEAD, BASE})
        self.assertEqual(snap['title'], 'pkg: 1 -> 2')
        self.assertEqual(snap['body'], self.request['body'])
        self.assertEqual(self.api.created, [])
        with self.assertRaises(c.Error):
            c.preflight(self.api, self.config, self.request, lambda *_: metadata())

    def test_preflight_previews_draft_creation_and_complete_runner_payload_without_writes(self):
        snapshot = c.preflight(self.api, self.config, self.request,
                               lambda sha, _: metadata('1' if sha == BASE else '2'))
        self.assertIn('preview', snapshot, 'preflight must include the provisional action plan')
        preview = snapshot['preview']
        self.assertIs(preview['provisional'], True)
        self.assertEqual(preview['publication']['action'], 'create')
        self.assertEqual(preview['publication']['target'], {'upstream': 'NixOS/nixpkgs', 'base': 'master',
                                                          'fork': 'alice/nixpkgs', 'branch': 'update/pkg'})
        self.assertEqual(preview['publication']['effective'], {'title': 'pkg: 1 -> 2', 'body': self.request['body'], 'draft': True})
        self.assertEqual(preview['review']['repository'], 'alice/nixpkgs-review-gha')
        self.assertEqual(preview['review']['action'], 'request')
        self.assertEqual(preview['review']['inputs'], {
            'pr': '<new PR number>', 'x86_64-linux': 'true', 'aarch64-linux': 'true',
            'x86_64-darwin': 'yes_sandbox_relaxed', 'aarch64-darwin': 'yes_sandbox_relaxed',
            'riscv64-linux': 'false', 'builders': 'gha', 'push-to-cache': 'true',
            'post-result': 'true', 'upterm': 'false', 'on-success': 'nothing', 'extra-args': ''})
        self.assertEqual(self.api.find_queries, [('NixOS/nixpkgs', 'alice/nixpkgs', 'update/pkg', 'master')])
        self.assertEqual(self.api.created, [])
        self.assertEqual(self.runner.posts, [])

    def test_preflight_previews_exact_pr_reuse_and_preserved_edits(self):
        existing = pr() | {'title': 'Manual title', 'body': 'Edited body\n', 'draft': False}
        decoy = copy.deepcopy(existing)
        decoy['head']['repo']['full_name'] = 'someone/nixpkgs'
        self.api.existing = [decoy, existing]
        snapshot = c.preflight(self.api, self.config, self.request,
                               lambda sha, _: metadata('1' if sha == BASE else '2'))
        self.assertIn('preview', snapshot, 'preflight must discover current exact PR reuse')
        publication = snapshot['preview']['publication']
        self.assertEqual(publication['action'], 'reuse')
        self.assertEqual(publication['pr_url'], existing['html_url'])
        self.assertEqual(publication['effective'], {'title': 'Manual title', 'body': 'Edited body\n', 'draft': False})
        self.assertEqual(publication['supplied']['body'], self.request['body'])
        self.assertIs(publication['supplied']['draft'], True)
        self.assertIn('preserve', publication['handling'])
        self.assertEqual(snapshot['preview']['review']['inputs']['pr'], '123')
        self.assertEqual(self.api.created, [])

    def test_preflight_previews_explicit_no_platform_review_skip(self):
        def unavailable(sha, _):
            data = metadata('1' if sha == BASE else '2')
            for row in data['systems'].values():
                row.update(eligible=False, reason='meta.broken')
            return data
        snapshot = c.preflight(self.api, self.config, self.request, unavailable)
        self.assertIn('preview', snapshot, 'preflight must report a zero-platform skip')
        review = snapshot['preview']['review']
        self.assertEqual(review['action'], 'skip')
        self.assertEqual(review['systems'], [])
        self.assertIsNone(review['inputs'])
        self.assertIn('No eligible', review['reason'])
        self.assertEqual(snapshot['preview']['publication']['action'], 'create')
        self.assertEqual(self.api.created, [])

    def test_publish_draft_and_reuse_preserves_manual_edits(self):
        snap = c.preflight(self.api, self.config, self.request,
                           lambda sha, _: metadata('1' if sha == BASE else '2'))
        result = c.publish(self.api, self.config, self.request, snap)
        self.assertEqual(result['action'], 'created')
        self.assertEqual(self.api.created[0]['body'], self.request['body'])
        self.assertIs(self.api.created[0]['draft'], True)
        self.api.existing = [pr() | {'title': 'edited', 'body': 'edited', 'draft': False}]
        self.assertEqual(c.publish(self.api, self.config, self.request, snap)['action'], 'reused')
        self.assertEqual(len(self.api.created), 1)

    def test_publish_rejects_tampering_and_head_movement(self):
        snap = c.preflight(self.api, self.config, self.request,
                           lambda sha, _: metadata('1' if sha == BASE else '2'))
        for field, value in [('head', 'z' * 40), ('schema', 2), ('body', 'changed')]:
            altered = copy.deepcopy(snap)
            altered[field] = value
            with self.subTest(field=field), self.assertRaises(c.Error):
                c.publish(self.api, self.config, self.request, altered)
        self.api.current['head']['sha'] = 'd' * 40
        with self.assertRaises(c.Error):
            c.publish(self.api, self.config, self.request, snap)
        self.assertEqual(self.api.created, [])
        self.api.current = pr()
        self.api.move_after_create = True
        with self.assertRaises(c.Error):
            c.publish(self.api, self.config, self.request, snap)
        self.assertEqual(len(self.api.created), 1)

    def test_fingerprint_covers_snapshot_and_settings_with_stable_system_order(self):
        snap = self.snapshot()
        other = copy.deepcopy(snap)
        other['systems'].reverse()
        self.assertEqual(c.fingerprint(snap), c.fingerprint(other))
        for field, value in [('head', 'd' * 40), ('base', 'e' * 40), ('systems', ['x86_64-linux']),
                             ('settings', dict(snap['settings'], builders='remote'))]:
            other = copy.deepcopy(snap)
            other[field] = value
            self.assertNotEqual(c.fingerprint(snap), c.fingerprint(other))

    def test_scope_intersection_and_empty_review(self):
        snap = c.review_snapshot(self.api, self.config, '123', 'pkg', 'darwin', lambda *_: metadata())
        self.assertEqual(snap['systems'], ['aarch64-darwin', 'x86_64-darwin'])
        snap['systems'] = []
        for row in snap['metadata']['systems'].values():
            row.update(eligible=False, reason='meta.broken')
        self.assertEqual(self.run_review(snap)['action'], 'skipped')
        self.assertEqual(self.runner.posts, [])

    def test_review_refuses_wrong_fork_closed_pr_and_changed_snapshot(self):
        for alteration in [lambda p: p.update(state='closed'),
                           lambda p: p['head']['repo'].update(full_name='evil/nixpkgs')]:
            alteration(self.api.current)
            with self.assertRaises(c.Error):
                self.snapshot()
            self.api.current = pr()
        snap = self.snapshot()
        self.api.current['base']['sha'] = 'f' * 40
        with self.assertRaises(c.Error):
            self.run_review(snap)
        self.assertEqual(self.runner.posts, [])

    def test_dispatch_payload_and_reuse_active_run(self):
        first = self.run_review()
        self.assertEqual(first['action'], 'dispatched')
        self.assertEqual(self.runner.posts[0], {
            'pr': '123', 'x86_64-linux': 'true', 'aarch64-linux': 'true',
            'x86_64-darwin': 'yes_sandbox_relaxed', 'aarch64-darwin': 'yes_sandbox_relaxed',
            'riscv64-linux': 'false', 'builders': 'gha', 'push-to-cache': 'true',
            'post-result': 'true', 'upterm': 'false', 'on-success': 'nothing', 'extra-args': ''})
        self.assertEqual(self.run_review()['action'], 'reused')
        self.assertEqual(len(self.runner.posts), 1)

    def test_failed_cancelled_timed_out_retry_and_force_preserve_history(self):
        for conclusion in ['failure', 'cancelled', 'timed_out']:
            self.store, self.runner = Store(), Runner()
            self.run_review(force=True)
            run_id = len(self.runner.posts)
            self.runner.runs[run_id].update(status='completed', conclusion=conclusion)
            self.assertEqual(self.run_review()['action'], 'dispatched')
        before = len(self.runner.posts)
        self.assertEqual(self.run_review(force=True)['action'], 'dispatched')
        attempts = next(iter(self.store.records.values()))['attempts']
        self.assertEqual(len(attempts), before + 1)

    def test_failed_forced_attempt_does_not_hide_older_active_matching_run(self):
        first = self.run_review()
        self.run_review(force=True)
        self.runner.runs[2].update(status='completed', conclusion='failure')
        result = self.run_review()
        self.assertEqual(result['action'], 'reused')
        self.assertEqual(result['run_url'], first['run_url'])
        self.assertEqual(result['coverage'], 'pending')
        self.assertEqual(len(self.runner.posts), 2)

    def test_failed_forced_attempt_does_not_hide_older_verified_success(self):
        self.complete_run(self.successful_reports())
        self.run_review(force=True)
        self.runner.runs[2].update(status='completed', conclusion='failure')
        result = self.run_review()
        self.assertEqual(result['action'], 'reused')
        self.assertEqual(result['run_url'], 'https://github.com/alice/nixpkgs-review-gha/actions/runs/1')
        self.assertEqual(result['coverage'], 'verified')
        self.assertEqual(len(self.runner.posts), 2)

    def test_intent_written_before_post_and_uncertainty_blocks_even_force(self):
        self.runner.error = TimeoutError('response lost')
        with self.assertRaises(c.Error):
            self.run_review()
        self.assertEqual(next(iter(self.store.records.values()))['attempts'][0]['state'], 'needs-reconciliation')
        self.runner.error = None
        with self.assertRaises(c.Error):
            self.run_review(force=True)
        self.assertEqual(len(self.runner.posts), 1)

    def test_failure_saving_run_leaves_blocking_intent(self):
        self.store.fail_after = 1
        with self.assertRaises(c.Error):
            self.run_review()
        self.store.fail_after = None
        with self.assertRaises(c.Error):
            self.run_review(force=True)
        self.assertEqual(len(self.runner.posts), 1)

    def test_malformed_dispatch_reply_never_claims_dispatch(self):
        for reply in [{}, {'workflow_run_id': True},
                      {'workflow_run_id': 1, 'run_url': 'https://evil/runs/1', 'html_url': 'https://evil/1'},
                      {'workflow_run_id': 1, 'run_url': 'https://api.github.com/repos/alice/nixpkgs-review-gha/actions/runs/2',
                       'html_url': 'https://github.com/alice/nixpkgs-review-gha/actions/runs/1'}]:
            self.store = Store()
            self.runner = Runner()
            self.runner.reply = reply
            # Empty reply is represented explicitly by a dispatcher returning None.
            if not reply:
                self.runner.dispatch = lambda _: None
            with self.subTest(reply=reply), self.assertRaises(c.Error):
                self.run_review()

    def successful_reports(self):
        return [{'head': HEAD, 'base': BASE, 'system': s, 'nixConfig': {'sandbox': 'relaxed' if s.endswith('darwin') else 'true'},
                 'result': {'failed': [], 'still_failing': [], 'built': [], 'tests': [], 'broken': [],
                            'non_existent': [], 'blacklisted': [], 'unsupported': []}} for s in SYSTEMS]

    def complete_run(self, reports):
        self.run_review()
        self.runner.runs[1].update(status='completed', conclusion='success')
        self.runner.artifacts[1] = reports

    def test_success_requires_reports_and_actual_matching_snapshot(self):
        for mutate in [lambda x: None, lambda x: [], lambda x: x[:-1],
                       lambda x: [dict(r, head='f' * 40) for r in x],
                       lambda x: [dict(r, base='f' * 40) for r in x],
                       lambda x: [dict(r, nixConfig={'sandbox': 'false'}) for r in x]]:
            self.store, self.runner = Store(), Runner()
            self.complete_run(mutate(self.successful_reports()))
            with self.subTest(mutate=mutate), self.assertRaises(c.Error):
                self.run_review()
            self.assertEqual(len(self.runner.posts), 1)
        self.store, self.runner = Store(), Runner()
        self.complete_run(self.successful_reports())
        self.assertEqual(self.run_review()['action'], 'reused')
        self.assertEqual(self.run_review()['coverage'], 'verified')

    def test_green_workflow_with_failed_builds_is_retryable(self):
        reports = self.successful_reports()
        reports[0]['result']['failed'] = [{'name': 'pkg', 'aliases': []}]
        self.complete_run(reports)
        self.assertEqual(self.run_review()['action'], 'dispatched')

    def test_cas_conflicts_retry_without_duplicate_dispatch(self):
        self.store.conflicts = 2
        self.assertEqual(self.run_review()['action'], 'dispatched')
        self.assertEqual(len(self.runner.posts), 1)
        self.store, self.runner = Store(), Runner()
        self.store.conflicts = 99
        with self.assertRaises(c.Error):
            self.run_review()
        self.assertEqual(self.runner.posts, [])

    def test_active_different_snapshot_blocks_until_runner_finishes(self):
        self.run_review()
        self.api.current['head']['sha'] = 'c' * 40
        for force in (False, True):
            with self.subTest(force=force), self.assertRaises(c.Error):
                self.run_review(force=force)
        self.assertEqual(len(self.runner.posts), 1)
        self.runner.runs[1].update(status='completed', conclusion='failure')
        self.assertEqual(self.run_review()['action'], 'dispatched')

    def test_runner_override_resolves_history_in_recorded_repository(self):
        self.run_review()
        original = self.runner
        replacement = Runner()
        replacement.repository = 'alice/new-review-runner'
        replacement.historical[original.repository] = original
        self.runner = replacement
        self.config['runner'] = replacement.repository
        with self.assertRaises(c.Error):
            self.run_review()
        self.assertEqual(replacement.posts, [])
        original.runs[1].update(status='completed', conclusion='failure')
        self.assertEqual(self.run_review()['action'], 'dispatched')
        self.assertEqual(original.lookups, [1, 1])
        self.assertEqual(replacement.lookups, [])

    def test_unavailable_old_runner_blocks_until_verified_terminal_retirement(self):
        self.run_review()
        original = self.runner
        snapshot = self.snapshot()
        replacement = Runner()
        replacement.repository = 'alice/new-review-runner'
        replacement.historical[original.repository] = Runner()  # No access to old run ID.
        self.runner = replacement
        self.config['runner'] = replacement.repository
        with self.assertRaisesRegex(c.Error, 'retire-run'):
            self.run_review(force=True)
        self.assertEqual(replacement.posts, [])
        key = c.record_key(snapshot)
        self.assertTrue(hasattr(c, 'retire_run'), 'explicit verified retirement must be implemented')
        with self.assertRaises(c.Error):
            c.retire_run(self.store, key, 0, original.runs[1], reason='Migration')
        self.assertEqual(self.store.records[key]['attempts'][0]['state'], 'dispatched')
        terminal = dict(original.runs[1], status='completed', conclusion='cancelled')
        c.retire_run(self.store, key, 0, terminal, reason='Old runner is paused; cancelled run is terminal')
        saved = self.store.records[key]['attempts'][0]
        self.assertEqual(saved['state'], 'retired')
        self.assertEqual(saved['run_id'], 1)
        self.assertEqual(saved['snapshot']['config']['runner'], original.repository)
        self.assertEqual(saved['terminal_evidence']['conclusion'], 'cancelled')
        self.assertEqual(self.run_review()['action'], 'dispatched')
        self.assertEqual(len(self.store.records[key]['attempts']), 2)

    def test_metadata_selection_cannot_be_widened_by_artifact(self):
        snap = self.snapshot()
        snap['scope'] = 'darwin'
        with self.assertRaises(c.Error):
            self.run_review(snap)

    def test_publication_rechecks_pr_identity_after_create(self):
        snapshot = c.preflight(self.api, self.config, self.request,
                               lambda sha, _: metadata('1' if sha == BASE else '2'))
        self.api.current['state'] = 'closed'
        with self.assertRaises(c.Error):
            c.publish(self.api, self.config, self.request, snapshot)

    def test_head_movement_during_ledger_work_stops_before_external_post(self):
        snap = self.snapshot()
        original_cas = self.store.cas
        def moving_cas(key, value, revision):
            original_cas(key, value, revision)
            self.api.current['head']['sha'] = 'c' * 40
        self.store.cas = moving_cas
        with self.assertRaises(c.Error):
            self.run_review(snap)
        self.assertEqual(self.runner.posts, [])
        self.assertEqual(next(iter(self.store.records.values()))['attempts'][0]['state'], 'not-dispatched')

    def test_reconcile_attaches_known_run_or_proven_no_run_and_preserves_intent(self):
        self.runner.error = TimeoutError()
        with self.assertRaises(c.Error):
            self.run_review()
        snap = self.snapshot()
        key = c.record_key(snap)
        c.reconcile(self.store, key, 0, no_run=True, reason='operator verified no target run exists')
        self.runner.error = None
        self.assertEqual(self.run_review()['action'], 'dispatched')


if __name__ == '__main__':
    unittest.main()
