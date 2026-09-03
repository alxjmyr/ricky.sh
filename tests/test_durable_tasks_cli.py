"""Provider-free durable-task CLI lifecycle tests."""

from __future__ import annotations

import re

from typer.testing import CliRunner

from ricky.interfaces.cli.app import app


def test_task_cli_create_list_complete_activity_and_reopen(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    created = runner.invoke(
        app,
        [
            "task",
            "create",
            "--title",
            "File taxes",
            "--objective",
            "File this year's taxes",
            "--closure-criteria",
            "Filing confirmation exists",
            "--mode",
            "user",
        ],
    )
    assert created.exit_code == 0, created.output
    match = re.search(r"task_[0-9a-f]{32}", created.output)
    assert match is not None
    task_id = match.group(0)

    listed = runner.invoke(app, ["task", "list"])
    completed = runner.invoke(
        app,
        ["task", "complete", task_id, "--summary", "Alex provided filing confirmation"],
    )
    activity = runner.invoke(app, ["task", "activity", task_id])
    reopened = runner.invoke(
        app, ["task", "reopen", task_id, "--reason", "An amended filing is needed"]
    )
    work_list = runner.invoke(app, ["task", "list", "--profile", "work"])

    assert listed.exit_code == 0 and "File taxes" in listed.output
    assert completed.exit_code == 0 and "status: completed" in completed.output
    assert activity.exit_code == 0 and "deterministic_user_command" in activity.output
    assert reopened.exit_code == 0 and "status: open" in reopened.output
    assert work_list.exit_code == 0 and "File taxes" not in work_list.output
    assert "no matching durable tasks" in work_list.output


def test_task_help_lists_mvp_commands() -> None:
    result = CliRunner().invoke(app, ["task", "--help"])
    assert result.exit_code == 0, result.output
    for command in (
        "list",
        "show",
        "activity",
        "artifacts",
        "create",
        "complete",
        "cancel",
        "reopen",
    ):
        assert command in result.output


def test_task_list_excludes_closed_tasks_unless_requested(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    task_ids: list[str] = []
    for title in ("Completed task", "Cancelled task"):
        created = runner.invoke(
            app,
            [
                "task",
                "create",
                "--title",
                title,
                "--objective",
                title,
                "--closure-criteria",
                "Terminal state recorded",
                "--mode",
                "user",
            ],
        )
        assert created.exit_code == 0, created.output
        match = re.search(r"task_[0-9a-f]{32}", created.output)
        assert match is not None
        task_ids.append(match.group(0))

    completed = runner.invoke(
        app,
        ["task", "complete", task_ids[0], "--summary", "Finished"],
    )
    cancelled = runner.invoke(
        app,
        ["task", "cancel", task_ids[1], "--reason", "No longer needed"],
    )
    default_list = runner.invoke(app, ["task", "list"])
    closed_list = runner.invoke(app, ["task", "list", "--include-closed"])

    assert completed.exit_code == 0, completed.output
    assert cancelled.exit_code == 0, cancelled.output
    assert default_list.exit_code == 0, default_list.output
    assert "Completed task" not in default_list.output
    assert "Cancelled task" not in default_list.output
    assert closed_list.exit_code == 0, closed_list.output
    unwrapped = closed_list.output.replace("\n", " ")
    assert "Completed task" in unwrapped
    assert "Cancelled task" in unwrapped
