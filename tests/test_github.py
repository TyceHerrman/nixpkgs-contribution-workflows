import base64
import io
import json
import unittest
import urllib.request
import zipfile
from unittest.mock import patch

try:
    from contribution import github as g
except ImportError:
    g = None


class GitHubTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(g, 'GitHub adapter must be implemented')

    def test_download_redirect_strips_authorization_and_rejects_http(self):
        handler = g.SafeRedirect()
        request = urllib.request.Request('https://api.github.com/repos/a/b/actions/artifacts/1/zip',
                                         headers={'Authorization': 'Bearer test-value', 'Accept': 'application/vnd.github+json'})
        redirected = handler.redirect_request(request, None, 302, 'Found', {}, 'https://blob.example/download')
        self.assertNotIn('Authorization', redirected.headers)
        self.assertNotIn('Authorization', redirected.unredirected_hdrs)
        with self.assertRaises(g.Error):
            handler.redirect_request(request, None, 302, 'Found', {}, 'http://blob.example/download')

    def test_reports_support_raw_json_and_zip_without_extracting(self):
        payload = [{'head': 'a' * 40, 'system': 'aarch64-darwin'}]
        data = json.dumps(payload).encode()
        self.assertEqual(g.decode_reports(data), payload)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr('reports.json', data)
        self.assertEqual(g.decode_reports(archive.getvalue()), payload)
        for name in ['../reports.json', '/reports.json', 'other.json']:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr(name, data)
            with self.subTest(name=name), self.assertRaises(g.Error):
                g.decode_reports(archive.getvalue())
        with self.assertRaises(g.Error):
            g.decode_reports(b' ' * (g.MAX_BYTES + 1))

    def test_orphan_ledger_has_no_parent_and_no_copied_source_tree(self):
        api = FakeGitAPI()
        store = g.ContentsStore(api, 'alice/workflows')
        store.initialize()
        self.assertEqual(api.tree, {'tree': [{'path': 'README.md', 'mode': '100644', 'type': 'blob',
                                            'content': g.LEDGER_README}]})
        self.assertEqual(api.commit['parents'], [])
        self.assertEqual(api.ref['ref'], 'refs/heads/state/reviews')

    def test_contents_cas_includes_previous_blob_and_conflicts_are_visible(self):
        api = FakeGitAPI()
        store = g.ContentsStore(api, 'alice/workflows')
        store.cas('reviews/NixOS/nixpkgs/123.json', {'schema': 1, 'attempts': []}, None)
        record, revision = store.read('reviews/NixOS/nixpkgs/123.json')
        self.assertEqual(record['schema'], 1)
        self.assertEqual(revision, 'blob1')
        store.cas('reviews/NixOS/nixpkgs/123.json', {'schema': 1, 'attempts': [{'id': 'new'}]}, revision)
        self.assertEqual(api.last_put['sha'], 'blob1')
        api.conflict = True
        with self.assertRaises(g.Conflict):
            store.cas('reviews/NixOS/nixpkgs/123.json', record, revision)

    def test_dispatch_uses_2026_response_and_204_is_uncertain(self):
        api = FakeDispatchAPI()
        runner = g.Runner(api, 'alice/runner')
        reply = runner.dispatch({'pr': '123'})
        self.assertEqual(reply['workflow_run_id'], 91)
        self.assertEqual(api.posts, [('repos/alice/runner/actions/workflows/review.yml/dispatches',
                                     {'ref': 'main', 'inputs': {'pr': '123'}})])
        api.status = 204
        with self.assertRaises(g.Error):
            runner.dispatch({'pr': '123'})
        self.assertEqual(len(api.posts), 2)

    def test_http_client_uses_current_version_and_never_retries_post(self):
        calls = []
        class Opener:
            def open(self, request, timeout):
                calls.append(request)
                raise TimeoutError('lost response')
        client = g.API('test-token', opener=Opener())
        with self.assertRaises(g.Error):
            client.request('POST', 'repos/alice/runner/actions/workflows/review.yml/dispatches', {'inputs': {}})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get_header('X-github-api-version'), '2026-03-10')
        with self.assertRaises(g.Error):
            client.request('GET', 'https://evil.example/')

    def test_historical_lookup_uses_recorded_repository_without_current_write_token(self):
        self.assertTrue(hasattr(g.Runner, 'for_repository'), 'historical runner routing must exist')
        requests = []
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                response = io.BytesIO(json.dumps({'event': 'workflow_dispatch', 'path': '.github/workflows/review.yml'}).encode())
                response.status = 200
                return response
        current = g.Runner(g.API('current-runner-write-token'), 'alice/new-runner')
        with patch('urllib.request.build_opener', return_value=Opener()):
            current.for_repository('alice/old-runner').get_run(71)
        self.assertEqual(requests[0].full_url, 'https://api.github.com/repos/alice/old-runner/actions/runs/71')
        self.assertIsNone(requests[0].get_header('Authorization'))


class FakeGitAPI:
    def __init__(self):
        self.tree = self.commit = self.ref = self.last_put = None
        self.content = None
        self.conflict = False

    def request(self, method, path, data=None):
        if method == 'GET' and '/git/ref/' in path:
            if self.ref is None:
                raise g.HTTPError(404)
            return 200, self.ref
        if method == 'POST' and path.endswith('/git/trees'):
            self.tree = data
            return 201, {'sha': 'tree'}
        if method == 'POST' and path.endswith('/git/commits'):
            self.commit = data
            return 201, {'sha': 'commit'}
        if method == 'POST' and path.endswith('/git/refs'):
            self.ref = data
            return 201, data
        if method == 'GET' and '/contents/' in path:
            if self.content is None:
                raise g.HTTPError(404)
            return 200, {'encoding': 'base64', 'content': self.content, 'sha': 'blob1', 'size': 100}
        if method == 'PUT' and '/contents/' in path:
            if self.conflict:
                raise g.HTTPError(409)
            self.last_put = data
            self.content = data['content']
            return 200, {}
        raise AssertionError((method, path, data))


class FakeDispatchAPI:
    def __init__(self):
        self.posts = []
        self.status = 200

    def request(self, method, path, data=None):
        if method == 'GET' and path == 'repos/alice/runner':
            return 200, {'full_name': 'alice/runner', 'default_branch': 'main'}
        if method == 'GET' and path.endswith('workflows/review.yml'):
            return 200, {'path': '.github/workflows/review.yml', 'state': 'active'}
        if method == 'POST':
            self.posts.append((path, data))
            return self.status, {'workflow_run_id': 91}
        raise AssertionError((method, path, data))
