"""CLAUDE.md linter — analyzes token cost and suggests skill extraction.

Parses CLAUDE.md into sections, estimates token cost per section,
classifies sections as "always needed" vs "workflow-specific",
and can extract workflow sections into standalone skill files.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class Section:
    """A markdown section (H2 level)."""
    heading: str
    level: int  # 2 for ##, 3 for ###
    content: str
    start_line: int
    end_line: int

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.content)


@dataclass
class Report:
    """Lint report for a CLAUDE.md file."""
    path: str
    total_chars: int
    total_tokens: int
    sections: list[Section] = field(default_factory=list)
    extraction_candidates: list[Section] = field(default_factory=list)


# Patterns that indicate workflow-specific content (extractable to skills)
_WORKFLOW_PATTERNS = [
    r"when\s+(?:the\s+)?user",
    r"if\s+(?:the\s+)?user\s+(?:asks|says|wants|requests)",
    r"use\s+this\s+(?:when|if|for)",
    r"trigger\s+when",
    r"invoke\s+(?:this|when)",
    r"/\w+",  # slash command references
    r"step\s+\d+:",
    r"workflow",
    r"recipe",
    r"playbook",
]
_WORKFLOW_RE = re.compile("|".join(_WORKFLOW_PATTERNS), re.IGNORECASE)

# Patterns that indicate always-needed content
_ALWAYS_PATTERNS = [
    r"^#\s+(?:commands?|architecture|environment|setup|install)",
    r"guidelines",
    r"security",
    r"conventions?",
    r"style\s+guide",
    r"key\s+patterns?",
]
_ALWAYS_RE = re.compile("|".join(_ALWAYS_PATTERNS), re.IGNORECASE)


def estimate_tokens(text: str) -> int:
    """Estimate token count from text (chars/4 heuristic)."""
    return len(text) // 4


def parse_sections(md_path: Path) -> list[Section]:
    """Split markdown into sections by H2/H3 headers."""
    text = md_path.read_text(encoding="utf-8")
    lines = text.split("\n")

    sections: list[Section] = []
    current_heading = ""
    current_level = 0
    current_start = 0
    current_lines: list[str] = []

    for i, line in enumerate(lines):
        m = re.match(r"^(#{2,3})\s+(.+)", line)
        if m:
            # Save previous section
            if current_heading:
                sections.append(Section(
                    heading=current_heading,
                    level=current_level,
                    content="\n".join(current_lines),
                    start_line=current_start + 1,
                    end_line=i,
                ))
            current_heading = m.group(2).strip()
            current_level = len(m.group(1))
            current_start = i
            current_lines = [line]
        else:
            current_lines.append(line)

    # Save last section
    if current_heading:
        sections.append(Section(
            heading=current_heading,
            level=current_level,
            content="\n".join(current_lines),
            start_line=current_start + 1,
            end_line=len(lines),
        ))

    return sections


def classify_section(section: Section) -> str:
    """Classify a section as 'always' or 'workflow'.

    Returns:
        'always' for sections that should stay in CLAUDE.md
        'workflow' for sections that are extraction candidates
    """
    text = section.content

    # Check always-needed patterns first (higher priority)
    if _ALWAYS_RE.search(section.heading):
        return "always"

    # Count workflow signals
    workflow_hits = len(_WORKFLOW_RE.findall(text))

    # Short sections are not worth extracting
    if section.tokens < 100:
        return "always"

    # Strong workflow signal: multiple pattern matches
    if workflow_hits >= 2:
        return "workflow"

    # Moderate signal with size threshold
    if workflow_hits >= 1 and section.tokens >= 300:
        return "workflow"

    return "always"


def lint_report(md_path: Path) -> Report:
    """Analyze a CLAUDE.md file and produce a report."""
    text = md_path.read_text(encoding="utf-8")
    sections = parse_sections(md_path)

    candidates = [s for s in sections if classify_section(s) == "workflow"]

    return Report(
        path=str(md_path),
        total_chars=len(text),
        total_tokens=estimate_tokens(text),
        sections=sections,
        extraction_candidates=candidates,
    )


def format_report(report: Report) -> str:
    """Format a lint report for terminal display."""
    lines = [
        f"CLAUDE.md Lint Report: {report.path}",
        f"Total: {report.total_chars:,} chars ≈ {report.total_tokens:,} tokens",
        "",
        f"{'Section':<40} {'Tokens':>8} {'Type':>10}",
        f"{'─' * 40} {'─' * 8} {'─' * 10}",
    ]

    for s in report.sections:
        classification = classify_section(s)
        marker = "*" if classification == "workflow" else " "
        truncated = s.heading[:38] + ".." if len(s.heading) > 40 else s.heading
        lines.append(f"{marker}{truncated:<39} {s.tokens:>8} {classification:>10}")

    if report.extraction_candidates:
        savings = sum(s.tokens for s in report.extraction_candidates)
        lines.extend([
            "",
            f"Extraction candidates ({len(report.extraction_candidates)} sections, ~{savings:,} tokens):",
        ])
        for s in report.extraction_candidates:
            lines.append(f"  * \"{s.heading}\" ({s.tokens:,} tokens, lines {s.start_line}-{s.end_line})")
        lines.append("")
        lines.append("Extract with: agentihooks extract-skill \"<Section Heading>\" --name <skill-name>")
    else:
        lines.extend(["", "No extraction candidates found — CLAUDE.md looks lean."])

    return "\n".join(lines)


def extract_to_skill(
    md_path: Path,
    section_heading: str,
    skill_name: str,
    output_dir: Optional[Path] = None,
) -> Path:
    """Extract a section from CLAUDE.md into a skill directory.

    Args:
        md_path: Path to CLAUDE.md
        section_heading: Exact heading text to extract
        skill_name: Name for the skill directory
        output_dir: Where to create the skill dir (default: md_path's parent/.claude/commands/)

    Returns:
        Path to the created SKILL.md file.

    Raises:
        ValueError: If section_heading not found.
    """
    sections = parse_sections(md_path)

    target = None
    for s in sections:
        if s.heading.strip().lower() == section_heading.strip().lower():
            target = s
            break

    if target is None:
        available = [s.heading for s in sections]
        raise ValueError(
            f"Section \"{section_heading}\" not found. "
            f"Available sections: {', '.join(available)}"
        )

    # Create skill directory
    if output_dir is None:
        output_dir = md_path.parent / ".claude" / "commands"
    skill_dir = output_dir / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Write SKILL.md
    skill_path = skill_dir / "SKILL.md"
    skill_content = target.content.strip()

    # Remove the heading line from content (it becomes the skill name)
    skill_lines = skill_content.split("\n")
    if skill_lines and skill_lines[0].startswith("#"):
        skill_lines = skill_lines[1:]

    skill_path.write_text("\n".join(skill_lines).strip() + "\n", encoding="utf-8")

    # Remove section from CLAUDE.md
    text = md_path.read_text(encoding="utf-8")
    lines = text.split("\n")

    # Remove lines from start_line-1 to end_line-1 (0-indexed)
    start_idx = target.start_line - 1
    end_idx = target.end_line
    # Also remove trailing blank lines
    while end_idx < len(lines) and lines[end_idx].strip() == "":
        end_idx += 1

    new_lines = lines[:start_idx] + lines[end_idx:]
    md_path.write_text("\n".join(new_lines), encoding="utf-8")

    return skill_path
