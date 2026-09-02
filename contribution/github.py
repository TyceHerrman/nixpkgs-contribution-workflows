"""Small GitHub REST adapter. No automatic retries of side-effecting requests."""

import base64
import io
import json
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from .core import Conflict, Error, canonical, require, validate_branch, validate_repo, validate_sha

API_VERSION = '2026-03-10'
MAX_BYTES = 8 * 1024 * 1024
LEDGER_BRANCH = 'state/reviews'
LEDGER_README = '# Review ledger\n\nData only. Managed by the contribution workflows; never execute files on this branch.\n'


class HTTPError(Error):
    def __init__(self, status):
        self.status = status
        super().__init__(f'GitHub API returned HTTP {status}')


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        require(parsed.scheme == 'https' and not parsed.username and not parsed.password, 'Unsafe artifact redirect')
        # REST mutations must never be redirected (or replayed to a second host).
        require(req.get_method() == 'GET', 'Unexpected redirect of a GitHub mutation')
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if parsed.hostname != 'api.github.com':
            redirected.remove_header('Authorization')
            redirected.remove_header('Cookie')
        return redirected


def read_bounded(response):
    data = response.read(MAX_BYTES + 1)
    require(len(data) <= MAX_BYTES, 'GitHub response or artifact exceeds 8 MiB limit')
    return data


class API:
    def __init__(self, token='', *, opener=None):
        self.token = token
        self.opener = opener or urllib.request.build_opener(SafeRedirect())

    def request(self, method, path, data=None, *, raw=False):
        require(path.startswith('repos/') and not any(s in path for s in ('://', '\n', '\r', '..')), 'Invalid GitHub API path')
        headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': API_VERSION,
                   'User-Agent': 'nixpkgs-contribution-workflows'}
        if self.token:
            headers['Authorization'] = f'Bearer {self.token}'
        if data is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request('https://api.github.com/' + path, method=method, headers=headers,
                                         data=None if data is None else canonical(data).encode())
        try:
            with self.opener.open(request, timeout=60) as response:
                content = read_bounded(response)
                status = response.status
        except urllib.error.HTTPError as exc:
            # Never log response bodies/headers or a redirected signed URL.
            raise HTTPError(exc.code) from None
        except (TimeoutError, OSError, urllib.error.URLError):
            raise Error('GitHub transport failed; no retry was attempted') from None
        if raw:
            return status, content
        try:
            return status, json.loads(content) if content else None
        except (ValueError, UnicodeError):
            raise Error('GitHub returned an invalid JSON response') from None

    def pages(self, path, field=None):
        separator = '&' if '?' in path else '?'
        for page in range(1, 21):
            _, result = self.request('GET', f'{path}{separator}per_page=100&page={page}')
            items = result[field] if field else result
            require(isinstance(items, list), 'Invalid GitHub list response')
            yield from items
            if len(items) < 100:
                return
        raise Error('GitHub pagination limit exceeded')

    def branch_sha(self, repo, branch):
        validate_repo(repo)
        validate_branch(branch)
        _, result = self.request('GET', f'repos/{repo}/git/ref/heads/{urllib.parse.quote(branch, safe="/")}')
        require(result.get('object', {}).get('type') == 'commit', 'Branch does not point to a commit')
        return validate_sha(result['object']['sha'])

    def get_pr(self, upstream, number):
        return self.request('GET', f'repos/{upstream}/pulls/{number}')[1]

    def find_prs(self, upstream, fork, branch, base):
        query = urllib.parse.urlencode({'state': 'open', 'head': f'{fork.split("/")[0]}:{branch}', 'base': base})
        return list(self.pages(f'repos/{upstream}/pulls?{query}'))

    def create_pr(self, upstream, payload):
        status, result = self.request('POST', f'repos/{upstream}/pulls', payload)
        require(status == 201, 'PR creation response was uncertain; check existing PRs before retrying')
        return result


class ContentsStore:
    def __init__(self, api, repository):
        self.api, self.repository = api, validate_repo(repository)

    def initialize(self):
        prefix = f'repos/{self.repository}'
        try:
            self.api.request('GET', f'{prefix}/git/ref/heads/{LEDGER_BRANCH}')
            return
        except HTTPError as exc:
            if exc.status != 404:
                raise
        # Omitting base_tree creates a new tree containing ONLY ledger data.
        _, tree = self.api.request('POST', f'{prefix}/git/trees', {'tree': [
            {'path': 'README.md', 'mode': '100644', 'type': 'blob', 'content': LEDGER_README}]})
        _, commit = self.api.request('POST', f'{prefix}/git/commits',
                                     {'message': 'Initialize data-only review ledger', 'tree': tree['sha'], 'parents': []})
        try:
            self.api.request('POST', f'{prefix}/git/refs', {'ref': f'refs/heads/{LEDGER_BRANCH}', 'sha': commit['sha']})
        except HTTPError as exc:
            if exc.status != 422:
                raise
            # Concurrent first requests can race branch creation, but may not
            # replace an existing branch or attach it to the source history.
            self.api.request('GET', f'{prefix}/git/ref/heads/{LEDGER_BRANCH}')

    def _path(self, key):
        require(key.startswith('reviews/NixOS/nixpkgs/') and key.endswith('.json') and
                key.removeprefix('reviews/NixOS/nixpkgs/').removesuffix('.json').isdigit(), 'Invalid ledger path')
        return f'repos/{self.repository}/contents/{key}'

    def read(self, key):
        try:
            _, result = self.api.request('GET', self._path(key) + '?ref=' + urllib.parse.quote(LEDGER_BRANCH, safe=''))
        except HTTPError as exc:
            if exc.status == 404:
                return None, None
            raise
        require(result.get('encoding') == 'base64' and result.get('size', MAX_BYTES + 1) <= MAX_BYTES, 'Invalid ledger content')
        try:
            value = json.loads(base64.b64decode(result['content'], validate=False))
        except (ValueError, UnicodeError):
            raise Error('Ledger is not valid JSON') from None
        return value, result['sha']

    def cas(self, key, value, revision):
        content = canonical(value).encode()
        require(len(content) < 900_000, 'PR ledger is full; archive old attempts manually before continuing')
        payload = {'message': f'Update {key}', 'branch': LEDGER_BRANCH, 'content': base64.b64encode(content).decode()}
        if revision is not None:
            payload['sha'] = revision
        try:
            self.api.request('PUT', self._path(key), payload)
        except HTTPError as exc:
            if exc.status in {409, 422}:
                raise Conflict('Ledger compare-and-swap conflict') from None
            raise


def decode_reports(data):
    require(len(data) <= MAX_BYTES, 'reports.json exceeds size limit')
    try:
        if data.startswith(b'PK'):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                require(len(entries) == 1 and entries[0].filename == 'reports.json' and
                        entries[0].file_size <= MAX_BYTES, 'Unexpected reports.json archive contents')
                with archive.open(entries[0]) as stream:
                    data = read_bounded(stream)
        return json.loads(data)
    except (ValueError, UnicodeError, zipfile.BadZipFile, RuntimeError):
        raise Error('Invalid reports.json artifact') from None


class Runner:
    def __init__(self, api, repository):
        self.api, self.repository = api, validate_repo(repository)

    def dispatch(self, inputs):
        prefix = f'repos/{self.repository}'
        _, repo = self.api.request('GET', prefix)
        require(repo.get('full_name', '').lower() == self.repository.lower(), 'Runner repository identity mismatch')
        ref = validate_branch(repo['default_branch'])
        _, workflow = self.api.request('GET', f'{prefix}/actions/workflows/review.yml')
        require(workflow.get('path') == '.github/workflows/review.yml' and workflow.get('state') == 'active', 'Runner review workflow is missing or disabled')
        status, reply = self.api.request('POST', f'{prefix}/actions/workflows/review.yml/dispatches', {'ref': ref, 'inputs': inputs})
        require(status == 200, 'Dispatch did not return HTTP 200 with a run ID; reconciliation required')
        return reply

    def get_run(self, run_id):
        _, run = self.api.request('GET', f'repos/{self.repository}/actions/runs/{run_id}')
        require(run.get('event') == 'workflow_dispatch' and run.get('path', '').split('@')[0] == '.github/workflows/review.yml',
                'Recorded run is not the runner review workflow')
        return run

    def reports(self, run_id):
        artifacts = [a for a in self.api.pages(f'repos/{self.repository}/actions/runs/{run_id}/artifacts', 'artifacts')
                     if a.get('name') == 'reports.json' and not a.get('expired')]
        require(len(artifacts) == 1, 'reports.json is missing, expired, or ambiguous; coverage unverified')
        artifact = artifacts[0]
        require(type(artifact.get('id')) is int and artifact['id'] > 0 and artifact.get('size_in_bytes', MAX_BYTES + 1) <= MAX_BYTES,
                'Invalid reports.json artifact metadata')
        _, data = self.api.request('GET', f"repos/{self.repository}/actions/artifacts/{artifact['id']}/zip", raw=True)
        return decode_reports(data)
