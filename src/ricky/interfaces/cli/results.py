"""Shared human and machine-readable CLI result rendering."""

from __future__ import annotations

import json
from typing import Any, Never

import typer
from pydantic import BaseModel

from ricky.interfaces.cli.render import CliRenderer


def error_message(exc: BaseException) -> str:
    """Render the cause with any context a handler attached while unwinding."""

    notes: object = getattr(exc, "__notes__", ())
    if not isinstance(notes, list):
        return str(exc)
    return "\n".join((str(exc), *(str(note) for note in notes)))


def emit_result(
    result: BaseModel | dict[str, Any],
    renderer: CliRenderer,
    *,
    as_json: bool,
    human: str,
) -> None:
    """Write exactly one machine-readable result or its human rendering."""

    if as_json:
        payload = result.model_dump(mode="json") if isinstance(result, BaseModel) else result
        typer.echo(json.dumps(payload, sort_keys=True))
        return
    renderer.render_status(human, style="green")


def fail(exc: BaseException, renderer: CliRenderer, *, as_json: bool, prefix: str) -> Never:
    """Report one bounded command failure and exit with a nonzero status."""

    message = error_message(exc)
    if as_json:
        typer.echo(json.dumps({"error": message}, sort_keys=True))
    else:
        renderer.render_error(f"{prefix}: {message}")
    raise typer.Exit(1) from exc
