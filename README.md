# Nixpkgs contribution workflows

Submit a version-update branch as a user-authored Nixpkgs pull request and immediately request the applicable Linux and Darwin reviews through your existing [nixpkgs-review-gha](https://github.com/Defelo/nixpkgs-review-gha) installation.

This repository coordinates two entrypoints: `submit.yml` and `review.yml`. It runs metadata evaluation on GitHub-hosted Linux runners. The existing review runner builds packages and posts its normal result. There is no push watcher, scheduled polling, automatic ready transition, approval, or merge.

## Install

Install these files on the default branch of your own public `nixpkgs-contribution-workflows` repository. Keep a `nixpkgs` fork and a working `nixpkgs-review-gha` installation under the same owner. The review runner's `.github/workflows/review.yml` remains unchanged.

Defaults are `OWNER/nixpkgs`, `OWNER/nixpkgs-review-gha`, and upstream `NixOS/nixpkgs` on `master`. Optional Actions variables:

| Variable | Purpose |
| --- | --- |
| `NIXPKGS_FORK_REPOSITORY` | Override the owner/name of your Nixpkgs fork. |
| `NIXPKGS_REVIEW_GHA_REPOSITORY` | Override the owner/name of your review runner. |
| `NIXPKGS_CONTRIBUTION_REPOSITORY` | Override the coordination repository when installing under a name other than `OWNER/nixpkgs-contribution-workflows`. |

Set the two secrets in this coordination repository:

| Secret | Required access | Where exposed |
| --- | --- | --- |
| `NIXPKGS_PR_TOKEN` | User authentication able to create PRs against public `NixOS/nixpkgs` from your fork. A classic PAT with `public_repo` is a common option; a fine-grained token for only your fork does not grant upstream PR access. | Only the fresh publication job. |
| `NIXPKGS_REVIEW_GHA_TOKEN` | Fine-grained PAT with **Actions: read and write**, selected repository **your review runner only**; Metadata read is implicit. | Only the fresh dispatch job. |

The runtime `github.token` writes ledger data with `contents: write` in this repository. It is not used to author upstream PRs or supplied to the other repository. Metadata jobs have only `contents: read`, checkouts do not persist credentials, and the Nix subprocess receives neither secrets nor GitHub runtime/file-command variables. Import-from-derivation is disabled. No secrets are required for submission preflight or a review with zero eligible systems.

Example setup with the GitHub CLI; `gh secret set` prompts privately for each value:

```sh
OWNER="$(gh api user --jq .login)"
COORDINATOR="$OWNER/nixpkgs-contribution-workflows"
gh secret set NIXPKGS_PR_TOKEN --repo "$COORDINATOR"
gh secret set NIXPKGS_REVIEW_GHA_TOKEN --repo "$COORDINATOR"

# Optional overrides; omit these for the defaults.
gh variable set NIXPKGS_FORK_REPOSITORY --repo "$COORDINATOR" --body "$OWNER/nixpkgs"
gh variable set NIXPKGS_REVIEW_GHA_REPOSITORY --repo "$COORDINATOR" --body "$OWNER/nixpkgs-review-gha"
```

Enable Actions for the installation and runner repositories. Repository or organization policies must permit the declared workflow permissions and pinned actions. The workflow files use JSON syntax, which is a YAML subset, so the Python stdlib tests can inspect the actual job graph without another dependency. External actions are pinned to full commits.

## Submit a branch

Create and push your version-update branch to your fork yourself. Supply a complete PR body, including actual testing evidence, the relevant Nixpkgs checklist, and any required disclosures. The body is preserved verbatim; the workflow does not invent tests or disclosures.

| `submit.yml` input | Required/default |
| --- | --- |
| `branch` | Required existing fork branch. |
| `attribute` | Required simple dotted attribute path, such as `lix` or `python3Packages.example`. Quoted/dynamic attribute expressions are deliberately unsupported. |
| `body` | Required complete PR body. |
| `title` | Optional; defaults to `attribute: old -> new`. |
| `draft` | Boolean, default `true`. |
| `mode` | `preflight` (default) or `submit`. |

```sh
gh workflow run submit.yml --repo "$COORDINATOR" \
  -f branch=update/example -f attribute=example \
  -F body=@pr-body.md -F draft=true -f mode=preflight

# After reviewing preflight, deliberately publish the real update:
gh workflow run submit.yml --repo "$COORDINATOR" \
  -f branch=update/example -f attribute=example \
  -F body=@pr-body.md -F draft=true -f mode=submit
```

Preflight resolves fork branch and upstream master to immutable commits, confirms the attribute exists and its version changes, and records per-system eligibility and reasons. It never publishes or dispatches. Metadata errors, absent versions/attributes, differing versions between platforms, and unchanged versions fail preflight.

Its **provisional action preview** also looks up the exact open PR target. The workflow summary and snapshot show whether publication currently intends to create or reuse a PR, the supplied title/body/draft versus the existing values that would be preserved, and the configured runner's complete proposed workflow inputs. A new PR number is shown as a placeholder until creation. If no systems qualify, the preview explicitly shows publication followed by a skipped review. Publication rechecks live PR state; review reevaluates and consults the ledger, so the preview is not a promise to dispatch a new run.

Publication occurs in a fresh job after checking the artifact schema, workflow-run/revision identity, original inputs, and current branch/base commits. Existing open PRs for the exact fork branch and upstream base are reused without editing title, body, or draft status. A newly created PR is draft by default. The branch and PR are rechecked after publication; if the head moved, the summary explains that the PR exists and review was not started. Rerunning safely discovers the existing PR.

Creation is serialized by fork branch with a non-cancelling queue. After publication, a local reusable call to `review.yml` starts immediately, including for drafts, without waiting for CI or ready transitions. No eligible systems still permits a PR and produces an explicit skipped review result.

Do not create upstream test PRs to validate this automation. Use the included fake-API tests and secret-free preflight on an actual intended update.

## Review an existing PR

Run `review.yml` **in the coordination repository**:

| Public input | Required/default |
| --- | --- |
| `pr` | Required positive PR number as a string. |
| `attribute` | Required package attribute to determine platform eligibility. |
| `platform-scope` | `auto` (default) or `darwin`. |
| `force` | Boolean, default `false`; creates another attempt for known state. |

```sh
gh workflow run review.yml --repo "$COORDINATOR" \
  -f pr=123456 -f attribute=example -f platform-scope=auto -F force=false
```

The workflow verifies the PR is open, comes from the configured fork, and targets `NixOS/nixpkgs:master`. It evaluates the pinned head and rechecks head/base before dispatch. The internal `expected-head` reusable input connects submission to the same head.

Cross-repository callers must **dispatch** this workflow in its installation repository, not use a remote reusable-workflow call: Actions concurrency groups and ledger permissions belong to the repository running the workflow. The default coordinator check rejects a caller such as `OWNER/nixpkgs-darwin-updater`. It is a configuration consistency check, not proof of a caller's identity. Updaters should use `platform-scope=darwin`; their own existing CI gate can remain in place.

All four standard targets are inspected: `x86_64-linux`, `aarch64-linux`, `x86_64-darwin`, and `aarch64-darwin`. Selection requires both Nixpkgs' global system declaration (`lib.systems.doubles.all`) and `lib.meta.availableOn`, excluding `meta.broken`, then intersects with the requested scope. Nixpkgs 26.11 removed global x86 Darwin support. The metadata-only `allowDeprecatedx86_64Darwin = "force"` option permits inspection despite its import guard; it does **not** restore eligibility or change the runner configuration. A package with a lingering explicit platform entry cannot restore a globally removed target.

Metadata is a platform-selection hint, not a successful build, test result, or complete dependency compatibility check. Darwin-only package versions can be inspected from the Linux evaluator by importing package sets with unsupported/broken metadata inspection enabled. Unexpected evaluation failures remain errors; they are never converted to unsupported by `tryEval`. The runner reviews the PR's changed packages, not only the attribute used to select platforms.

The unchanged runner receives:

- Eligible Linux targets as `true`/`false`; eligible Darwin targets as `yes_sandbox_relaxed`/`no`.
- `riscv64-linux=false`, `builders=gha`, `push-to-cache=true`, `post-result=true`.
- `upterm=false`, `on-success=nothing`, and an empty `extra-args`.

Cache pushes use only caches already configured in the runner; no cache credentials are transferred here. The controller expects GitHub API version `2026-03-10` to return HTTP 200 with `workflow_run_id`, `run_url`, and `html_url`. Older HTTP 204 replies are uncertain and require reconciliation.

## Ledger, retries, and evidence

The dispatcher initializes `state/reviews` as an orphan branch through the Git Data API. Its initial tree contains only a ledger README, with no source tree or parent commit. It writes small JSON records at `reviews/NixOS/nixpkgs/PR.json`, preserving attempts and fingerprints of upstream/runner repositories, PR, actual requested head/base, sorted systems, and settings. Do not execute files from this branch or delete it to make retries work.

The dispatch critical section is serialized per PR in this repository, using `queue: max` and `cancel-in-progress: false`. Bounded Contents API compare-and-swap retries handle unrelated record updates. Every attempt records an intent **before** the external dispatch, followed by the returned run identity. Timeout, ambiguous response, bad identity, or failure saving that identity leaves `intent`/`needs-reconciliation`; all automatic redispatch for the PR is blocked, including `force`.

For a matching request, all recorded attempts are considered: an active run is reused as pending before considering any report-verified success. A later failed forced attempt cannot hide an older active or verified successful attempt. A completed successful workflow is reused as verified only after its `reports.json` confirms actual head/base, exact system coverage, sandbox settings, and no failed/still-failing builds. Workflow success alone is insufficient because the runner uses `--no-exit-status`. Failed, cancelled, or timed-out runs and reports with failed builds are retryable when no matching reusable attempt remains. Missing/expired/mismatched artifacts produce an error when no other attempt establishes reusable coverage. Raw JSON and bounded ZIP artifacts are supported; authorization is removed on cross-host HTTPS redirects.

The runner resolves the PR again when its preparation starts. It cannot accept a pinned expected SHA, so a push/base movement after dispatch can change what it actually tests. Reports remain authoritative. A different active request for the same PR blocks a new dispatch until it finishes or is resolved; the error links that run. `force` allows a deliberate extra attempt for known matching state, but cannot bypass an unresolved intent or an active different snapshot.

Reports expose commits, systems, and sandbox. They do **not** echo all runner inputs. Other settings are tied to the original recorded request and returned run ID, not independently proven by the artifact. Operator attachment of a lost run additionally depends on manually verifying the candidate's workflow inputs. No scheduled poller refreshes records: later explicit requests inspect their recorded runs.

### Reconcile an uncertain attempt

Pause new review requests first. Inspect the ledger and the target runner's Actions history, including timestamps, workflow, PR input, settings, and all plausible runs. Never infer that no run exists from a missing reply. An active candidate must finish before the CLI can attach it, because independent reports are required.

Use a local checkout of this repository. Supply an operator-scoped `LEDGER_TOKEN` with Contents write on the coordination repository and `NIXPKGS_REVIEW_GHA_TOKEN` with Actions read on the runner using your normal private environment mechanism. Do not print tokens or commit them. The CLI provides two explicit recovery actions:

```sh
# Attach a known completed run after manually verifying its original inputs.
python3 -m contribution.cli reconcile --repository "$COORDINATOR" \
  --pr 123456 --attempt 0 --run-id 987654321 \
  --reason 'Verified workflow inputs and candidate run identity in Actions history'

# Only when you have established that the dispatch created no run:
python3 -m contribution.cli reconcile --repository "$COORDINATOR" \
  --pr 123456 --attempt 0 --no-run \
  --reason 'Verified no matching target run exists; explain supporting evidence here'
```

The CLI uses compare-and-swap, preserves the original intent and explanation, and validates attached run repository/workflow identity and report snapshot. If evidence cannot resolve an intent, leave it blocked. After resolution, resume requests and rerun `review.yml`; use `force` only for a deliberate extra attempt. Review records have a conservative size limit and must be archived deliberately if a PR accumulates very many attempts.

### Change the review runner repository

Historical run IDs are resolved against the runner recorded in each attempt, never against the replacement repository. These historical lookups use public read-only API access and do not forward the replacement runner's write credential. An active historical run still blocks another snapshot. A missing run, HTTP 404, or inaccessible repository is not evidence of completion and cannot be bypassed with `force`.

If public lookup is unavailable during a migration, pause coordination requests, disable the **old** runner's review workflow, and let or cancel its active runs to completion. Keep that workflow disabled and do not rerun retired jobs: retirement deliberately ends automatic live monitoring of those historical runs. Using your private local environment mechanism, supply `LEDGER_TOKEN` scoped to Contents write in the coordinator and `NIXPKGS_REVIEW_GHA_TOKEN` scoped to Actions read in the **old runner only**. Do not widen the replacement runner's workflow secret or change its scope.

```sh
python3 -m contribution.cli retire-run --repository "$COORDINATOR" \
  --pr 123456 --attempt 0 \
  --reason 'Old runner workflow disabled; recorded run verified terminal for migration'
```

The command fetches the recorded run from the old repository, verifies repository/workflow/run identity and completed status, then preserves the complete attempt with terminal evidence and a retirement explanation. It rejects active, unavailable, or unknown runs; it cannot retire unresolved intents. Retired attempts provide no coverage and are no longer consulted for active-run guards. Retire each inaccessible terminal attempt that blocks migration, then update `NIXPKGS_REVIEW_GHA_REPOSITORY`, provision the replacement-only secret, and resume requests. If the old run cannot be verified, leave it blocked rather than deleting history or treating 404 as success.

## Development checks

Python 3.11+ and Nix are sufficient; no Python packages are installed. Point `NIXPKGS_LIB` at a real Nixpkgs `lib` directory for the evaluator fixtures:

```sh
NIXPKGS_LIB=/path/to/nixpkgs/lib python3 -m unittest discover -s tests -v
```

The test workflow checks out a pinned Nixpkgs library and runs all tests. Without `NIXPKGS_LIB`, only the Nix fixtures are skipped. Tests use fake API clients for publication, dispatch, ledger conflicts and reconciliation; they create no upstream PRs and dispatch no real reviews.
