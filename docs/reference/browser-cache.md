---
title: Browser cache
parent: Reference
nav_order: 6
---

# Browser cache

Browser jobs use the **Verified Chromium cache** action to prepare Chromium and its OS dependencies. Only a push to `dev` can save a new browser cache. Pull requests, merge groups and manual dispatches can restore an accessible entry, or install Chromium for that job without saving another copy.

This keeps large browser downloads from crowding out smaller CI caches. Before [the change](https://github.com/the-cloud-clockwork/agentihooks/pull/3515), 21 ref-scoped copies of about 287 MB occupied roughly 6 GB of the repository's 10 GB cache allowance, evicting mutation statistics within forty minutes. Browser caches store tools; [CI result caching](ci-result-cache.md) separately decides whether a check can reuse a passed result.

## Restore and save rules

The action first tries `ARC_PLAYWRIGHT_BROWSERS_PATH`, when the runner image provides that directory and the workspace browser path is absent. It links the directory into the workspace and verifies Chromium by launching it and rendering a page. A successful verification skips both cache restore and installation.

Otherwise it reads the installed Python Playwright version and restores the exact key `chromium-<runner OS>-<runner architecture>-<Playwright version>` into the workspace's `.playwright` directory. For example, Linux X64 with Playwright 1.64.0 uses `chromium-Linux-X64-1.64.0`. There is no prefix fallback to a different Playwright version.

| Job situation | Restore | Install and verify | Save |
|---|---|---|---|
| Verified browser from the runner image | Skipped | Skipped | Skipped |
| Exact cache hit passes launch verification | Exact key | Skipped | Skipped |
| Cache miss on a `push` to `refs/heads/dev` | Attempted | Required | After successful installation |
| Cache miss on any other event or ref | Attempted | Required | Skipped |
| Exact cache hit fails launch verification | Exact key | Required | Skipped, even on `dev` |

The restore step uses `actions/cache/restore@v4`; it has no automatic post-job save. The separate `actions/cache/save@v4` step runs only after installation succeeds, the initial launch check was not ready, the restore was not an exact hit, and the event is a push to `refs/heads/dev`. A manual dispatch on `dev` does not save. An existing exact key is not overwritten after a failed launch.

Restoring on every ref means attempting the lookup, subject to [GitHub cache access restrictions](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching#restrictions-for-accessing-a-cache). A pull request targeting `dev` can access its base branch's cache. Do not assume a merge group can read `dev` caches: queue refs and the default branch (`main`) have different scopes. An inaccessible entry behaves as a miss and the job installs Chromium. The action does not copy or fetch browser caches across scopes.

## Configure a browser job

The composite action has no inputs or outputs. Check out the repository and install Python Playwright before calling it. Set `PLAYWRIGHT_BROWSERS_PATH` on the job so subsequent browser tests use the same directory that the action prepares:

{% raw %}
```yaml
jobs:
  browser-checks:
    runs-on: ubuntu-latest
    env:
      PLAYWRIGHT_BROWSERS_PATH: ${{ github.workspace }}/.playwright
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install project dependencies
        run: python -m pip install -e '.[dev,all]'
      - name: Prepare Chromium
        timeout-minutes: 6
        uses: ./.github/actions/browser-cache
      - name: Verify Chromium is available to later steps
        run: bash .github/actions/browser-cache/verify.sh
```
{% endraw %}

Add these job settings to a workflow with the events required by the check. The action applies its save condition itself; callers do not need a separate save step or event condition.

The runner needs Python, Bash, `timeout` and permission to install Chromium's OS dependencies. On GitHub-hosted runners the action removes the Azure Ubuntu archive mirror entry before installation. On ARC runners, `ARC_PLAYWRIGHT_BROWSERS_PATH` is an optional runner-provided directory; leave it unset when no baked browser exists. Each job verifies that directory rather than trusting its presence.

The existing Proofs workflow supports focused browser checks without changing cache policy:

```bash
gh workflow run proofs.yml \
  --ref <branch> \
  -f proof=tests \
  -f tests=tests/test_ci_browser_cache.py \
  -f browser=true
```

Replace the test path with the browser tests being investigated. A dispatch can restore or install Chromium but cannot seed the shared cache. A qualifying `dev` push seeds it only when the job actually needs installation after a miss; a runner with a working baked browser does not create an entry.

## Inspect caches and troubleshoot

List every Chromium cache's key, saving ref and size with the read-only API:

```bash
gh api --paginate \
  'repos/the-cloud-clockwork/agentihooks/actions/caches?key=chromium-&per_page=100' \
  --jq '.actions_caches[] | {key, ref, size_in_bytes}'
```

New browser entries should have `ref: refs/heads/dev`; multiple keys can be legitimate when Playwright, OS or architecture differs. This policy prevents new pull request and queue copies; it does not delete old entries. Cache inventory on 17 October 2026 showed one entry, `chromium-Linux-X64-1.64.0`, on `refs/heads/dev`, sized 301262672 bytes.

Inspect a job's **Restore Chromium**, **Verify the cached browser and OS dependencies**, **Install Chromium and missing OS dependencies**, and **Save Chromium from dev** steps. A skipped save is expected on a cache hit, a baked browser, a pull request, a merge group or a dispatch. A miss can mean the version changed, the entry was evicted or GitHub ref scope prevents access.

A restored browser must launch and render a page within ten seconds. If it cannot, installation runs `python -m playwright install --with-deps chromium` and verifies again. Each installation attempt has a 120 second budget, with a ten second kill grace, and launch verification has a thirty second limit. The action retries once in the same job and fails if neither attempt succeeds. A cache hit alone never establishes browser readiness, and a failed browser setup must remain a failed check.

See [Tests workflow timing](../ci-timing.md) for delivery tiers and CI measurements.
