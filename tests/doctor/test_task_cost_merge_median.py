from scripts.doctor import traces


def _record(costs):
    return {
        "merged": [task for task, _, _ in costs],
        "traces": [
            {
                "tags": [f"task:{task}"],
                "observations": [
                    {"type": "GENERATION", "turn": None, "tokens": tokens},
                    {"type": "SPAN", "turn": 1, "start": end - 10, "end": end},
                ],
            }
            for task, tokens, end in costs
        ],
    }


MERGED_FIRST = [("a", 400, 50), ("b", 500, 60), ("old", 800, 100)]
CHEAPER_LATER = [("c", 100, 200), ("d", 100, 210), ("e", 100, 220)]


def test_a_task_under_twice_the_median_at_its_merge_is_not_raised_by_cheaper_later_tasks():
    assert traces.task_cost(_record(MERGED_FIRST), traces.Limits()) == []
    assert traces.task_cost(_record(MERGED_FIRST + CHEAPER_LATER), traces.Limits()) == []


def test_a_task_over_twice_the_median_at_its_merge_is_still_raised():
    [found] = traces.task_cost(_record(MERGED_FIRST + CHEAPER_LATER + [("f", 2000, 300)]), traces.Limits())
    assert found.id == "task-cost/f"
    assert "uncached tokens 2000, median of the other merged tasks 250" in found.evidence


def _task(task, tokens, *spans):
    return {
        "tags": [f"task:{task}"],
        "observations": [
            {"type": "GENERATION", "turn": None, "tokens": tokens},
            *({"type": "SPAN", "turn": n, "start": start, "end": end} for n, (start, end) in enumerate(spans, 1)),
        ],
    }


def test_a_task_finishes_at_the_latest_end_of_its_turns():
    record = {
        "merged": ["a", "b", "heavy"],
        "traces": [
            _task("a", 400, (40, 50)),
            _task("b", 500, (50, 60)),
            _task("heavy", 5000, (10, 100), (2, 5)),
        ],
    }
    assert [f.subject for f in traces.task_cost(record, traces.Limits())] == ["heavy"]


def test_a_task_finished_at_the_same_time_counts_in_the_median():
    record = {"merged": ["a", "heavy"], "traces": [_task("a", 400, (40, 50)), _task("heavy", 5000, (45, 50))]}
    assert [f.subject for f in traces.task_cost(record, traces.Limits())] == ["heavy"]


def test_a_turn_without_an_end_counts_as_finished_before_every_ended_turn():
    unended = {
        "tags": ["task:heavy"],
        "observations": [
            {"type": "GENERATION", "turn": None, "tokens": 5000},
            {"type": "SPAN", "turn": 1, "start": 0},
        ],
    }
    record = {"merged": ["a", "heavy"], "traces": [_task("a", 400, (0, 1)), unended]}
    assert traces.task_cost(record, traces.Limits()) == []
