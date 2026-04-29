"""Tests for hooks.context.kubectl_mutation_guard.

Validates the HARD-FLOOR live-patching guard against the doctrine in
CI Manifesto §3.5 + rules/code-is-source.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hooks.context.kubectl_mutation_guard import (
    check_kubectl_mutation_command,
)

# ---------------------------------------------------------------------------
# ALLOWED — read-only diagnostics
# ---------------------------------------------------------------------------

ALLOWED_COMMANDS = [
    # kubectl read-only
    "kubectl logs deploy/foo -n bar",
    "kubectl get pods",
    "kubectl get pods -A",
    "kubectl describe deploy/foo",
    "kubectl top pods -n anton-prod",
    "kubectl explain deployment.spec",
    "kubectl events -n anton-dev",
    "kubectl auth can-i list pods",
    # kubectl exec read-only
    "kubectl exec mypod -- ls /tmp",
    "kubectl exec mypod -- cat /etc/config.yaml",
    "kubectl exec mypod -- ps aux",
    "kubectl exec mypod -- env",
    "kubectl exec mypod -- printenv FOO",
    "kubectl exec mypod -- df -h",
    "kubectl exec mypod -- ss -tlnp",
    "kubectl exec mypod -- curl http://localhost:8080/health",
    "kubectl exec mypod -- python3 -c 'import os; print(os.environ.get(\"FOO\"))'",
    # outer redirect to LOCAL filesystem (kubectl prints to stdout, shell
    # redirects locally — pod is read-only)
    "kubectl exec mypod -- cat /etc/config.yaml > /tmp/local-copy.yaml",
    # outer pipe to local tee — same: kubectl prints, local shell pipes/tees
    "kubectl exec mypod -- cat data.txt | tee /tmp/local-saved",
    # kubectl cp OUT of pod
    "kubectl cp mypod:/etc/config.yaml /tmp/config.yaml",
    "kubectl cp anton-prod/mypod:/var/log/app.log /tmp/app.log",
    # lifecycle / troubleshooting ops — allowed (these are operational
    # actions, not behavior changes encoded into the cluster)
    "kubectl delete pod mypod -n anton-dev",
    "kubectl port-forward svc/myservice 8080:80",
    "kubectl rollout restart deploy/foo",
    "kubectl rollout pause deploy/foo",
    "kubectl rollout resume deploy/foo",
    "kubectl scale deploy foo --replicas=5",
    "kubectl drain node-1 --ignore-daemonsets",
    "kubectl cordon node-1",
    "kubectl uncordon node-1",
    "kubectl taint node node-1 key=value:NoSchedule",
    "kubectl debug pod/foo --image=busybox",
    "kubectl annotate deploy foo key=value",
    "kubectl label pod foo new-label=true",
    "kubectl create configmap myconfig --from-literal=key=value",
    "kubectl create secret generic mysecret --from-literal=key=value",
    "kubectl create deployment foo --image=foo:latest",
    "kubectl apply -f manifest.yaml",
    "kubectl replace -f manifest.yaml",
    # SSH read-only
    "ssh anton ls /tmp",
    "ssh anton 'cat /etc/hostname'",
    "ssh anton 'journalctl -u nginx --no-pager -n 50'",
    "ssh anton 'docker ps'",
    "ssh anton 'docker logs mycontainer --tail 100'",
    "ssh anton 'systemctl status nginx'",
    # scp OUT of host
    "scp anton:/var/log/syslog /tmp/syslog",
    # Generic non-kubectl commands
    "git status",
    "ls -la",
    "cat /tmp/foo.yaml",
    "grep -rn 'foo' .",
    "python3 -c 'print(1+1)'",
    # Unrelated commands that happen to contain matching words
    "echo 'patch this manifest in the chart, then push'",
    "git log --oneline | grep edit",
    # TEXT MENTIONS: kubectl/helm strings inside quoted arguments must NOT
    # block — they are content, not invocations. Critical for commit
    # messages, doc generation, echo statements, etc.
    'git commit -m "doctrine: forbid kubectl edit and kubectl patch fleet-wide"',
    "git commit -m 'helm upgrade outside CI is now blocked'",
    'echo "Forbidden patterns: kubectl exec writes, kubectl cp INTO pod, kubectl edit"',
    'printf "Rule names: kubectl-edit, kubectl-patch, kubectl-set\\n"',
    "cat <<EOF\nDocument body talking about kubectl edit and helm upgrade\nEOF",
    # SSH text mentions inside commit/echo bodies — must not block
    'git commit -m "ssh-edit detection: blocks ssh with sed -i, vi, nano"',
    'echo "ssh into a pod" > /tmp/local-note',
    "git commit -m 'docs: list scp INTO host as forbidden'",
    # docker / kubectl / helm mentions in commit messages
    'git commit -m "docker exec writes blocked; kubectl edit blocked; helm upgrade blocked"',
]


# ---------------------------------------------------------------------------
# BLOCKED — state mutation
# ---------------------------------------------------------------------------

BLOCKED_COMMANDS = [
    # kubectl edit / patch / set / autoscale (manifest/behavior mutation only)
    ("kubectl edit deploy/foo", "kubectl-edit"),
    ("kubectl edit configmap myconfig -n anton-dev", "kubectl-edit"),
    ("kubectl patch deploy foo --type=strategic -p '{}'", "kubectl-patch"),
    ("kubectl set env deploy/foo FOO=bar", "kubectl-set"),
    ("kubectl set image deploy/foo container=img:tag", "kubectl-set"),
    ("kubectl autoscale deploy foo --min=2 --max=10", "kubectl-autoscale"),
    # kubectl exec write
    ("kubectl exec mypod -- tee /etc/config.yaml", "exec-tee"),
    ("kubectl exec mypod -- sed -i 's/x/y/' /etc/config", "exec-sed-i"),
    ("kubectl exec mypod -- vi /etc/config", "exec-editor"),
    ("kubectl exec mypod -- pip install requests", "exec-pkg-install"),
    ("kubectl exec mypod -- apt-get install vim", "exec-pkg-install"),
    ("kubectl exec mypod -- npm install lodash", "exec-pkg-install"),
    ("kubectl exec mypod -- rm -rf /var/log/old", "exec-fs-mutate"),
    ("kubectl exec mypod -- sh -c 'echo x > /etc/config'", "exec-shell-c:redirect-to-fs-path"),
    ('kubectl exec mypod -- bash -c "cat <<EOF > /etc/x.conf\\nfoo\\nEOF"', "exec-shell-c"),
    # kubectl cp INTO pod
    ("kubectl cp /tmp/local.yaml mypod:/etc/config.yaml", "kubectl-cp-into-pod"),
    ("kubectl cp /tmp/local mypod:/tmp/", "kubectl-cp-into-pod"),
    # helm
    ("helm install myrelease ./chart", "helm-deploy"),
    ("helm upgrade myrelease ./chart --set foo=bar", "helm-deploy"),
    ("helm rollback myrelease 3", "helm-deploy"),
    # argocd local sync
    ("argocd app sync myapp --local /tmp/manifests", "argocd-sync-local"),
    # ssh write
    ("ssh anton 'sed -i s/x/y/g /etc/foo'", "ssh-mutate:ssh-sed-i"),
    ("ssh anton 'echo new > /etc/motd'", "ssh-mutate:ssh-redirect"),
    ("ssh anton 'systemctl restart nginx'", "ssh-mutate:ssh-systemctl-mutate"),
    ("ssh anton 'apt-get install nginx'", "ssh-mutate:ssh-pkg-install"),
    ("ssh anton 'docker exec foo sh -c \"x\"'", "ssh-mutate:ssh-docker-exec"),
    # ssh-wrapped kubectl mutation: whole-command kubectl-edit is suppressed
    # by the quote-stripping (correctly — the kubectl is inside the ssh
    # quoted remote command, not a local invocation), but the SSH detection
    # parses inside and catches the kubectl-mutate.
    ("ssh anton 'kubectl edit deploy/foo'", "ssh-mutate:ssh-kubectl-mutate"),
    # scp INTO host
    ("scp /tmp/foo anton:/etc/foo", "scp-into-host"),
    ("scp ./local.txt user@anton:/var/foo.txt", "scp-into-host"),
    # docker exec writes
    ("docker exec mycon sh -c 'echo x > /etc/foo'", "docker-exec-mutate"),
    ("docker exec mycon apt-get install vim", "docker-exec-mutate"),
    ("docker exec mycon tee /etc/x", "docker-exec-mutate"),
    ("docker exec mycon sed -i s/x/y/ /etc/foo", "docker-exec-mutate"),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", ALLOWED_COMMANDS)
def test_allowed_commands_are_not_blocked(command):
    """Read-only diagnostic commands must NOT be blocked."""
    result = check_kubectl_mutation_command(command)
    assert result is None, (
        f"Command was unexpectedly blocked:\n  {command}\n\n  Result:\n{result}"
    )


@pytest.mark.parametrize("command,expected_rule", BLOCKED_COMMANDS)
def test_blocked_commands_are_blocked(command, expected_rule):
    """Mutating commands must be blocked with the expected rule key."""
    result = check_kubectl_mutation_command(command)
    assert result is not None, f"Command was unexpectedly allowed: {command}"
    assert "BLOCKED by kubectl_mutation_guard" in result
    assert expected_rule in result, (
        f"Expected rule '{expected_rule}' not in violation message:\n{result}"
    )


def test_empty_command_returns_none():
    assert check_kubectl_mutation_command("") is None
    assert check_kubectl_mutation_command("   ") is None


def test_case_insensitive_block():
    """Patterns must match regardless of case."""
    assert check_kubectl_mutation_command("KUBECTL EDIT deploy/foo") is not None
    assert check_kubectl_mutation_command("Kubectl Patch deploy/foo --type strategic -p '{}'") is not None


def test_chained_command_with_safe_prefix_still_blocks():
    """A safe-looking prefix doesn't sneak a mutation through."""
    cmd = "kubectl get pods && kubectl edit deploy/foo"
    assert check_kubectl_mutation_command(cmd) is not None


def test_check_kubectl_mutation_payload_raises_block_action():
    """The payload-based entrypoint raises BlockAction."""
    from hooks.context.kubectl_mutation_guard import check_kubectl_mutation
    from hooks.hook_manager import BlockAction

    payload = {"tool_input": {"command": "kubectl edit deploy/foo"}}
    with pytest.raises(BlockAction):
        check_kubectl_mutation(payload)


def test_check_kubectl_mutation_payload_allows_safe_command():
    """The payload-based entrypoint does NOT raise on safe commands."""
    from hooks.context.kubectl_mutation_guard import check_kubectl_mutation

    payload = {"tool_input": {"command": "kubectl get pods"}}
    check_kubectl_mutation(payload)  # No exception expected
