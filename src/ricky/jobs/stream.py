"""Harness-owned collection over typed stream adapters and committed cursors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from pydantic import BaseModel, JsonValue

from ricky.jobs.sources import CollectedBatch, JobStreamRegistry
from ricky.jobs.spec import SlackStreamSourceSpec
from ricky.jobs.store import JobRunStore
from ricky.profiles import ProfileScope


async def collect_stream(
    source: SlackStreamSourceSpec,
    *,
    registry: JobStreamRegistry,
    store: JobRunStore,
    job_name: str,
    profile_scope: ProfileScope,
    upper_bound: datetime | None = None,
) -> CollectedBatch:
    """Resolve an adapter-owned cursor and collect one bounded immutable batch."""

    adapter = registry.get(source.adapter)
    if adapter is None:
        raise ValueError(f"job stream adapter is unavailable: {source.adapter}")
    config = adapter.Config.model_validate(source.model_dump(exclude={"name", "adapter"}))
    cursor = await store.cursor(job_name, source.name, scope=profile_scope)
    upper = (upper_bound or datetime.now(UTC)).astimezone(UTC)
    if cursor is None:
        cursor = cast(
            JsonValue,
            {"ts": f"{(upper - timedelta(hours=source.initial_lookback_hours)).timestamp():.6f}"},
        )
    batch = await adapter.collect(
        config,
        cursor=cursor,
        upper_bound=upper,
        limit=source.item_limit,
    )
    if batch.input_cursor != cursor:
        raise ValueError("stream adapter returned a mismatched input cursor")
    if len(batch.items) > source.item_limit:
        raise ValueError("stream adapter exceeded the configured item limit")
    return batch


def validate_stream_source(source: SlackStreamSourceSpec, registry: JobStreamRegistry) -> BaseModel:
    adapter = registry.get(source.adapter)
    if adapter is None:
        raise ValueError(f"job stream adapter is unavailable: {source.adapter}")
    return adapter.Config.model_validate(source.model_dump(exclude={"name", "adapter"}))
