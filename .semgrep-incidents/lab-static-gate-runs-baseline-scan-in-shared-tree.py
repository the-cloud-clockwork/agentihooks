import contextlib
from typing import Any


def runs_every_gate_in_the_shared_tree(pod: Any, lease: Any, run_static: Any, gate: Any) -> Any:
    with pod.trees.shared(lease.head, lease.base, lease.grader) as ((head, base, grader), _):
        found = gate(grader, lease.unit.members[0])
        # ruleid: lab-static-gate-runs-baseline-scan-in-shared-tree
        return run_static.run(found, head, base, grader, pod.options)


def gives_a_gate_that_rewrites_its_tree_a_private_one(pod: Any, lease: Any, run_static: Any, gate: Any) -> Any:
    with (
        pod.trees.shared(lease.head, lease.base, lease.grader) as ((head, base, grader), _),
        contextlib.ExitStack() as private,
    ):
        found = gate(grader, lease.unit.members[0])
        tree = private.enter_context(pod.trees.owned(lease.head, lease.base))[0] if found.rewrites_tree else head
        # ok: lab-static-gate-runs-baseline-scan-in-shared-tree
        return run_static.run(found, tree, base, grader, pod.options)
