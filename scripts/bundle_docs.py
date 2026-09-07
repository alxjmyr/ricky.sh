"""Generate and verify the release-matched passive docs skill references.

This build helper uses only the standard library and never imports Ricky or
reads live configuration. Run it after editing docs in an editable checkout.
"""

from __future__ import annotations

import argparse
import re
import shutil
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

BUNDLE = Path("src/ricky/builtins/skills/ricky-docs")
WHEEL_BUNDLE = "ricky/builtins/skills/ricky-docs/"


def reference_files(root: Path) -> dict[str, bytes]:
    """Read canonical user docs and examples, preserving their relative links."""
    sources = sorted((root / "docs").rglob("*"))
    files: dict[str, bytes] = {}
    for source in sources:
        if source.is_symlink():
            raise ValueError(f"documentation source must not be a symlink: {source}")
        if not source.is_file() or source.name == "development.md":
            continue
        files[source.relative_to(root).as_posix()] = source.read_bytes()
    if "docs/README.md" not in files:
        raise ValueError("docs/README.md is required")
    for name in ("ricky.toml.example", ".secrets.toml.example"):
        source = root / name
        if source.is_symlink():
            raise ValueError(f"example must not be a symlink: {name}")
        files[name] = source.read_bytes()
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    index = [
        f"# Ricky {version} documentation",
        "",
        "Generated from this release's user documentation. Do not edit this copy.",
        "Read `references/docs/README.md` for the user guide index. Paths below are",
        "relative to the skill bundle; line numbers refer to the complete source file.",
        "",
    ]
    for name, content in sorted(files.items()):
        index.append(f"## references/{name}")
        if name.endswith(".md"):
            fenced = False
            for number, line in enumerate(content.decode("utf-8").splitlines(), start=1):
                if line.startswith("```"):
                    fenced = not fenced
                if not fenced and re.match(r"^#{1,3} ", line):
                    index.append(f"- Line {number}: {line.lstrip('# ')}")
        index.append("")
    files["INDEX.md"] = "\n".join(index).encode("utf-8")
    validate_links(files)
    return files


def validate_links(files: dict[str, bytes]) -> None:
    """Reject missing local files or Markdown heading fragments in the bundle."""
    for name, content in files.items():
        if not name.endswith(".md"):
            continue
        for link in re.findall(r"\[[^\]]*\]\(([^\s)]+)\)", content.decode("utf-8")):
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc:
                continue
            parts: list[str] = []
            target = Path(name).parent / unquote(parsed.path) if parsed.path else Path(name)
            for part in target.parts:
                if part == ".." and parts:
                    parts.pop()
                elif part == ".." or part == "/":
                    raise ValueError(f"{name}: link escapes references: {link}")
                elif part != ".":
                    parts.append(part)
            target_name = "/".join(parts)
            if target_name not in files:
                raise ValueError(f"{name}: missing bundled link: {link}")
            if parsed.fragment and target_name.endswith(".md"):
                headings = re.findall(
                    r"^#{1,6} (.+)$", files[target_name].decode("utf-8"), re.M
                )
                anchors: set[str] = set()
                for heading in headings:
                    base = re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
                    anchor = base
                    suffix = 0
                    while anchor in anchors:
                        suffix += 1
                        anchor = f"{base}-{suffix}"
                    anchors.add(anchor)
                if unquote(parsed.fragment) not in anchors:
                    raise ValueError(f"{name}: missing bundled heading: {link}")


def synchronize(root: Path) -> Path:
    """Replace generated references only, leaving the authored skill untouched."""
    files = reference_files(root)
    destination = root / BUNDLE / "references"
    if destination.is_symlink():
        raise ValueError("generated references must not be a symlink")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    for name, content in files.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return destination


def check_references(root: Path) -> None:
    expected = reference_files(root)
    destination = root / BUNDLE / "references"
    actual = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }
    if actual != expected:
        raise ValueError("stale docs references; run uv run python scripts/bundle_docs.py")


def check_wheel(root: Path, wheel: Path) -> None:
    """Verify actual wheel bytes, including missing, stale, and extra references."""
    expected = reference_files(root)
    prefix = WHEEL_BUNDLE + "references/"
    with ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("wheel contains duplicate entries")
        if archive.read(WHEEL_BUNDLE + "SKILL.md") != (root / BUNDLE / "SKILL.md").read_bytes():
            raise ValueError("wheel docs skill differs from source")
        actual = {
            name.removeprefix(prefix): archive.read(name)
            for name in names
            if name.startswith(prefix) and not name.endswith("/")
        }
    if actual != expected:
        raise ValueError("wheel docs references differ from current source documentation")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true", help="Check editable-install freshness.")
    actions.add_argument("--wheel", type=Path, help="Verify a built release wheel.")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.wheel:
        check_wheel(root, args.wheel)
    elif args.check:
        check_references(root)
    else:
        synchronize(root)


if __name__ == "__main__":
    main()
