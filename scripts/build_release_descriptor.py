"""Build Ricky's strict public release descriptor from exact local artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

REPOSITORY = "alxjmyr/ricky.sh"
VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--constraints", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-generation", action="append", type=int, required=True)
    parser.add_argument("--target-generation", type=int, required=True)
    parser.add_argument("--python-requirement", default=">=3.12")
    parser.add_argument("--minimum-uv-version", default="0.6.0")
    args = parser.parse_args()

    if VERSION.fullmatch(args.version) is None:
        parser.error("--version must be MAJOR.MINOR.PATCH")
    if VERSION.fullmatch(args.minimum_uv_version) is None:
        parser.error("--minimum-uv-version must be MAJOR.MINOR.PATCH")
    expected_wheel = f"ricky-{args.version}-py3-none-any.whl"
    expected_constraints = f"ricky-{args.version}-constraints.txt"
    if args.wheel.name != expected_wheel or not args.wheel.is_file():
        parser.error(f"--wheel must identify {expected_wheel}")
    if args.constraints.name != expected_constraints or not args.constraints.is_file():
        parser.error(f"--constraints must identify {expected_constraints}")
    generations = sorted(set(args.source_generation))
    if generations != args.source_generation or any(item < 1 for item in generations):
        parser.error("source generations must be positive, sorted, and unique")
    if args.target_generation < 1:
        parser.error("target generation must be positive")

    base = f"https://github.com/{REPOSITORY}/releases/download/v{args.version}"
    document = {
        "format_version": 1,
        "repository": REPOSITORY,
        "channel": "stable",
        "source": "github_release",
        "software_version": args.version,
        "supported_source_data_generations": generations,
        "target_data_generation": args.target_generation,
        "python_requirement": args.python_requirement,
        "minimum_uv_version": args.minimum_uv_version,
        "wheel": _artifact(args.wheel, base),
        "constraints": _artifact(args.constraints, base),
    }
    expected_output = f"ricky-{args.version}-release.json"
    if args.output.name != expected_output:
        parser.error(f"--output must be named {expected_output}")
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _artifact(path: Path, base: str) -> dict[str, str | int]:
    payload = path.read_bytes()
    return {
        "name": path.name,
        "url": f"{base}/{path.name}",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


if __name__ == "__main__":
    main()
