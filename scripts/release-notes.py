#!/usr/bin/env python3
"""Render release notes without shell or Markdown command interpretation."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


def markdown_code(value: str) -> str:
    """Return a Markdown code span whose delimiter cannot occur in its value."""
    value = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", r"\n")
    longest = max((len(run) for run in re.findall(r"`+", value)), default=0)
    delimiter = "`" * (longest + 1)
    padding = " " if value.startswith(("`", " ")) or value.endswith(("`", " ")) else ""
    return f"{delimiter}{padding}{value}{padding}{delimiter}"


def render(profile: str, commit: str, workflow_commit: str,
           feed_lock_sha256: str, run_url: str, built_at: str | None = None) -> str:
    lines = [
        f"Automated build for {markdown_code(profile)}.",
        "",
        f"- Commit: {markdown_code(commit)}",
        f"- Workflow commit: {markdown_code(workflow_commit)}",
        f"- Feed lock SHA-256: {markdown_code(feed_lock_sha256)}",
        f"- Run: {run_url}",
    ]
    if built_at is not None:
        lines.append(f"- Built at (UTC): {markdown_code(built_at)}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--workflow-commit", required=True)
    parser.add_argument("--feed-lock-sha256", required=True)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--built-at")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        render(args.profile, args.commit, args.workflow_commit,
               args.feed_lock_sha256, args.run_url, args.built_at),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
