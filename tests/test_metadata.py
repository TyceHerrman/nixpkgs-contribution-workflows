import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('nix-instantiate') and os.environ.get('NIXPKGS_LIB'), 'set NIXPKGS_LIB to run Nix metadata fixtures')
class MetadataTests(unittest.TestCase):
    def evaluate(self, attr, fixture='nixpkgs'):
        self.assertTrue((ROOT / 'metadata.nix').exists(), 'metadata evaluator must exist')
        return subprocess.run(['nix-instantiate', '--eval', '--strict', '--json', str(ROOT / 'metadata.nix'),
                               '--argstr', 'nixpkgs', str(ROOT / 'tests/fixtures' / fixture),
                               '--argstr', 'attribute', attr], capture_output=True, text=True)

    def test_platform_selection_uses_real_nixpkgs_lib(self):
        for attr, expected in [('linuxOnly', {'x86_64-linux', 'aarch64-linux'}),
                               ('darwinOnly', {'x86_64-darwin', 'aarch64-darwin'}),
                               ('armOnly', {'aarch64-darwin', 'aarch64-linux'}),
                               ('badPlatform', {'aarch64-linux'}), ('broken', set()),
                               ('deprecatedDarwin', {'x86_64-darwin', 'aarch64-darwin'})]:
            with self.subTest(attr=attr):
                result = self.evaluate(attr)
                self.assertEqual(result.returncode, 0, result.stderr)
                parsed = json.loads(result.stdout)
                self.assertEqual(parsed['version'], '2.0')
                self.assertEqual({s for s, v in parsed['systems'].items() if v['eligible']}, expected)
                self.assertTrue(all(v['reason'] for v in parsed['systems'].values()))

    def test_missing_attribute_and_unexpected_eval_failure_are_errors(self):
        for attr in ['missing', 'evalError']:
            with self.subTest(attr=attr):
                result = self.evaluate(attr)
                self.assertNotEqual(result.returncode, 0)

    def test_removed_global_target_is_ineligible_despite_lingering_package_metadata(self):
        result = self.evaluate('deprecatedDarwin', 'removed-nixpkgs')
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = json.loads(result.stdout)
        self.assertFalse(parsed['systems']['x86_64-darwin']['eligible'])
        self.assertIn('Nixpkgs', parsed['systems']['x86_64-darwin']['reason'])
        self.assertTrue(parsed['systems']['aarch64-darwin']['eligible'])


if __name__ == '__main__':
    unittest.main()
