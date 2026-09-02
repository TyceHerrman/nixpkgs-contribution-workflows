import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def load(self, name):
        path = ROOT / '.github/workflows' / (name + '.yml')
        self.assertTrue(path.exists(), f'{name} workflow must exist')
        # JSON is a YAML subset. Keeping workflow definitions in this syntax
        # lets stdlib tests inspect the actual Actions job graph without a parser dependency.
        return json.loads(path.read_text())

    def test_preflight_never_receives_write_secrets_or_mutation_jobs(self):
        workflow = self.load('submit')
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['mode']['default'], 'preflight')
        evaluate = workflow['jobs']['preflight']
        self.assertEqual(evaluate['permissions'], {'contents': 'read'})
        self.assertNotIn('secrets.', json.dumps(evaluate))
        self.assertIn("inputs.mode == 'submit'", workflow['jobs']['publish']['if'])
        self.assertEqual(workflow['jobs']['publish']['needs'], ['preflight'])
        self.assertIn('NIXPKGS_PR_TOKEN', json.dumps(workflow['jobs']['publish']))
        self.assertNotIn('NIXPKGS_REVIEW_GHA_TOKEN', json.dumps(workflow['jobs']['publish']))
        call = workflow['jobs']['review']
        self.assertEqual(call['uses'], './.github/workflows/review.yml')
        self.assertEqual(call['needs'], ['publish'])
        self.assertEqual(call['permissions']['contents'], 'write')
        self.assertEqual(set(call['secrets']), {'NIXPKGS_REVIEW_GHA_TOKEN'})
        self.assertNotIn('schedule', workflow['on'])

    def test_review_scopes_secret_separation_and_concurrency(self):
        workflow = self.load('review')
        self.assertEqual(set(workflow['on']), {'workflow_call', 'workflow_dispatch'})
        public = workflow['on']['workflow_dispatch']['inputs']
        self.assertEqual(set(public), {'pr', 'attribute', 'platform-scope', 'force'})
        self.assertEqual(public['platform-scope']['options'], ['auto', 'darwin'])
        evaluate, dispatch = workflow['jobs']['evaluate'], workflow['jobs']['dispatch']
        self.assertEqual(evaluate['permissions'], {'contents': 'read'})
        self.assertNotIn('secrets.', json.dumps(evaluate))
        self.assertEqual(dispatch['permissions'], {'contents': 'write'})
        self.assertNotIn('NIXPKGS_PR_TOKEN', json.dumps(workflow))
        self.assertEqual(dispatch['concurrency']['cancel-in-progress'], False)
        self.assertEqual(dispatch['concurrency']['queue'], 'max')
        self.assertIn('inputs.pr', dispatch['concurrency']['group'])
        self.assertFalse(workflow['on']['workflow_call']['secrets']['NIXPKGS_REVIEW_GHA_TOKEN']['required'])

    def test_all_external_actions_pinned_and_checkouts_forbid_persistent_credentials(self):
        for name in ('submit', 'review', 'test'):
            workflow = self.load(name)
            for job in workflow['jobs'].values():
                for step in job.get('steps', []):
                    uses = step.get('uses', '')
                    if uses and not uses.startswith('./'):
                        self.assertRegex(uses, r'^[^@]+@[0-9a-f]{40}$')
                    if uses.startswith('actions/checkout@'):
                        self.assertIs(step['with']['persist-credentials'], False)
                    if 'run' in step:
                        self.assertNotIn('${{ inputs.', step['run'])


if __name__ == '__main__':
    unittest.main()
