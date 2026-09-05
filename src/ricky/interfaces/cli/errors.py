"""Shared error rendering for provider-backed CLI commands."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import typer

from ricky.interfaces.cli.render import CliRenderer
from ricky.llm import ProviderError
from ricky.tools.integrations.gcal import GcalError
from ricky.tools.integrations.gmail import GmailError
from ricky.tools.integrations.google import GoogleAuthError


def run_with_provider_errors[T](
    factory: Callable[[CliRenderer], Coroutine[Any, Any, T]],
) -> T:
    renderer = CliRenderer()
    try:
        return asyncio.run(factory(renderer))
    except ProviderError as exc:
        renderer.render_error(f"Provider error: {exc}")
        raise typer.Exit(1) from exc
    except (GoogleAuthError, GmailError, GcalError) as exc:
        renderer.render_error(str(exc))
        raise typer.Exit(1) from exc
    except ValueError as exc:
        renderer.render_error(str(exc))
        raise typer.Exit(2) from exc
