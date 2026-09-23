#!yeet
"""Print a release's notes from CHANGELOG.md, for `task release` to post on GitHub."""

import sys
from pathlib import Path


def _changelog_section(changelog: str, version: str) -> str:
    """The body under `## [version]`, up to the next `## ` heading or the link definitions.

    >>> text = "# Changelog\\n\\n## [Unreleased]\\n\\n## [1.2.0] - 2026-01-01\\n\\n- Added x.\\n"
    >>> _changelog_section(text + "\\n## [1.1.0]\\n\\n- Older.\\n", "1.2.0")
    '- Added x.'
    >>> _changelog_section(text, "1.3.0")
    ''
    """
    lines: list[str] = []
    inside = False
    for line in changelog.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line.startswith(f"## [{version}]")
            continue
        if inside and line.startswith("[") and "]: " in line:
            break
        if inside:
            lines.append(line)
    return "\n".join(lines).strip()


def main(version: str, previous: str) -> None:
    """Print the notes for `version`, ending with a compare link from `previous`.

    Exits non-zero if CHANGELOG.md has no non-empty `## [version]` section, so a release can't
    go out with empty notes.
    """
    changelog = (Path(__file__).parent.parent / "CHANGELOG.md").read_text()
    section = _changelog_section(changelog, version)
    if not section:
        sys.exit(
            f"CHANGELOG.md has no notes under '## [{version}]'. Rename '## [Unreleased]' to "
            f"'## [{version}] - <date>', add a fresh empty '## [Unreleased]' above it, and commit."
        )
    compare = f"https://github.com/RogerThomas/lothc/compare/{previous}...{version}"
    print(f"{section}\n\n**Full Changelog**: {compare}")
