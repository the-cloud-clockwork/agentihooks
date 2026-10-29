import io
import json

from scripts.gates import Call, Gate, Who, entry, modes
from scripts.gates.node_caps import NodeCaps, caps, raises, refusal, resizes

ME = Who(name="engineer@1-1", swarm="demo", lane="eng", task="t1")
AUTOSCALER = "            - --nodes=0:15:$(OCI_INSTANCE_POOL_ID)\n            - --nodes=2:8:$(OCI_SWARM_POOL_ID)\n"


def rows(tmp_path):
    path = tmp_path / "demo" / "gates" / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_it_is_a_gate_that_ships_log_only():
    gate = NodeCaps()
    assert isinstance(gate, Gate)
    assert (gate.name, gate.default_mode) == ("node-caps", "observe")
    assert isinstance(entry.HOST_GATES["node-caps"], NodeCaps)


def test_a_raised_autoscaler_pool_maximum_is_named():
    after = AUTOSCALER.replace("0:15:", "0:20:")
    assert raises(AUTOSCALER, after) == ["--nodes $(OCI_INSTANCE_POOL_ID) 15 to 20"]


def test_the_refusal_names_the_raise_and_the_way_out():
    assert refusal(["maxRunners 30 to 40", "max_nodes 1 to 2"]) == (
        "node cap: maxRunners 30 to 40, max_nodes 1 to 2 raises an infrastructure node cap, which only the operator authorizes. Send "
        "the ask to the front door master by inbox and continue other work."
    )


def test_a_write_of_a_new_file_raises_nothing(tmp_path):
    call = Call("Write", {"file_path": str(tmp_path / "new.yaml"), "content": "maxRunners: 99\n"})
    assert NodeCaps().decide(call, ME, None).allowed


def test_an_edit_replaces_only_the_first_match(tmp_path):
    target = tmp_path / "deployment.yaml"
    target.write_text("--nodes=0:15:a\n--nodes=0:15:b\n")
    call = Call("Edit", {"file_path": str(target), "old_string": "0:15:", "new_string": "0:40:"})
    assert NodeCaps().decide(call, ME, None).reason == refusal(["--nodes a 15 to 40"])


def test_a_multi_edit_of_a_missing_file_reads_each_edit_on_its_own_line():
    edits = [
        {"old_string": "maxRunners: 2", "new_string": "maxRunners: 3"},
        {"old_string": "max_nodes: 1", "new_string": "max_nodes: 1"},
    ]
    call = Call("MultiEdit", {"file_path": "/nowhere/values.yaml", "edits": edits})
    assert NodeCaps().decide(call, ME, None).reason == refusal(["maxRunners 2 to 3"])


def test_a_repeated_cap_is_read_as_its_largest_value():
    text = "--nodes=0:9:pool\n--nodes=0:4:pool\nmaxRunners: 7\nmaxRunners: 3\n"
    assert caps(text) == {"--nodes pool": 9, "maxRunners": 7}


def test_a_multi_edit_skips_entries_that_are_not_edits(tmp_path):
    target = tmp_path / "values.yaml"
    target.write_text("maxRunners: 30\n")
    edits = ["30", {"old_string": "30", "new_string": "35"}]
    call = Call("MultiEdit", {"file_path": str(target), "edits": edits})
    assert NodeCaps().decide(call, ME, None).reason == refusal(["maxRunners 30 to 35"])


def test_lowered_unchanged_and_new_caps_are_not_raises():
    assert raises(AUTOSCALER, AUTOSCALER.replace("0:15:", "0:10:")) == []
    assert raises(AUTOSCALER, AUTOSCALER) == []
    assert raises("", "maxRunners: 30\n") == []


def test_runner_and_pool_size_keys_are_compared_as_integers():
    assert raises("maxRunners: 30\n", "maxRunners: 40\n") == ["maxRunners 30 to 40"]
    assert raises("  max_nodes: 0\n", "  max_nodes: 4\n") == ["max_nodes 0 to 4"]
    assert raises("maxSize: 8Gi\n", "maxSize: 16Gi\n") == []
    assert raises("max_size = 3\n", "max_size = 5\n") == ["max_size 3 to 5"]
    assert raises('{"maxRunners": 30,\n', '{"maxRunners": 40,\n') == ["maxRunners 30 to 40"]
    assert raises("max_nodes: 2  # cap\n", "max_nodes: 9  # cap\n") == ["max_nodes 2 to 9"]


def test_node_pool_resize_commands_are_recognised():
    assert resizes("oci compute-management instance-pool update --instance-pool-id x --size 20")
    assert resizes("eksctl scale nodegroup --cluster c --name n --nodes-max 9")
    assert resizes('kubectl patch autoscalingrunnerset anton-oss-batch --type merge -p \'{"spec":{"maxRunners":40}}\'')
    assert resizes("aws eks update-nodegroup-config --cluster c --scaling-config minSize=1,maxSize=9")
    assert resizes("gcloud container clusters resize c --node-pool p --num-nodes 9")
    assert not resizes("oci compute-management instance-pool get --instance-pool-id x")
    assert not resizes("grep maxRunners values.yaml")
    assert not resizes("kubectl get nodes")


def test_an_edit_that_raises_a_cap_is_a_would_be_deny(tmp_path):
    target = tmp_path / "deployment.yaml"
    target.write_text(AUTOSCALER)
    call = Call("Edit", {"file_path": str(target), "old_string": "0:15:", "new_string": "0:40:"})
    gate = NodeCaps()
    assert gate.matches(call)
    decision = gate.decide(call, ME, None)
    assert (decision.allowed, decision.reason) == (False, refusal(["--nodes $(OCI_INSTANCE_POOL_ID) 15 to 40"]))


def test_an_edit_of_a_missing_file_compares_its_own_strings():
    edit = {"file_path": "/nowhere/values.yaml", "old_string": "maxRunners: 2", "new_string": "maxRunners: 3"}
    assert NodeCaps().decide(Call("Edit", edit), ME, None).reason == refusal(["maxRunners 2 to 3"])


def test_a_multi_edit_and_a_write_are_compared_against_what_they_replace(tmp_path):
    target = tmp_path / "values.yaml"
    target.write_text("maxRunners: 30\n")
    gate = NodeCaps()
    write = Call("Write", {"file_path": str(target), "content": "maxRunners: 31\n"})
    multi = Call("MultiEdit", {"file_path": str(target), "edits": [{"old_string": "30", "new_string": "29"}]})
    assert gate.decide(write, ME, None).reason == refusal(["maxRunners 30 to 31"])
    assert gate.decide(multi, ME, None).allowed


def test_unrelated_calls_pass():
    gate = NodeCaps()
    assert not gate.matches(Call("Read", {"file_path": "/r/x"}))
    assert gate.decide(Call("Bash", {"command": "kubectl get pods"}), ME, None).allowed
    assert gate.decide(Call("Edit", {"file_path": "/r/a.py", "old_string": "a", "new_string": "b"}), ME, None).allowed


def test_a_resize_command_is_logged_and_let_through_in_log_only_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(modes, "swarm_gates", lambda swarm, environ: {})
    environ = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_AGENT_NAME": ME.name, "AGENTIHOOKS_SWARM_TASK": "t1"}
    command = "oci compute-management instance-pool update --instance-pool-id x --size 20"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    assert entry.main(["node-caps"], io.StringIO(payload), environ, tmp_path) == 0
    assert [(r["gate"], r["kind"], r["reason"]) for r in rows(tmp_path)] == [
        ("node-caps", "observe", refusal(["a node pool resize command"]))
    ]


def test_an_edit_raise_is_logged_and_let_through_in_log_only_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(modes, "swarm_gates", lambda swarm, environ: {})
    target = tmp_path / "values.yaml"
    target.write_text("maxRunners: 24\n")
    environ = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_AGENT_NAME": ME.name, "AGENTIHOOKS_SWARM_TASK": "t1"}
    edit = {"file_path": str(target), "old_string": "24", "new_string": "40"}
    payload = json.dumps({"tool_name": "Edit", "tool_input": edit})
    assert entry.main(["node-caps"], io.StringIO(payload), environ, tmp_path) == 0
    assert [(r["gate"], r["kind"], r["reason"]) for r in rows(tmp_path)] == [
        ("node-caps", "observe", refusal(["maxRunners 24 to 40"]))
    ]
