"""Repository hygiene guard ensuring no private planning-doc references leak into the repo."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Forbidden markers built dynamically so this test file itself does not trigger them.
# Exclude the HTTP API path `/v1/audit/verify` while blocking any private planning-doc path.
_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("\u00a7", re.compile("\u00a7")),
    ("audit" + "/", re.compile(r"(?<!/v1/)" + "audit" + "/")),
    ("session-" + "docs", re.compile("session-" + "docs")),
)

# Frozen LLM schema docstring: Pydantic exports `Answer.__doc__` directly into Gemini's
# `response_json_schema["description"]` hashed by replay cassettes, so it stays untouched
# until the next deliberate schema change and cassette re-record.
_FROZEN_SCHEMA_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        (
            "src/fleet_copilot/agent/schemas.py",
            '    """Structured answer produced by the `synthesizer` node',
        ),
    }
)


def _iter_repo_text_files() -> list[Path]:
    files: list[Path] = []
    for root_name in ("src", "docs", "tests", "scripts", "evals"):
        root = REPO_ROOT / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if (
                "__pycache__" in rel
                or rel.startswith("evals/cassettes/")
                or rel.startswith("evals/reports/")
            ):
                continue
            if path.suffix in (".py", ".md", ".yaml", ".yml", ".sql"):
                files.append(path)
    readme = REPO_ROOT / "README.md"
    if readme.exists():
        files.append(readme)
    return files


def test_no_private_planning_doc_references_in_repo() -> None:
    """Fail if any repo source, doc, README, or test cites private planning notes."""
    violations: list[str] = []
    for path in _iter_repo_text_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(),
            start=1,
        ):
            if any(
                rel == allow_rel and line.startswith(allow_prefix)
                for allow_rel, allow_prefix in _FROZEN_SCHEMA_ALLOWLIST
            ):
                continue
            for label, regex in _FORBIDDEN_PATTERNS:
                if regex.search(line):
                    violations.append(f"{rel}:{lineno}: contains {label!r} -> {line.strip()[:90]}")
    assert not violations, "Private planning-doc references found:\n" + "\n".join(violations)
