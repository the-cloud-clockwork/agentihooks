import os


def test_plant_fails_only_in_the_merge_queue():
    assert os.environ.get("GITHUB_EVENT_NAME") != "merge_group"
