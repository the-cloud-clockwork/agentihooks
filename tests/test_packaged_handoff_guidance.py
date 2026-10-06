from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PACKAGE = Path(__file__).resolve().parents[1] / "profiles" / "package"


@pytest.mark.parametrize("source", ["rules/agentihooks-toolbelt.md", "skills/init-agent/SKILL.md"])
def test_packaged_handoff_guidance_uses_the_skill_and_a_derived_recap(source):
    text = (PACKAGE / source).read_text()
    assert "Handoff v2" in text
    assert "handoff skill" in text
    assert "recap is derived" in text
    assert "--recap" not in text
    assert "Write the handoff document and a recap" not in text
    assert "these sections: goal" not in text
