"""Versioned prompt loader for `agent/prompts/{planner,investigator,synthesizer}.md`."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=16)
def load_prompt(name: str) -> tuple[str, str]:
    """Load `agent/prompts/<name>.md` and return `(body_text, version_string)`."""
    path = _PROMPTS_DIR / f"{name}.md"
    raw = path.read_text(encoding="utf-8")
    version = "1"
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            header, body = parts[1], parts[2].lstrip("\n")
            for line in header.splitlines():
                if line.strip().startswith("version:"):
                    version = line.split(":", 1)[1].strip()
    return body, version


def prompt_bundle_version() -> str:
    """Return a compact version+hash identifier over all three prompt templates."""
    pieces: list[str] = []
    versions: list[str] = []
    for name in ("planner", "investigator", "synthesizer"):
        body, ver = load_prompt(name)
        versions.append(f"{name[0]}v{ver}")
        pieces.append(f"{name}:{ver}:{body}")
    digest = hashlib.sha256("\n".join(pieces).encode("utf-8")).hexdigest()[:8]
    return f"{'-'.join(versions)}-{digest}"
