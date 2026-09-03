"""Name-to-ID resolution over the client's complete-or-marked directories.

Read operations allow a unique substring match when the fetched directory is
complete. Side-effecting callers opt into exact-only matching so a permission
gate never approves one destination and silently sends to another.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable

from ricky.tools.integrations.slack.client import SlackClient, SlackError
from ricky.tools.integrations.slack.types import ChannelKind, SlackChannel, SlackUser

CHANNEL_ID = re.compile(r"[CGD][A-Z0-9]+")
USER_ID = re.compile(r"[UW][A-Z0-9]+")
# Kinds a ``#name`` reference can match; DMs carry no name of their own.
NAMED_KINDS: tuple[ChannelKind, ...] = ("public", "private", "mpim")


class ResolutionError(SlackError):
    """A reference did not resolve to exactly one channel or user."""


def match_users(users: Iterable[SlackUser], query: str) -> list[SlackUser]:
    """Return directory users matching a name substring or exact email."""
    normalized = query.removeprefix("@").strip().lower()
    if not normalized:
        return []
    if "@" in normalized:
        return [user for user in users if user.email.lower() == normalized]
    return [
        user
        for user in users
        if normalized in user.name.lower()
        or normalized in user.display_name.lower()
        or normalized in user.real_name.lower()
    ]


async def resolve_user(client: SlackClient, ref: str, *, exact_only: bool = False) -> SlackUser:
    """Resolve a user reference to exactly one active workspace member."""
    ref = ref.strip()
    directory, truncated = await client.users()
    id_shaped = USER_ID.fullmatch(ref) is not None
    known = directory.get(ref) if id_shaped else None
    if known is not None:
        if known.deleted:
            raise ResolutionError(f"user {ref} is deactivated")
        return known

    users = [user for user in directory.values() if not user.is_bot and not user.deleted]
    query = ref.removeprefix("@").strip()
    if not query:
        raise ResolutionError("empty user reference")

    matches = match_users(users, query)
    exact = _exact_users(matches, query)
    if exact:
        return _exactly_one(exact, ref, kind="user", describe=lambda u: f"{u.id} ({u.label})")
    if id_shaped and not matches:
        return SlackUser(id=ref)
    if exact_only and matches:
        raise _substring_error(
            ref,
            matches,
            kind="user",
            describe=lambda u: f"@{u.label} ({u.id})",
            truncated=truncated,
            directory_size=len(directory),
        )
    if truncated:
        raise _truncated_error("user", ref, len(directory), matches)
    return _exactly_one(matches, ref, kind="user", describe=lambda u: f"{u.id} ({u.label})")


async def resolve_channel(
    client: SlackClient, ref: str, *, exact_only: bool = False
) -> SlackChannel:
    """Resolve a channel reference (or ``@user`` DM) to one conversation."""
    ref = ref.strip()
    id_shaped = CHANNEL_ID.fullmatch(ref) is not None
    # Fetch only the kinds this reference could match. Each kind costs its own
    # paginated pass, so a bare ``@user`` must not warm the public directory.
    if id_shaped:
        wanted = None  # an id can name any kind
    elif ref.startswith("@"):
        wanted = ("im",)
    else:
        wanted = NAMED_KINDS
    channels, truncated = await client.channels(wanted)
    if id_shaped:
        known = next((channel for channel in channels if channel.id == ref), None)
        if known is not None:
            return known

    if ref.startswith("@"):
        user = await resolve_user(client, ref, exact_only=exact_only)
        dms = [c for c in channels if c.kind == "im" and c.user_id == user.id]
        if not dms:
            # Only the DM directory can hide a DM; a crowded public directory is
            # irrelevant here and must not be reported as the cause.
            dm_truncated, dm_size = _kind_scope(channels, truncated, ("im",))
            if dm_truncated:
                raise ResolutionError(
                    f"DM directory truncated at {dm_size} entries; "
                    f"a DM with {user.label} ({user.id}) may exist beyond the cap. "
                    "Retry with a conversation id."
                )
            raise ResolutionError(
                f"no existing DM with {user.label} ({user.id}); sending a message will open one"
            )
        return dms[0]

    name = ref.removeprefix("#").strip().lower()
    if not name:
        raise ResolutionError("empty channel reference")
    named = [channel for channel in channels if channel.name]
    exact = [channel for channel in named if channel.name.lower() == name]
    if exact:
        return _exactly_one(exact, ref, kind="channel", describe=lambda c: f"{c.id} (#{c.name})")
    matches = [channel for channel in named if name in channel.name.lower()]
    if id_shaped and not matches:
        return SlackChannel(id=ref)
    # Named conversations only: a truncated DM directory cannot hide a #name.
    name_truncated, name_size = _kind_scope(channels, truncated, NAMED_KINDS)
    if exact_only and matches:
        raise _substring_error(
            ref,
            matches,
            kind="channel",
            describe=lambda c: f"#{c.name} ({c.id})",
            truncated=name_truncated,
            directory_size=name_size,
        )
    if name_truncated:
        raise _truncated_error("channel", ref, name_size, matches)
    return _exactly_one(matches, ref, kind="channel", describe=lambda c: f"{c.id} (#{c.name})")


def _kind_scope(
    channels: list[SlackChannel],
    truncated: dict[ChannelKind, bool],
    kinds: tuple[ChannelKind, ...],
) -> tuple[bool, int]:
    """Truncation flag and fetched size for the kinds a reference could match."""
    hit_cap = any(truncated.get(kind, False) for kind in kinds)
    return hit_cap, sum(1 for channel in channels if channel.kind in kinds)


def _exact_users(matches: list[SlackUser], query: str) -> list[SlackUser]:
    lowered = query.lower()
    if "@" in lowered:
        return [user for user in matches if user.email.lower() == lowered]
    return [
        user
        for user in matches
        if lowered in {user.name.lower(), user.display_name.lower(), user.real_name.lower()}
    ]


def _substring_error[T](
    ref: str,
    matches: list[T],
    *,
    kind: str,
    describe: Callable[[T], str],
    truncated: bool,
    directory_size: int,
) -> ResolutionError:
    if len(matches) == 1:
        detail = f"did you mean {describe(matches[0])}? Retry with the id or exact name."
    else:
        candidates = ", ".join(describe(match) for match in matches[:8])
        detail = (
            f"{ref!r} only substring-matches multiple {kind}s: {candidates}. "
            "Retry with an id or exact name."
        )
    if truncated:
        detail += (
            f" The {kind} directory is truncated at {directory_size} entries, "
            "so additional matches may exist beyond the cap."
        )
    return ResolutionError(detail)


def _truncated_error[T](
    kind: str, ref: str, directory_size: int, matches: list[T]
) -> ResolutionError:
    match_note = f" ({len(matches)} partial match(es) in the fetched subset)" if matches else ""
    return ResolutionError(
        f"{kind} directory truncated at {directory_size} entries{match_note}; "
        f"{ref!r} may exist beyond the cap. Retry with an id"
        + (" or email." if kind == "user" else ".")
    )


def _exactly_one[T](matches: list[T], ref: str, *, kind: str, describe: Callable[[T], str]) -> T:
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ResolutionError(f"no {kind} matching {ref!r}")
    candidates = ", ".join(describe(match) for match in matches[:8])
    more = f" (+{len(matches) - 8} more)" if len(matches) > 8 else ""
    raise ResolutionError(
        f"{ref!r} is ambiguous; matching {kind}s: {candidates}{more}. Retry with an id."
    )
