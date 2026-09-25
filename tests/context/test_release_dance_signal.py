"""'release dance' is the operator's PR and merge signal for main."""

from hooks.context import ci_manifesto as cm


def test_release_dance_arms_pr_and_merge_signals():
    prompt = "disable controls - do the release dance to pypi"

    assert cm._signal_match(prompt, cm._DEFAULT_PR_SIGNALS)
    assert cm._signal_match(prompt, cm._DEFAULT_RELEASE_SIGNALS)
    assert not cm._signal_match("never release dance on friday", cm._DEFAULT_PR_SIGNALS)
