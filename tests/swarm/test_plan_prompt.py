from scripts.swarm import cli, prompt


def test_plan_prompt_and_done_parser():
    args = cli.build_parser().parse_args(["demo", "done", "--slice", "build,check"])
    assert args.proof_slice == "build,check"
    text = prompt.build(
        "demo",
        "/repo",
        "plan",
        "planner@abcdef-0001",
        {"id": "plan-one", "title": "Slice phase", "phase": "p1", "kind": "plan"},
    )
    assert "done --slice <ids>" in text
    assert "task add" in text
    assert "--phase p1" in text
    assert "Edit no code" in text
    assert "outside this phase" in text
    assert "draft pull request" not in text
