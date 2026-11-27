import subprocess


def anonymous(repo: str) -> subprocess.CompletedProcess[str]:
    # ruleid: lab-private-ref-listing
    return subprocess.run(["git", "ls-remote", repo], capture_output=True, text=True, check=True)


def anonymous_groups(repo: str) -> subprocess.CompletedProcess[str]:
    argv = ["git", "ls-remote", repo, "refs/heads/gh-readonly-queue/*"]
    # ruleid: lab-private-ref-listing
    return subprocess.run(argv, capture_output=True, text=True, check=True)


def authenticated(repo: str, environ: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # ok: lab-private-ref-listing
    return subprocess.run(["git", "ls-remote", repo], env=environ, capture_output=True, text=True, check=True)


def authenticated_groups(repo: str, environ: dict[str, str]) -> subprocess.CompletedProcess[str]:
    argv = ["git", "ls-remote", repo, "refs/heads/gh-readonly-queue/*"]
    # ok: lab-private-ref-listing
    return subprocess.run(argv, env=environ, capture_output=True, text=True, check=True)


def local_status(repo: str) -> subprocess.CompletedProcess[str]:
    # ok: lab-private-ref-listing
    return subprocess.run(["git", "-C", repo, "status"], capture_output=True, text=True, check=True)
