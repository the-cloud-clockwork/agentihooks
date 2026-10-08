import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

WT = Path(__file__).resolve().parents[1] / "profiles" / "package" / "skills" / "worktree" / "scripts" / "wt.sh"
BASH = shutil.which("bash")

ISOLATED_TOOLS = ["git", "basename", "dirname", "mkdir", "awk", "rmdir", "df", "mktemp"]


def _isolated_bin(dest):
    dest.mkdir(parents=True, exist_ok=True)
    for name in ISOLATED_TOOLS:
        real = shutil.which(name)
        assert real, f"required tool not found: {name}"
        os.symlink(real, dest / name)
    return dest


def _git(repo, *args, env=None):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


class WtBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wt-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        root = Path(self.tmp)

        gitenv = dict(os.environ)
        gitenv.update(
            {
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.com",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
                "HOME": str(root / "home"),
            }
        )
        (root / "home").mkdir()

        seed = root / "seed"
        _git(root, "init", "--quiet", "-b", "dev", str(seed), env=gitenv)
        (seed / "README.md").write_text("seed\n")
        _git(seed, "add", "README.md", env=gitenv)
        _git(seed, "commit", "--quiet", "-m", "initial", env=gitenv)

        self.origin = root / "origin.git"
        _git(root, "init", "--quiet", "--bare", str(self.origin), env=gitenv)
        _git(seed, "remote", "add", "origin", str(self.origin), env=gitenv)
        _git(seed, "push", "--quiet", "origin", "dev", env=gitenv)
        _git(self.origin, "symbolic-ref", "HEAD", "refs/heads/dev", env=gitenv)

        self.primary = root / "primary"
        _git(root, "clone", "--quiet", str(self.origin), str(self.primary), env=gitenv)
        _git(self.primary, "checkout", "--quiet", "dev", env=gitenv)

        self.worktree_root = root / "worktrees"
        self.bin = _isolated_bin(root / "bin")
        self.env = {
            "PATH": str(self.bin),
            "HOME": str(root / "home"),
            "WORKTREE_ROOT": str(self.worktree_root),
            "WT_MIN_FREE_GB": "0",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        }
        self.gitenv = gitenv

    def run_wt(self, *args):
        return subprocess.run(
            [BASH, str(WT), *args],
            cwd=str(self.primary),
            env=self.env,
            capture_output=True,
            text=True,
        )

    def branch_of(self, path):
        return subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            env=self.gitenv,
        ).stdout.strip()

    def branch_exists(self, name):
        return (
            subprocess.run(
                ["git", "-C", str(self.primary), "show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
                env=self.gitenv,
            ).returncode
            == 0
        )


class New(WtBase):
    def test_new_creates_worktree_on_branch_and_prints_path(self):
        result = self.run_wt("new", "feature-x", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = Path(result.stdout.strip())
        self.assertEqual(dest, self.worktree_root / "primary" / "feature-x")
        self.assertTrue(dest.is_dir())
        self.assertEqual(self.branch_of(dest), "feature-x")

    def test_new_refuses_protected_names(self):
        for name in ("dev", "main", "master"):
            result = self.run_wt("new", name, "--repo", str(self.primary))
            self.assertNotEqual(result.returncode, 0, name)
            self.assertIn("protected branch name", result.stderr)


class Ls(WtBase):
    def test_ls_lists_the_new_worktree(self):
        created = self.run_wt("new", "list-me", "--repo", str(self.primary))
        dest = created.stdout.strip()
        result = self.run_wt("ls", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(dest, result.stdout)
        self.assertIn("list-me", result.stdout)


class Done(WtBase):
    def _new(self, name):
        return Path(self.run_wt("new", name, "--repo", str(self.primary)).stdout.strip())

    def test_done_refuses_a_dirty_worktree(self):
        dest = self._new("dirty-one")
        (dest / "scratch.txt").write_text("uncommitted\n")
        result = self.run_wt("done", "dirty-one", "--repo", str(self.primary))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uncommitted changes", result.stderr)
        self.assertTrue(dest.is_dir())

    def test_done_force_removes_a_dirty_worktree(self):
        dest = self._new("dirty-two")
        (dest / "scratch.txt").write_text("uncommitted\n")
        result = self.run_wt("done", "dirty-two", "--repo", str(self.primary), "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(dest.exists())

    def test_done_deletes_branch_already_merged_into_dev(self):
        dest = self._new("merged-one")
        result = self.run_wt("done", "merged-one", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse(self.branch_exists("merged-one"))
        self.assertIn("and branch merged-one", result.stdout)

    def test_done_keeps_an_unmerged_branch_without_gh(self):
        dest = self._new("unmerged-one")
        (dest / "new-file.txt").write_text("extra\n")
        _git(dest, "add", "new-file.txt", env=self.gitenv)
        _git(dest, "commit", "--quiet", "-m", "extra commit", env=self.gitenv)
        result = self.run_wt("done", "unmerged-one", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(dest.exists())
        self.assertTrue(self.branch_exists("unmerged-one"))
        self.assertIn("kept", result.stderr)

    def test_done_preserves_an_open_pull_request_worktree_until_the_queue_lands(self):
        dest = self._new("queue-one")
        state = Path(self.tmp) / "pr-state"
        state.write_text("open")
        gh = self.bin / "gh"
        gh.write_text(
            f"#!{BASH}\nset -euo pipefail\n"
            f'current="$(<"{state}")"\n'
            'if [[ "$current" != merged && ( "$*" == *"--state all"* || "$current" != closed ) ]]; then\n'
            '  echo "https://github.com/o/r/pull/9"\n'
            "fi\n"
        )
        gh.chmod(0o755)
        for value in ("open", "dropped", "closed"):
            state.write_text(value)
            result = self.run_wt("done", "queue-one", "--repo", str(self.primary))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not merged", result.stderr)
            self.assertTrue(dest.is_dir())
            self.assertEqual(self.branch_of(dest), "queue-one")
            self.assertTrue(self.branch_exists("queue-one"))
        state.write_text("merged")
        result = self.run_wt("done", "queue-one", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse(self.branch_exists("queue-one"))

    def test_done_deletes_the_remote_branch_only_after_the_pull_request_merges(self):
        dest = self._new("remote-queue")
        _git(dest, "push", "--quiet", "-u", "origin", "remote-queue", env=self.gitenv)
        state = Path(self.tmp) / "remote-pr-state"
        gh = self.bin / "gh"
        gh.write_text(
            f"#!{BASH}\nset -euo pipefail\n"
            f'current="$(<"{state}")"\n'
            'if [[ "$current" == missing ]]; then exit 0; fi\n'
            'if [[ "$current" == merged ]]; then\n'
            '  echo "MERGED"\n'
            "else\n"
            '  echo "https://github.com/o/r/pull/9"\n'
            "fi\n"
        )
        gh.chmod(0o755)
        for value in ("queued", "open", "closed", "missing"):
            state.write_text(value)
            result = self.run_wt("done", "remote-queue", "--repo", str(self.primary), "--force")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("merged", result.stderr)
            self.assertTrue(dest.is_dir())
            self.assertTrue(self.branch_exists("remote-queue"))
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(self.origin), "show-ref", "--verify", "--quiet", "refs/heads/remote-queue"],
                    env=self.gitenv,
                ).returncode,
                0,
            )
        state.write_text("merged")
        result = self.run_wt("done", "remote-queue", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse(self.branch_exists("remote-queue"))
        self.assertNotEqual(
            subprocess.run(
                ["git", "-C", str(self.origin), "show-ref", "--verify", "--quiet", "refs/heads/remote-queue"],
                env=self.gitenv,
            ).returncode,
            0,
        )

    def test_done_keeps_the_worktree_when_github_cannot_read_its_pull_request(self):
        dest = self._new("queue-unknown")
        gh = self.bin / "gh"
        gh.write_text(f"#!{BASH}\nset -euo pipefail\nexit 1\n")
        gh.chmod(0o755)
        result = self.run_wt("done", "queue-unknown", "--repo", str(self.primary))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot read", result.stderr)
        self.assertTrue(dest.is_dir())
        self.assertTrue(self.branch_exists("queue-unknown"))

    def test_done_keeps_a_published_worktree_when_github_cli_is_missing_even_with_force(self):
        dest = self._new("queue-no-gh")
        _git(dest, "push", "--quiet", "-u", "origin", "queue-no-gh", env=self.gitenv)
        for flags in ((), ("--force",)):
            result = self.run_wt("done", "queue-no-gh", "--repo", str(self.primary), *flags)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot verify", result.stderr)
            self.assertTrue(dest.is_dir())
            self.assertTrue(self.branch_exists("queue-no-gh"))

    def _published_with_pull_requests(self, name, pulls):
        jq = shutil.which("jq")
        if not jq:
            self.skipTest("jq is required to evaluate the gh query")
        dest = self._new(name)
        (dest / "probe.txt").write_text("probe\n")
        _git(dest, "add", "probe.txt", env=self.gitenv)
        _git(dest, "commit", "--quiet", "-m", "probe", env=self.gitenv)
        _git(dest, "push", "--quiet", "-u", "origin", name, env=self.gitenv)
        listing = Path(self.tmp) / f"{name}-pulls.json"
        listing.write_text(pulls)
        gh = self.bin / "gh"
        gh.write_text(
            f"#!{BASH}\nset -euo pipefail\n"
            'query=""; state=all\n'
            "while (($#)); do\n"
            '  case "$1" in --jq) query="$2"; shift ;; --state) state="$2"; shift ;; esac\n'
            "  shift\n"
            "done\n"
            f'"{jq}" -r --arg s "$state" '
            '"map(select(\\$s == \\"all\\" or .state == (\\$s | ascii_upcase))) | ($query) | values"'
            f' < "{listing}"\n'
        )
        gh.chmod(0o755)
        return dest

    def remote_branch_exists(self, name):
        return (
            subprocess.run(
                ["git", "-C", str(self.origin), "show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
                env=self.gitenv,
            ).returncode
            == 0
        )

    def test_done_force_drops_a_worktree_whose_pull_request_closed_unmerged(self):
        dest = self._published_with_pull_requests(
            "closed-probe", '[{"state": "CLOSED", "url": "https://github.com/o/r/pull/7"}]'
        )
        result = self.run_wt("done", "closed-probe", "--repo", str(self.primary), "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse(self.branch_exists("closed-probe"))
        self.assertTrue(self.remote_branch_exists("closed-probe"))
        self.assertIn(f"wt: removed {dest} and branch closed-probe\n", result.stdout)
        self.assertNotIn("kept", result.stderr)

    def test_done_without_force_keeps_a_worktree_whose_pull_request_closed_unmerged(self):
        dest = self._published_with_pull_requests(
            "closed-kept", '[{"state": "CLOSED", "url": "https://github.com/o/r/pull/7"}]'
        )
        result = self.run_wt("done", "closed-kept", "--repo", str(self.primary))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            result.stderr,
            "wt: pull request https://github.com/o/r/pull/7 was closed without merging"
            " — pass --force to drop the worktree and branch closed-kept\n",
        )
        self.assertTrue(dest.is_dir())
        self.assertTrue(self.branch_exists("closed-kept"))
        self.assertTrue(self.remote_branch_exists("closed-kept"))

    def test_done_refuses_an_open_or_queued_pull_request_even_with_force(self):
        pulls = {
            "open": '[{"state": "OPEN", "url": "https://github.com/o/r/pull/8"}]',
            "queued-reads-open": '[{"state": "OPEN", "url": "https://github.com/o/r/pull/8"},'
            ' {"state": "CLOSED", "url": "https://github.com/o/r/pull/6"}]',
        }
        for kind, listing in pulls.items():
            name = f"{kind}-pull"
            dest = self._published_with_pull_requests(name, listing)
            for flags in ((), ("--force",)):
                result = self.run_wt("done", name, "--repo", str(self.primary), *flags)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    result.stderr,
                    "wt: pull request https://github.com/o/r/pull/8 is not merged"
                    " — wait for it to land before worktree teardown\n",
                )
                self.assertTrue(dest.is_dir())
                self.assertTrue(self.branch_exists(name))
                self.assertTrue(self.remote_branch_exists(name))


CONCURRENT_GIT = """#!{bash}
"{real}" "$@"
rc=$?
if [[ " $* " == *" fetch "* ]]; then
  GIT_EXEC_PATH="{exec_path}" "{real}" -C "{primary}" fetch --quiet origin dev side
fi
exit $rc
"""


class DoneSync(WtBase):
    def _advance(self, clone, branch, name):
        _git(clone, "checkout", "--quiet", "-B", branch, "origin/dev", env=self.gitenv)
        (clone / name).write_text(f"{name}\n")
        _git(clone, "add", name, env=self.gitenv)
        _git(clone, "commit", "--quiet", "-m", name, env=self.gitenv)
        _git(clone, "push", "--quiet", "origin", branch, env=self.gitenv)

    def _arm_concurrent_fetch(self):
        real = shutil.which("git")
        exec_path = subprocess.run([real, "--exec-path"], capture_output=True, text=True, check=True).stdout.strip()
        shim = Path(self.tmp) / "git-exec"
        shim.mkdir()
        for entry in Path(exec_path).iterdir():
            os.symlink(entry, shim / entry.name)
        script = CONCURRENT_GIT.format(bash=BASH, real=real, exec_path=exec_path, primary=self.primary)
        for path in (shim / "git", self.bin / "git"):
            path.unlink(missing_ok=True)
            path.write_text(script)
            path.chmod(0o755)
        self.env["GIT_EXEC_PATH"] = str(shim)

    def test_done_syncs_dev_while_another_session_fetches_two_advanced_branches(self):
        self.run_wt("new", "racing", "--repo", str(self.primary))
        other = Path(self.tmp) / "other"
        _git(Path(self.tmp), "clone", "--quiet", str(self.origin), str(other), env=self.gitenv)
        self._advance(other, "dev", "dev-advance.txt")
        self._advance(other, "side", "side-advance.txt")
        dev_sha = subprocess.run(
            ["git", "-C", str(self.origin), "rev-parse", "dev"], capture_output=True, text=True, env=self.gitenv
        ).stdout.strip()
        self._arm_concurrent_fetch()
        result = self.run_wt("done", "racing", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("local dev synced to origin/dev", result.stdout, result.stderr)
        head = subprocess.run(
            ["git", "-C", str(self.primary), "rev-parse", "HEAD"], capture_output=True, text=True, env=self.gitenv
        ).stdout.strip()
        self.assertEqual(head, dev_sha)


class Limits(WtBase):
    def test_new_refuses_below_the_free_space_floor(self):
        self.env["WT_MIN_FREE_GB"] = "999999"
        result = self.run_wt("new", "too-full", "--repo", str(self.primary))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("below the 999999 GB floor", result.stderr)
        self.assertFalse((self.worktree_root / "primary" / "too-full").exists())

    def test_new_and_tmp_refuse_at_the_per_repo_cap(self):
        self.env["WT_MAX_PER_REPO"] = "1"
        self.assertEqual(self.run_wt("new", "first", "--repo", str(self.primary)).returncode, 0)
        for args in (("new", "second"), ("tmp", "third")):
            result = self.run_wt(*args, "--repo", str(self.primary))
            self.assertNotEqual(result.returncode, 0, args)
            self.assertIn("already has 1 worktrees (cap 1", result.stderr)


class Tmp(WtBase):
    def test_tmp_is_detached_and_done_removes_it_even_when_dirty(self):
        result = self.run_wt("tmp", "plant", "--repo", str(self.primary), "--from", "origin/dev")
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = Path(result.stdout.strip())
        self.assertEqual(dest.parent, self.worktree_root / "primary" / "_tmp")
        self.assertTrue(dest.name.startswith("plant-"))
        self.assertEqual(self.branch_of(dest), "HEAD")
        (dest / "planted.txt").write_text("fault\n")
        removed = self.run_wt("done", str(dest))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(dest.exists())
        self.assertFalse((self.worktree_root / "primary" / "_tmp").exists())

    def test_tmp_rejects_an_unknown_ref(self):
        result = self.run_wt("tmp", "bad", "--repo", str(self.primary), "--from", "no-such-ref")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list((self.worktree_root / "primary" / "_tmp").iterdir()), [])


class BaseBranch(WtBase):
    def setUp(self):
        super().setUp()
        _git(self.primary, "checkout", "--quiet", "-b", "trunk", env=self.gitenv)
        (self.primary / "trunk.txt").write_text("trunk only\n")
        _git(self.primary, "add", "trunk.txt", env=self.gitenv)
        _git(self.primary, "commit", "--quiet", "-m", "trunk", env=self.gitenv)
        _git(self.primary, "push", "--quiet", "origin", "trunk", env=self.gitenv)
        self.env["WT_BASE_BRANCH"] = "trunk"

    def test_new_cuts_from_the_configured_base_branch(self):
        result = self.run_wt("new", "on-trunk", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((Path(result.stdout.strip()) / "trunk.txt").is_file())

    def test_the_base_branch_is_a_protected_name(self):
        result = self.run_wt("new", "trunk", "--repo", str(self.primary))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("protected branch name", result.stderr)

    def test_ls_and_done_follow_the_base_branch(self):
        self.run_wt("new", "follow", "--repo", str(self.primary))
        listed = self.run_wt("ls", "--repo", str(self.primary))
        self.assertIn("vs origin/trunk", listed.stdout)
        done = self.run_wt("done", "follow", "--repo", str(self.primary))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("local trunk synced to origin/trunk", done.stdout)
        self.assertIn("and branch follow", done.stdout)

    def test_tmp_defaults_to_the_base_branch(self):
        result = self.run_wt("tmp", "probe", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((Path(result.stdout.strip()) / "trunk.txt").is_file())


class FromRef(WtBase):
    def setUp(self):
        super().setUp()
        other = Path(self.tmp) / "other"
        _git(Path(self.tmp), "clone", "--quiet", str(self.origin), str(other), env=self.gitenv)
        _git(other, "checkout", "--quiet", "-b", "dep", env=self.gitenv)
        (other / "dep.txt").write_text("dependency work\n")
        _git(other, "add", "dep.txt", env=self.gitenv)
        _git(other, "commit", "--quiet", "-m", "dep", env=self.gitenv)
        _git(other, "push", "--quiet", "origin", "dep", env=self.gitenv)
        self.dep_sha = self.rev(other, "HEAD")
        self.dev_sha = self.rev(self.origin, "dev")

    def rev(self, repo, ref):
        return subprocess.run(
            ["git", "-C", str(repo), "rev-parse", ref], capture_output=True, text=True, env=self.gitenv
        ).stdout.strip()

    def test_new_from_a_ref_starts_at_that_ref(self):
        result = self.run_wt("new", "stacked", "--repo", str(self.primary), "--from", "origin/dep")
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = Path(result.stdout.strip())
        self.assertEqual(dest, self.worktree_root / "primary" / "stacked")
        self.assertEqual(self.branch_of(dest), "stacked")
        self.assertEqual(self.rev(dest, "HEAD"), self.dep_sha)
        self.assertTrue((dest / "dep.txt").is_file())

    def test_new_without_from_starts_at_origin_dev(self):
        result = self.run_wt("new", "plain", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = Path(result.stdout.strip())
        self.assertEqual(self.rev(dest, "HEAD"), self.dev_sha)
        self.assertFalse((dest / "dep.txt").exists())

    def test_new_refuses_an_unknown_ref_and_leaves_nothing(self):
        result = self.run_wt("new", "orphan", "--repo", str(self.primary), "--from", "origin/no-such-branch")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.worktree_root / "primary" / "orphan").exists())
        self.assertFalse(self.branch_exists("orphan"))

    def test_new_from_a_ref_still_refuses_protected_names(self):
        result = self.run_wt("new", "dev", "--repo", str(self.primary), "--from", "origin/dep")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("protected branch name", result.stderr)

    def test_done_still_judges_merged_against_origin_dev(self):
        self.run_wt("new", "stacked-done", "--repo", str(self.primary), "--from", "origin/dep")
        result = self.run_wt("done", "stacked-done", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.branch_exists("stacked-done"))
        self.assertIn("commits not on origin/dev", result.stderr)


class Lease(WtBase):
    def test_new_and_tmp_record_the_owner_through_agentihooks(self):
        log = Path(self.tmp) / "lease.log"
        fake = self.bin / "agentihooks"
        fake.write_text(f'#!{BASH}\necho "$@" >> {log}\n')
        fake.chmod(0o755)
        new = self.run_wt("new", "owned", "--repo", str(self.primary)).stdout.strip()
        tmp = self.run_wt("tmp", "probe", "--repo", str(self.primary)).stdout.strip()
        leases = [line for line in log.read_text().splitlines() if line.startswith("lease ")]
        self.assertEqual(leases, [f"lease {new} --kind worktree", f"lease {tmp} --kind ephemeral"])

    def test_done_releases_the_worktree_serena_backend_before_removing_it(self):
        log = Path(self.tmp) / "lease.log"
        fake = self.bin / "agentihooks"
        fake.write_text(f'#!{BASH}\necho "$@" >> {log}\n')
        fake.chmod(0o755)
        new = self.run_wt("new", "released", "--repo", str(self.primary)).stdout.strip()
        self.run_wt("done", "released", "--repo", str(self.primary))
        self.assertIn(f"serena release {new}", log.read_text().splitlines())


NAMER = """#!{bash}
[[ "$1" == name ]] || exit 0
base=engineer-a1b2c3-0002
if [[ -n "${{NO_SESSION:-}}" ]]; then echo "name: no session" >&2; exit 3; fi
if [[ "$2" == tmp ]]; then built="$base-tmp-1"; else built="$base"; fi
check=""
while [[ $# -gt 0 ]]; do [[ "$1" == --check ]] && check="$2"; shift; done
if [[ -z "$check" ]]; then echo "$built"; exit 0; fi
[[ "$check" == "$base" || "$check" == "$base"-* ]] && exit 0
echo "name: names come from code; use $built" >&2
exit 1
"""


class Names(WtBase):
    def setUp(self):
        super().setUp()
        fake = self.bin / "agentihooks"
        fake.write_text(NAMER.format(bash=BASH))
        fake.chmod(0o755)

    def test_new_without_a_name_takes_the_built_name(self):
        result = self.run_wt("new", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        dest = Path(result.stdout.strip())
        self.assertEqual(dest, self.worktree_root / "primary" / "engineer-a1b2c3-0002")
        self.assertEqual(self.branch_of(dest), "engineer-a1b2c3-0002")

    def test_new_refuses_a_name_the_session_did_not_build(self):
        result = self.run_wt("new", "my-feature", "--repo", str(self.primary))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("names come from code", result.stderr)
        self.assertFalse((self.worktree_root / "primary" / "my-feature").exists())

    def test_tmp_without_a_name_takes_the_built_name_exactly(self):
        result = self.run_wt("tmp", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            Path(result.stdout.strip()), self.worktree_root / "primary" / "_tmp" / "engineer-a1b2c3-0002-tmp-1"
        )

    def test_a_given_name_stands_outside_an_agent_session(self):
        self.env["NO_SESSION"] = "1"
        result = self.run_wt("new", "operator-fix", "--repo", str(self.primary))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()).name, "operator-fix")


if __name__ == "__main__":
    unittest.main()
