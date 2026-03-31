"""Tests for scripts.claude_linter."""

import pytest

pytestmark = pytest.mark.unit


class TestClaudeLinter:
    """Tests for CLAUDE.md linting and skill extraction."""

    def test_estimate_tokens(self):
        from scripts.claude_linter import estimate_tokens

        assert estimate_tokens("a" * 400) == 100
        assert estimate_tokens("") == 0

    def test_parse_sections(self, tmp_path):
        from scripts.claude_linter import parse_sections

        md = tmp_path / "CLAUDE.md"
        md.write_text("# Title\n\n## Commands\nRun this.\n\n## Architecture\nDesign.\n")

        sections = parse_sections(md)
        assert len(sections) == 2
        assert sections[0].heading == "Commands"
        assert sections[1].heading == "Architecture"

    def test_classify_always_section(self):
        from scripts.claude_linter import Section, classify_section

        s = Section(
            heading="Architecture", level=2, content="## Architecture\nDesign patterns.", start_line=1, end_line=3
        )
        assert classify_section(s) == "always"

    def test_classify_workflow_section(self):
        from scripts.claude_linter import Section, classify_section

        # Must be >400 chars (>100 tokens) with multiple workflow signals
        content = (
            "## Deploy\n"
            "When the user asks to deploy, use this workflow.\n"
            "If the user wants to rollback, trigger the /rollback command.\n"
            "Step 1: Build the project artifacts.\n"
            "Step 2: Push to the remote server and verify health checks pass.\n"
            "This is additional context to make the section long enough. " * 5
        )
        s = Section(heading="Deploy", level=2, content=content, start_line=1, end_line=10)
        assert classify_section(s) == "workflow"

    def test_short_section_is_always(self):
        from scripts.claude_linter import Section, classify_section

        s = Section(heading="Short", level=2, content="## Short\nSmall.", start_line=1, end_line=2)
        assert classify_section(s) == "always"

    def test_lint_report(self, tmp_path):
        from scripts.claude_linter import lint_report

        padding = "This is a long section with lots of text to pad the content. " * 8
        md = tmp_path / "CLAUDE.md"
        md.write_text(
            "# Project\n\n"
            "## Commands\nRun tests.\n\n"
            "## Deploy Workflow\n"
            "When the user asks to deploy, use this workflow.\n"
            "Step 1: Do this. Step 2: Do that.\n"
            "If the user wants rollback, use the rollback command.\n"
            f"{padding}\n"
        )

        report = lint_report(md)
        assert report.total_tokens > 0
        assert len(report.sections) == 2

    def test_format_report(self, tmp_path):
        from scripts.claude_linter import format_report, lint_report

        md = tmp_path / "CLAUDE.md"
        md.write_text("# Project\n\n## Commands\nRun tests.\n\n## Architecture\nDesign.\n")

        report = lint_report(md)
        output = format_report(report)
        assert "CLAUDE.md Lint Report" in output
        assert "Commands" in output

    def test_extract_to_skill(self, tmp_path):
        from scripts.claude_linter import extract_to_skill

        md = tmp_path / "CLAUDE.md"
        md.write_text("# Project\n\n## Commands\nRun tests.\n\n## Architecture\nDesign.\n")
        output_dir = tmp_path / "skills"

        result = extract_to_skill(md, "Commands", "cmds", output_dir)
        assert result.exists()
        assert "Run tests" in result.read_text()

        # Section should be removed from CLAUDE.md
        remaining = md.read_text()
        assert "## Commands" not in remaining
        assert "## Architecture" in remaining

    def test_extract_nonexistent_raises(self, tmp_path):
        from scripts.claude_linter import extract_to_skill

        md = tmp_path / "CLAUDE.md"
        md.write_text("# Project\n\n## Commands\nRun tests.\n")

        with pytest.raises(ValueError, match="not found"):
            extract_to_skill(md, "Nonexistent", "test", tmp_path / "out")
