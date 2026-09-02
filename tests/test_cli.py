import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from test_core import BASE, Store, Runner, PublicAPI, metadata, pr

try:
    from contribution import cli
except ImportError:
    cli = None


class CLITests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(cli, 'CLI must be implemented')
        self.env = {'GITHUB_REPOSITORY': 'alice/nixpkgs-contribution-workflows',
                    'COORDINATION_REPOSITORY': 'alice/nixpkgs-contribution-workflows',
                    'GITHUB_REPOSITORY_OWNER': 'alice', 'GITHUB_RUN_ID': '77', 'GITHUB_WORKFLOW_SHA': 'a' * 40}

    def test_configuration_defaults_and_external_reusable_call_rejection(self):
        with patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(cli.configuration()['fork'], 'alice/nixpkgs')
            self.assertEqual(cli.configuration()['runner'], 'alice/nixpkgs-review-gha')
            os.environ['GITHUB_REPOSITORY'] = 'alice/nixpkgs-darwin-updater'
            with self.assertRaises(cli.Error):
                cli.configuration()

    def test_artifact_is_bound_to_workflow_run_and_schema(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, self.env, clear=True):
            path = Path(directory) / 'snapshot.json'
            cli.write_snapshot(path, {'hello': 'world'})
            self.assertEqual(cli.read_snapshot(path), {'hello': 'world'})
            os.environ['GITHUB_RUN_ID'] = '78'
            with self.assertRaises(cli.Error):
                cli.read_snapshot(path)

    def test_evaluator_environment_does_not_inherit_secrets_or_github_file_commands(self):
        with patch.dict(os.environ, {'PATH': '/usr/bin', 'HOME': '/home/runner', 'NIXPKGS_PR_TOKEN': 'x',
                                    'GITHUB_TOKEN': 'x', 'GITHUB_OUTPUT': '/tmp/output',
                                    'ACTIONS_RUNTIME_TOKEN': 'x', 'GITHUB_ENV': '/tmp/env'}, clear=True):
            clean = cli.evaluation_environment()
            self.assertNotIn('NIXPKGS_PR_TOKEN', clean)
            self.assertNotIn('GITHUB_TOKEN', clean)
            self.assertNotIn('GITHUB_OUTPUT', clean)
            self.assertNotIn('ACTIONS_RUNTIME_TOKEN', clean)
            self.assertNotIn('GITHUB_ENV', clean)

    def test_output_rejects_multiline_injection(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, self.env, clear=True):
            os.environ['GITHUB_OUTPUT'] = str(Path(directory) / 'output')
            with self.assertRaises(cli.Error):
                cli.output(pr='123\nhead=evil')
            self.assertFalse(Path(os.environ['GITHUB_OUTPUT']).exists())

    def test_retirement_cli_verifies_old_run_and_refuses_missing_identity(self):
        self.assertTrue(hasattr(cli, 'retire_command'), 'operator retirement command must exist')
        old, store, api = Runner(), Store(), PublicAPI()
        config = {'upstream': 'NixOS/nixpkgs', 'base_ref': 'master', 'fork': 'alice/nixpkgs', 'runner': old.repository}
        snap = cli.core.review_snapshot(api, config, '123', 'pkg', 'auto', lambda *_: metadata())
        cli.core.review(api, old, store, snap)
        args = SimpleNamespace(repository='alice/workflows', pr='123', attempt=0, reason='Old runner paused and inspected')
        with patch.dict(os.environ, {'LEDGER_TOKEN': 'test-ledger', 'NIXPKGS_REVIEW_GHA_TOKEN': 'test-old-runner'}, clear=True), \
                patch.object(cli, 'ContentsStore', return_value=store), patch.object(cli, 'Runner', return_value=old) as factory, \
                patch.object(cli, 'summary'):
            old.runs.clear()
            with self.assertRaises(cli.Error):
                cli.retire_command(args)
            self.assertEqual(next(iter(store.records.values()))['attempts'][0]['state'], 'dispatched')
            old.runs[1] = {'id': 1, 'repository': {'full_name': old.repository}, 'status': 'completed', 'conclusion': 'failure',
                           'html_url': 'https://github.com/alice/nixpkgs-review-gha/actions/runs/1'}
            cli.retire_command(args)
            self.assertEqual(factory.call_args.args[1], old.repository)
            self.assertEqual(next(iter(store.records.values()))['attempts'][0]['state'], 'retired')

    def test_secret_free_preflight_displays_provisional_reuse_and_complete_review_plan(self):
        api = PublicAPI()
        api.existing = [pr() | {'title': 'Manual title', 'body': 'Existing edited body', 'draft': False}]
        request = {'branch': 'update/pkg', 'attribute': 'pkg', 'body': 'Supplied body', 'draft': True, 'mode': 'preflight'}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, self.env, clear=True), \
                patch.object(cli, 'API', return_value=api), \
                patch.object(cli, 'Evaluator', return_value=lambda sha, _: metadata('1' if sha == BASE else '2')), \
                patch.object(cli, 'Runner', side_effect=AssertionError('preflight must not construct a dispatcher')):
            path = Path(directory) / 'snapshot.json'
            summary_path = Path(directory) / 'summary.md'
            os.environ['GITHUB_STEP_SUMMARY'] = str(summary_path)
            os.environ['INPUT_JSON'] = json.dumps(request)
            cli.execute('preflight', path)
            report = summary_path.read_text()
            self.assertIn('Provisional', report)
            self.assertIn('Existing edited body', report)
            self.assertIn('Supplied body', report)
            self.assertIn('push-to-cache', report)
            self.assertIn('on-success', report)
            self.assertEqual(cli.read_snapshot(path)['preview']['publication']['action'], 'reuse')
            self.assertEqual(api.created, [])


if __name__ == '__main__':
    unittest.main()
