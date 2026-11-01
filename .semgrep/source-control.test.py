import subprocess


def direct(url):
    # ruleid: swarm-source-control-port
    return subprocess.run(["gh", "pr", "view", url], capture_output=True)


def spread(args):
    # ruleid: swarm-source-control-port
    return subprocess.run(["gh", *args], capture_output=True)


def other(root):
    # ok: swarm-source-control-port
    return subprocess.run(["git", "-C", str(root), "status"], capture_output=True)
