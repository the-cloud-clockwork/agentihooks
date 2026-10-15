import pytest

from hooks.classifier.result import Answer, DecisionResult
from hooks.context import documentation


@pytest.mark.parametrize(
    ("needed", "covered", "size", "expected"),
    [(0.95, 0.05, "large", True), (0.95, 0.95, "small", False), (0.05, 0.05, "none", False)],
)
def test_classification_distinguishes_missing_docs_from_existing_coverage(needed, covered, size, expected):
    evidence = {
        "changes": "+ class WidgetPolicy: pass",
        "docs": [{"path": "handbook/widget.md", "line": 4, "text": "The previous WidgetPolicy setting"}],
    }
    calls = []

    def answer(state, questions, **options):
        calls.append((state, questions, options))
        return DecisionResult(
            {
                "needed": Answer("yesno", noul=needed),
                "covered": Answer("yesno", noul=covered),
                "size": Answer("choice", choice=size, confidence=0.9),
            },
            source="fixture",
        )

    result = documentation.classify(evidence, decider=answer)
    assert result["needs_documentation"] is expected
    assert result["size"] == size
    assert result["source"] == "fixture"
    assert calls[0][0] == evidence
    assert calls[0][2]["purpose"] == "documentation-needed"
    assert set(calls[0][1]) == {"needed", "covered", "size"}


def test_uncertain_documentation_decision_is_retained_for_retry():
    def answer(*args, **kwargs):
        return DecisionResult(
            {
                "needed": Answer("yesno", noul=0.5),
                "covered": Answer("yesno", noul=0.1),
                "size": Answer("choice", choice="small", confidence=0.4),
            },
            source="fixture",
        )

    result = documentation.classify({"changes": "A new setting"}, decider=answer)
    assert result["needs_documentation"] is None
