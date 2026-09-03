"""Guided selection prompts for the CLI: model picker and subset approval."""

from __future__ import annotations

from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from ricky.config import ModelSelection, RickySettings, user_data_path, write_default_selection
from ricky.interfaces.cli.render import CliRenderer
from ricky.llm.factory import (
    ProviderEntry,
    auth_ready,
    create_provider,
    provider_entries,
)
from ricky.llm.provider import SupportsModelListing
from ricky.llm.types import ModelInfo, ProviderError
from ricky.profiles import ProfileScope

_MAX_DISPLAYED_MODELS = 30


async def select_subset(items: list[str], renderer: CliRenderer, *, title: str) -> list[int]:
    """Present a numbered list and return the approved zero-based indices.

    Accepts numbers and ranges (``1,3-5``), ``all``, or ``none``; empty input
    (and EOF) approves nothing, so the review fails closed.
    """
    if not items:
        renderer.render_status(f"{title} — nothing to review.", style="dim")
        return []
    renderer.render_status(title, style="bold")
    for number, text in enumerate(items, start=1):
        renderer.console.print(Panel(Text(text), title=str(number), border_style="magenta"))
    prompt = (
        "[bold yellow]Approve which?[/bold yellow] "
        f"(e.g. `1,3-5`, `all`, `none`) [1-{len(items)}]: "
    )
    while True:
        try:
            answer = await renderer.read_line(prompt)
        except EOFError:
            return []
        approved = parse_subset_reply(answer, len(items))
        if approved is not None:
            return approved
        renderer.render_status(
            f"Enter numbers 1-{len(items)} and ranges (e.g. 1,3-5), 'all', or 'none'.",
            style="yellow",
        )


def parse_subset_reply(reply: str, count: int) -> list[int] | None:
    """Parse a subset reply into zero-based indices; None means invalid."""
    text = reply.strip().lower()
    if text in {"", "none"}:
        return []
    if text == "all":
        return list(range(count))
    indices: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            return None
        first, separator, last = part.partition("-")
        first, last = first.strip(), last.strip()
        if not separator:
            last = first
        if not (first.isdigit() and last.isdigit()):
            return None
        start, end = int(first), int(last)
        if start < 1 or end > count or start > end:
            return None
        indices.update(range(start - 1, end))
    return sorted(indices)


async def run_model_picker(
    settings: RickySettings,
    renderer: CliRenderer,
    *,
    profile_scope: ProfileScope,
    profile: str | None = None,
) -> ModelSelection:
    """Guide the user through provider/model selection and persist the result."""
    current_selection = settings.resolve_profile_selection(profile_scope)
    runtime_settings = settings.resolve_profile_runtime_settings(profile_scope)
    entries = [
        entry
        for entry in provider_entries()
        if provider_allowed(settings, profile_scope, entry.name)
    ]
    if not entries:
        raise ValueError("the target profile scope allows no registered providers")
    renderer.render_status("Providers:", style="bold")
    for index, entry in enumerate(entries, start=1):
        ready = auth_ready(entry.name, runtime_settings)
        auth = "ready" if ready else f"missing — {entry.auth_hint}"
        current = " (current default)" if entry.name == current_selection.provider else ""
        renderer.render_status(
            f"  {index}. {entry.title}   {entry.name}   auth: {auth}{current}",
            style="",
        )

    entry = _prompt_provider(entries, current_selection.provider, renderer)
    models = await _fetch_models(entry, runtime_settings, renderer)
    if models is None:
        current_model = settings.resolve_profile_selection(
            profile_scope,
            provider=entry.name,
        ).model
        model = _prompt_manual_model(current_model, renderer)
    else:
        model = _prompt_catalog_model(models, renderer)

    selection = settings.resolve_profile_selection(
        profile_scope,
        provider=entry.name,
        model=model,
    )
    path = write_default_selection(
        selection,
        root=user_data_path(settings),
        profile=profile,
    )
    setting = f"profile default for {profile}" if profile is not None else "installation default"
    renderer.render_status(
        f"Saved {setting}: {selection.provider} · {selection.model}",
        style="green",
    )
    renderer.render_status(f"  → {path}", style="dim")
    return selection


def _prompt_provider(
    entries: list[ProviderEntry],
    default_provider: str,
    renderer: CliRenderer,
) -> ProviderEntry:
    names = [entry.name for entry in entries]
    try:
        default_index = names.index(default_provider) + 1
    except ValueError:
        default_index = 1

    while True:
        answer = Prompt.ask(
            f"Select provider [1-{len(entries)}, Enter keeps {entries[default_index - 1].name}]",
            default=str(default_index),
            console=renderer.console,
        ).strip()
        if answer.isdigit():
            index = int(answer)
            if 1 <= index <= len(entries):
                return entries[index - 1]
        elif answer in names:
            return entries[names.index(answer)]
        renderer.render_status("Choose a listed provider number or name.", style="yellow")


async def _fetch_models(
    entry: ProviderEntry,
    settings: RickySettings,
    renderer: CliRenderer,
) -> list[ModelInfo] | None:
    if not auth_ready(entry.name, settings):
        renderer.render_status(
            f"Catalog unavailable: authentication missing — {entry.auth_hint}.",
            style="yellow",
        )
        return None

    renderer.render_status(f"Fetching models from {entry.name}…")
    provider = None
    try:
        provider = create_provider(entry.name, settings)
        if not isinstance(provider, SupportsModelListing):
            renderer.render_status(
                "Catalog unavailable: this provider does not support model listing.",
                style="yellow",
            )
            return None
        return await provider.list_models()
    except ProviderError as exc:
        renderer.render_status(f"Catalog unavailable: {exc}", style="yellow")
        return None
    finally:
        if provider is not None:
            await provider.aclose()


def _prompt_catalog_model(models: list[ModelInfo], renderer: CliRenderer) -> str:
    filter_text = Prompt.ask(
        "Filter (substring, Enter for all)",
        default="",
        show_default=False,
        console=renderer.console,
    ).strip()
    filtered = [
        model
        for model in models
        if not filter_text
        or filter_text.casefold() in model.id.casefold()
        or (model.name is not None and filter_text.casefold() in model.name.casefold())
    ]

    if not filtered:
        renderer.render_status("No catalog matches; type a model id.", style="yellow")
    else:
        for index, model in enumerate(filtered[:_MAX_DISPLAYED_MODELS], start=1):
            label = model.id
            if model.name and model.name != model.id:
                label = f"{label} — {model.name}"
            renderer.render_status(f"  {index}. {label}", style="")
        hidden = len(filtered) - _MAX_DISPLAYED_MODELS
        if hidden > 0:
            renderer.render_status(
                f"  …and {hidden} more — refine the filter or type an id",
                style="dim",
            )

    while True:
        answer = Prompt.ask(
            "Select model [number, or type a model id]",
            default="",
            show_default=False,
            console=renderer.console,
        ).strip()
        if not answer:
            renderer.render_status("Model id cannot be empty.", style="yellow")
            continue
        if not answer.isdigit():
            return answer
        index = int(answer)
        if 1 <= index <= min(len(filtered), _MAX_DISPLAYED_MODELS):
            return filtered[index - 1].id
        renderer.render_status("Choose a displayed number or type a model id.", style="yellow")


def _prompt_manual_model(
    current: str,
    renderer: CliRenderer,
) -> str:
    while True:
        answer = Prompt.ask(
            "Model id",
            default=current,
            console=renderer.console,
        ).strip()
        if answer:
            return answer
        renderer.render_status("Model id cannot be empty.", style="yellow")


def provider_allowed(
    settings: RickySettings,
    profile_scope: ProfileScope,
    provider: str,
) -> bool:
    """Return whether one provider survives the target scope's intersection."""

    try:
        settings.resolve_profile_selection(profile_scope, provider=provider)
    except ValueError:
        return False
    return True
