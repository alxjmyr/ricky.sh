"""Keep operational examples and generated release references usable."""

from __future__ import annotations

import re
import runpy
import shlex
import shutil
import tomllib
from pathlib import Path
from zipfile import ZipFile

import pytest
from typer.core import TyperGroup, TyperOption
from typer.main import get_command

from ricky.interfaces.cli.app import app
from ricky.jobs.spec import JobSpec

ROOT = Path(__file__).resolve().parents[1]
HELPER = runpy.run_path(str(ROOT / "scripts/bundle_docs.py"))


def test_editable_docs_are_current() -> None:
    HELPER["check_references"](ROOT)


def test_user_cli_examples_name_registered_commands_and_options() -> None:
    root_command = get_command(app)
    for path in sorted((ROOT / "docs").glob("*.md")):
        for block in re.findall(r"```(?:bash|text)\n(.*?)```", path.read_text(), re.S):
            for line in block.replace("\\\n", "").splitlines():
                if not line.startswith("ricky "):
                    continue
                # The CLI synopsis uses optional brackets and a COMMAND placeholder.
                # Validate its real flags without treating the notation as a command.
                args = [
                    arg[1:-1] if arg.startswith("[--") and arg.endswith("]") else arg
                    for arg in shlex.split(line)[1:]
                    if arg != "[COMMAND]"
                ]
                if any("<command>" in arg or "<subcommand>" in arg for arg in args):
                    continue
                command = root_command
                while isinstance(command, TyperGroup) and args and args[0] in command.commands:
                    command = command.commands[args.pop(0)]
                known = {
                    option
                    for param in command.params
                    if isinstance(param, TyperOption)
                    for option in [*param.opts, *param.secondary_opts]
                } | {"--help"}
                for arg in args:
                    if arg.startswith("-"):
                        assert arg.split("=")[0] in known, (path.name, line, arg)
                if isinstance(command, TyperGroup) and "--help" not in args:
                    assert command.invoke_without_command, (path.name, line)
                    assert all(arg.startswith("-") for arg in args), (path.name, line)


def test_complete_documented_jobs_validate() -> None:
    source = (ROOT / "docs/jobs-and-schedules.md").read_text()
    jobs = [
        tomllib.loads(block)
        for block in re.findall(r"```toml\n(.*?)```", source, re.S)
        if block.startswith("version =")
    ]
    assert jobs
    for document in jobs:
        JobSpec.model_validate(document)


@pytest.fixture
def docs_source(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/README.md").write_text("# Docs\n\n[Setup](setup.md)\n")
    (tmp_path / "docs/setup.md").write_text("# Setup\n\n## Configure\n\nUse settings.\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
    (tmp_path / "ricky.toml.example").write_text('user_timezone = "UTC"\n')
    (tmp_path / ".secrets.toml.example").write_text("# Placeholder keys only.\n")
    skill = tmp_path / HELPER["BUNDLE"] / "SKILL.md"
    skill.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / HELPER["BUNDLE"] / "SKILL.md", skill)
    return tmp_path


def test_regeneration_updates_edits_additions_deletions_and_version(docs_source: Path) -> None:
    destination = HELPER["synchronize"](docs_source)
    (docs_source / "docs/setup.md").unlink()
    (docs_source / "docs/README.md").write_text("# Docs\n[Operations](operations.md)\n")
    (docs_source / "docs/operations.md").write_text("# Operations\n\nUpdated operations.\n")
    (docs_source / "pyproject.toml").write_text('[project]\nversion = "2.0.0"\n')
    with pytest.raises(ValueError, match="stale docs"):
        HELPER["check_references"](docs_source)
    HELPER["synchronize"](docs_source)
    HELPER["check_references"](docs_source)
    assert not (destination / "docs/setup.md").exists()
    assert (destination / "docs/operations.md").read_bytes() == (
        docs_source / "docs/operations.md"
    ).read_bytes()
    assert "Ricky 2.0.0" in (destination / "INDEX.md").read_text()


def test_generation_rejects_missing_links_and_symlink_sources(docs_source: Path) -> None:
    (docs_source / "docs/README.md").write_text("# Docs\n[Setup](setup.md#missing)\n")
    with pytest.raises(ValueError, match="missing bundled heading"):
        HELPER["synchronize"](docs_source)
    (docs_source / "docs/README.md").write_text("# Docs\n[Setup](setup.md#configure)\n")
    HELPER["synchronize"](docs_source)
    (docs_source / "docs/setup.md").unlink()
    with pytest.raises(ValueError, match="missing bundled link"):
        HELPER["synchronize"](docs_source)
    (docs_source / "docs/setup.md").symlink_to(docs_source / "ricky.toml.example")
    with pytest.raises(ValueError, match="symlink"):
        HELPER["synchronize"](docs_source)


@pytest.mark.parametrize("defect", [None, "missing", "stale", "extra"])
def test_wheel_verification_checks_packaged_bytes(docs_source: Path, defect: str | None) -> None:
    expected = HELPER["reference_files"](docs_source)
    if defect == "missing":
        del expected["docs/setup.md"]
    elif defect == "stale":
        expected["docs/setup.md"] = b"Old configuration instructions."
    elif defect == "extra":
        expected["docs/retired.md"] = b"Removed instructions."
    wheel = docs_source / "test.whl"
    with ZipFile(wheel, "w") as archive:
        prefix = HELPER["WHEEL_BUNDLE"]
        archive.write(docs_source / HELPER["BUNDLE"] / "SKILL.md", prefix + "SKILL.md")
        for name, content in expected.items():
            archive.writestr(prefix + "references/" + name, content)
    if defect:
        with pytest.raises(ValueError, match="differ"):
            HELPER["check_wheel"](docs_source, wheel)
    else:
        HELPER["check_wheel"](docs_source, wheel)
