import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

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


if __name__ == '__main__':
    unittest.main()
