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


def test_planner_instructions_keep_the_output_contract():
    task = {"id": "plan-one", "title": "Slice phase", "phase": "p1", "kind": "plan"}
    text = prompt.build("demo", "/repo", "plan", "planner@abcdef-0001", task)
    for paragraph in (
        "Work it end to end, then stop:",
        "1. Read steering.md: project and mission intent, dependency phase evidence and any review note.",
        "2. Slice only phase p1. Edit no code. Write the slice as a markdown plan: the plan you leave plan mode with, or plan.md in your work folder.",
        "3. Publish the plan before adding tasks: agentihooks ledger --slug demo --as planner@abcdef-0001 publish-plan <plan file> --phase p1. It opens a GitHub issue where the repo has issues, else a ledger artifact, links and comments the phase, and every task you add in this phase carries the link.",
        '4. Add tasks with agentihooks ledger --slug demo --as planner@abcdef-0001 task add <id> <title> --phase p1 --lane <eng or ci> --kind <kind> --description "<scope and Done when sentence>" --depends-on <ids> --territory <areas>; include --must, --check and --judge for work beyond code.',
        "5. Keep each code or ci task to one pull request, at most six territory areas and twelve tasks in the slice.",
        '6. Propose work outside this phase as a follow up: agentihooks ledger --slug demo --as planner@abcdef-0001 followup add "<plain words>".',
        "7. Leave the crew with agentihooks ledger --slug demo --as planner@abcdef-0001 leave, then close with agentihooks swarm demo done --slice <ids>, the comma separated ids of the tasks you added in this phase. The ledger refuses invalid slice ids and slice tasks without the plan link. The swarm then closes this session; stop working.",
        'If you cannot finish: agentihooks swarm demo block "<plain words naming the blocker>" and stop.',
    ):
        assert paragraph in text.splitlines()
    normal = prompt.build("demo", "/repo", "eng", "engineer@abcdef-0001", {"id": "build", "title": "Build"})
    assert (
        'If you cannot finish (missing secret, a decision only the operator can make, another task first): push your branch, open a draft pull request, then agentihooks swarm demo block "<plain words naming the blocker>" and stop.'
        in normal
    )


def test_master_publishes_its_accepted_plan_before_adding_tasks():
    text = prompt.build_master("demo", "/repo", "demo-master-1", {})
    assert (
        "- When the operator accepts a plan of yours, add its phases, then publish it before adding its tasks: "
        "agentihooks ledger --slug demo --as demo-master-1 publish-plan <plan file> --phase <phase ids>. It opens a "
        "GitHub issue where the repo has issues, else a ledger artifact, links and comments each phase, and every "
        "task added to those phases carries the link." in text.splitlines()
    )
