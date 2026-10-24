"""Tests for hooks.context.kubectl_mutation_guard.

Validates the HARD-FLOOR live-patching guard against the doctrine in
CI Manifesto §2 + rules/code-is-source.md.
"""

from __future__ import annotations

import pytest

from hooks.context import kubectl_mutation_guard as guard
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
    "kubectl events -n anton-prod",
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
    "kubectl delete pod mypod -n anton-prod",
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
    ("kubectl edit configmap myconfig -n anton-prod", "kubectl-edit"),
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
    assert result is None, f"Command was unexpectedly blocked:\n  {command}\n\n  Result:\n{result}"


@pytest.mark.parametrize("command,expected_rule", BLOCKED_COMMANDS)
def test_blocked_commands_are_blocked(command, expected_rule):
    """Mutating commands must be blocked with the expected rule key."""
    result = check_kubectl_mutation_command(command)
    assert result is not None, f"Command was unexpectedly allowed: {command}"
    assert "BLOCKED by kubectl_mutation_guard" in result
    assert expected_rule in result, f"Expected rule '{expected_rule}' not in violation message:\n{result}"


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


def test_check_kubectl_mutation_payload_raises_block_action(monkeypatch):
    """The payload-based entrypoint raises BlockAction."""
    from hooks.context import controls_toggle
    from hooks.context.kubectl_mutation_guard import check_kubectl_mutation
    from hooks.hook_manager import BlockAction

    # Bypass mode short-circuits the guard — force it off for this test.
    monkeypatch.setattr(controls_toggle, "is_controls_disabled", lambda *_a, **_kw: False)

    payload = {"tool_input": {"command": "kubectl edit deploy/foo"}}
    with pytest.raises(BlockAction):
        check_kubectl_mutation(payload)


def test_check_kubectl_mutation_payload_allows_safe_command():
    """The payload-based entrypoint does NOT raise on safe commands."""
    from hooks.context.kubectl_mutation_guard import check_kubectl_mutation

    payload = {"tool_input": {"command": "kubectl get pods"}}
    check_kubectl_mutation(payload)  # No exception expected


OTEL_EVENT = "agentihooks.guardrail.kubectl_mutation_detected"
HIVE_ALLOW = "agentihooks-swarm/agentihooks-swarm-worker-hive-*"


@pytest.fixture
def emitted(monkeypatch):
    from hooks.observability import otel

    events = []
    monkeypatch.setattr(otel, "emit_event", lambda name, attributes=None: events.append((name, attributes)))
    return events


def test_check_kubectl_mutation_payload_allows_on_bypass(monkeypatch, emitted):
    """Bypass mode (controls disabled) lets a mutating exec through and logs the pass-through."""
    from hooks.context import controls_toggle
    from hooks.context.kubectl_mutation_guard import check_kubectl_mutation

    monkeypatch.setattr(controls_toggle, "is_controls_disabled", lambda sid: sid == "s1")

    payload = {"session_id": "s1", "tool_input": {"command": "kubectl exec mypod -- sh -c 'echo x > /etc/config'"}}
    check_kubectl_mutation(payload)
    assert emitted == [(OTEL_EVENT, {"session.id": "s1", "action": "allow", "bypass": True})]


def test_bypass_lookup_failure_keeps_the_guard(monkeypatch, emitted):
    """A failing bypass lookup counts as controls on, so the mutation stays blocked."""
    from hooks.context import controls_toggle
    from hooks.context.kubectl_mutation_guard import check_kubectl_mutation
    from hooks.hook_manager import BlockAction

    def broken(_sid):
        raise RuntimeError("controls store unreadable")

    monkeypatch.setattr(controls_toggle, "is_controls_disabled", broken)
    assert guard._controls_disabled("s1") is False
    with pytest.raises(BlockAction):
        check_kubectl_mutation({"session_id": "s1", "tool_input": {"command": "kubectl edit deploy/foo"}})
    assert emitted == []


def test_violation_message_names_doctrine_and_volunteers_no_unlock():
    msg = check_kubectl_mutation_command("kubectl exec -n other other-pod -- tee /etc/config.yaml")
    assert "  Rule:        exec-tee\n" in msg
    assert "  Reason:      kubectl exec ... -- tee (writes a file inside the pod)\n" in msg
    assert "\n  Doctrine:    CI Manifesto §2 + rules/code-is-source.md\n  Action:" in msg
    assert msg.endswith("\n  Action:      change the source code, commit, push to dev, let CI deploy.")
    assert "KUBECTL_MUTATION_GUARD_EXEC_ALLOW" not in msg
    assert "disable controls" not in msg


@pytest.mark.parametrize(
    "cmd",
    [
        "kubectl exec -n ns web-1 -- tee /etc/a; kubectl exec -n prod db-1 -- tee /etc/b",
        "kubectl exec -n ns web-1 -- sh -c 'echo x > /etc/a' && kubectl exec -n prod db-1 -- tee /etc/b",
        "kubectl cp ./a ns/web-1:/etc/a && kubectl cp ./b prod/db-1:/etc/b",
        "kubectl exec -n ns web-1 -- tee /etc/a; kubectl edit deploy/foo -n prod",
        "kubectl exec -n ns web-1 -- tee /etc/a; ssh host 'echo x > /etc/b'",
    ],
)
def test_allowlisted_segment_does_not_hide_a_later_mutation(monkeypatch, cmd):
    monkeypatch.setenv("KUBECTL_MUTATION_GUARD_EXEC_ALLOW", "ns/web-*")
    assert check_kubectl_mutation_command(cmd) is not None


def test_violation_message_carries_the_rule_description():
    msg = check_kubectl_mutation_command("kubectl exec mypod -- vim /etc/config")
    assert "  Reason:      kubectl exec ... -- vi/vim/nano/emacs (interactive file edit inside the pod)\n" in msg
    msg = check_kubectl_mutation_command("kubectl exec mypod -- sh -c 'echo x > /etc/config'")
    assert "  Rule:        exec-shell-c:redirect-to-fs-path\n" in msg
    assert "  Reason:      kubectl exec ... -- sh/bash -c with mutation pattern (redirect-to-fs-path)\n" in msg


@pytest.mark.parametrize(
    ("raw", "entries"),
    [
        (None, []),
        ("", []),
        ("a/b,c/d", ["a/b", "c/d"]),
        (" a/b , ,junk,*, c/* ", ["a/b", "c/*"]),
    ],
)
def test_exec_allowlist_entries(monkeypatch, raw, entries):
    if raw is None:
        monkeypatch.delenv("KUBECTL_MUTATION_GUARD_EXEC_ALLOW", raising=False)
    else:
        monkeypatch.setenv("KUBECTL_MUTATION_GUARD_EXEC_ALLOW", raw)
    assert guard._exec_allowlist_entries() == entries


@pytest.mark.parametrize(
    ("allow", "namespace", "pod", "expected"),
    [
        ("ns/*", "ns", None, False),
        ("ns/*", "ns", "", False),
        ("ns/*", "ns", "web-1", True),
        ("ns/*", "other", "web-1", False),
        ("/web-1", None, "web-1", True),
        ("ns/*", None, "web-1", False),
        ("x/y,ns/web-*", "ns", "web-1", True),
    ],
)
def test_target_allowlisted(monkeypatch, allow, namespace, pod, expected):
    monkeypatch.setenv("KUBECTL_MUTATION_GUARD_EXEC_ALLOW", allow)
    assert guard._target_allowlisted(namespace, pod) is expected


@pytest.mark.parametrize(
    ("args", "target"),
    [
        (" mypod ", (None, "mypod")),
        ("pod/mypod", (None, "mypod")),
        ("-n ns mypod", ("ns", "mypod")),
        ("--namespace ns mypod", ("ns", "mypod")),
        ("--namespace=ns mypod", ("ns", "mypod")),
        ("-n=ns mypod", ("ns", "mypod")),
        ("--namespace=a=b mypod", ("a=b", "mypod")),
        ("-c ctr mypod", (None, "mypod")),
        ("--container ctr mypod", (None, "mypod")),
        ("--container=ctr mypod", (None, "mypod")),
        ("mypod extra", (None, "mypod")),
        ("mypod -n", ("", "mypod")),
        ("-it", (None, None)),
        ("--context=web-1 mypod", (None, "mypod")),
        ("-n ns --context web-1 db-1", (None, None)),
        ("-n ns db-1 --as admin", (None, None)),
        ("'unbalanced", (None, None)),
    ],
)
def test_parse_kubectl_target(args, target):
    assert guard._parse_kubectl_target(args) == target


@pytest.mark.parametrize("flag", ["-i", "-t", "-it", "-ti", "--stdin", "--tty", "-q", "--quiet", "--no-preserve"])
def test_boolean_flags_keep_the_target(flag):
    assert guard._parse_kubectl_target(f"-n ns {flag} mypod") == ("ns", "mypod")


def test_unknown_value_flag_allowlists_nothing(monkeypatch, emitted):
    monkeypatch.setenv("KUBECTL_MUTATION_GUARD_EXEC_ALLOW", "ns/web-*")
    assert check_kubectl_mutation_command("kubectl exec -n ns --context web-1 db-1 -- tee /etc/x") is not None
    assert emitted == []


@pytest.mark.parametrize(
    ("args", "target"),
    [
        (" ./f ns/web-1:/etc/a ", ("ns", "web-1")),
        ("./f web-1:/etc/a", (None, "web-1")),
        ("-n ns ./f web-1:/etc/a", ("ns", "web-1")),
        ("-n ns ./f other/web-1:/etc/a", ("other", "web-1")),
        ("./f ns/web-1:/etc/a:b -c ctr", ("ns", "web-1")),
        ("./f", (None, None)),
        ("'unbalanced", (None, None)),
    ],
)
def test_cp_dest_target(args, target):
    assert guard._cp_dest_target(args) == target


def test_allowlisted_pod_lets_mutating_exec_through(monkeypatch, emitted):
    """A pod matching KUBECTL_MUTATION_GUARD_EXEC_ALLOW is let through and logged."""
    monkeypatch.setenv("KUBECTL_MUTATION_GUARD_EXEC_ALLOW", HIVE_ALLOW)
    cmd = "kubectl exec -n agentihooks-swarm agentihooks-swarm-worker-hive-3 -- tee /etc/config.yaml"
    assert check_kubectl_mutation_command(cmd) is None
    assert emitted == [
        (
            OTEL_EVENT,
            {
                "rule": "exec-tee",
                "namespace": "agentihooks-swarm",
                "pod": "agentihooks-swarm-worker-hive-3",
                "action": "allow",
                "allowlisted": True,
            },
        )
    ]


def test_allowlisted_pod_lets_shell_mutation_through(monkeypatch, emitted):
    monkeypatch.setenv("KUBECTL_MUTATION_GUARD_EXEC_ALLOW", HIVE_ALLOW)
    cmd = "kubectl exec -n agentihooks-swarm agentihooks-swarm-worker-hive-3 -- sh -c 'echo x > /etc/config'"
    assert check_kubectl_mutation_command(cmd) is None
    assert [attrs["rule"] for _name, attrs in emitted] == ["exec-shell-c:redirect-to-fs-path"]


def test_allowlist_without_namespace_logs_an_empty_namespace(monkeypatch, emitted):
    monkeypatch.setenv("KUBECTL_MUTATION_GUARD_EXEC_ALLOW", "/web-1")
    assert check_kubectl_mutation_command("kubectl exec web-1 -- tee /etc/config.yaml") is None
    assert emitted[0][1]["namespace"] == ""


def test_allowlisted_pod_lets_cp_into_pod_through(monkeypatch, emitted):
    monkeypatch.setenv("KUBECTL_MUTATION_GUARD_EXEC_ALLOW", "ns/web-*")
    assert check_kubectl_mutation_command("kubectl cp ./config ns/web-1:/etc/config") is None
    assert emitted[0][1] == {
        "rule": "kubectl-cp-into-pod",
        "namespace": "ns",
        "pod": "web-1",
        "action": "allow",
        "allowlisted": True,
    }


def test_cp_into_non_allowlisted_pod_still_blocked(monkeypatch, emitted):
    monkeypatch.setenv("KUBECTL_MUTATION_GUARD_EXEC_ALLOW", "ns/web-*")
    assert check_kubectl_mutation_command("kubectl cp ./config ns/db-1:/etc/config") is not None
    assert emitted == []


def test_non_allowlisted_pod_still_blocked(monkeypatch, emitted):
    """A pod NOT matching KUBECTL_MUTATION_GUARD_EXEC_ALLOW stays blocked."""
    monkeypatch.setenv("KUBECTL_MUTATION_GUARD_EXEC_ALLOW", HIVE_ALLOW)
    cmd = "kubectl exec -n other-namespace other-pod -- tee /etc/config.yaml"
    assert check_kubectl_mutation_command(cmd) is not None
    assert emitted == []


def test_malformed_allowlist_entry_blocks_nothing_extra(monkeypatch):
    """A malformed entry (no namespace/pod-glob shape) neither allows unrelated pods nor crashes."""
    monkeypatch.setenv("KUBECTL_MUTATION_GUARD_EXEC_ALLOW", "garbage-no-slash, , *")
    cmd = "kubectl exec -n agentihooks-swarm agentihooks-swarm-worker-hive-3 -- tee /etc/config.yaml"
    assert check_kubectl_mutation_command(cmd) is not None
