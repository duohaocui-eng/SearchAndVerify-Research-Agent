"""Fail when a relative Markdown link points to a missing local file."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")


def main() -> None:
    checked = 0
    for document in ROOT.rglob("*.md"):
        if any(part.startswith(".") for part in document.relative_to(ROOT).parts):
            continue
        text = document.read_text(encoding="utf-8")
        for raw in LINK.findall(text):
            destination = raw.split("#", 1)[0].strip()
            if not destination or destination.startswith(("http://", "https://", "mailto:")):
                continue
            target = (document.parent / unquote(destination)).resolve()
            if ROOT not in target.parents and target != ROOT:
                raise SystemExit(f"Link escapes repository: {document}: {raw}")
            if not target.exists():
                raise SystemExit(f"Broken local link: {document}: {raw}")
            checked += 1
    print(f"Verified {checked} local Markdown links.")


if __name__ == "__main__":
    main()
