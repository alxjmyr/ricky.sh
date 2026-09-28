"""Small profile and bundled catalogs for chat listing tests."""

from pathlib import Path

from ricky.config import RickySettings, profile_data_path


def write_catalogs(settings: RickySettings, bundled_root: Path) -> None:
    for owner, name in (
        ("bundled", "shipped"),
        ("bundled", "override"),
        ("personal", "override"),
        ("personal", "review"),
        ("shared", "review"),
        ("shared", "common"),
        ("work", "private"),
    ):
        root = bundled_root if owner == "bundled" else profile_data_path(settings, owner)
        skill = root / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(
            f"---\nname: {name}\ndescription: Catalog description\n---\nPrivate instructions\n"
        )
        workflow = root / "workflows" / name / "workflow.toml"
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text(
            f'version = 2\nname = "{name}"\ndescription = "Catalog description"\n'
            '[[steps]]\nid = "done"\nkind = "message"\nmessage = "Done"\n'
        )
