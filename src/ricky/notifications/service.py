"""Validated notification producer service and deterministic state helpers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from ricky.attachments import (
    AttachmentInput,
    AttachmentSnapshotBatch,
    LoadedAttachment,
    StoredAttachment,
    delete_attachment_snapshots,
    load_attachments,
    snapshot_attachment_batch,
)
from ricky.config import RickySettings
from ricky.notifications.routes import RoutePolicy
from ricky.notifications.store import NotificationStore
from ricky.notifications.types import (
    CorrelationRef,
    NotificationRecord,
    NotificationRequest,
    NotificationUrgency,
)
from ricky.profiles import ProfileLabel, ProfileScope


def _attachment_signature(
    attachments: list[StoredAttachment],
) -> tuple[tuple[str, str, int, str], ...]:
    """Compare content identity without attempt-specific storage paths."""

    return tuple(
        (
            attachment.filename,
            attachment.media_type,
            attachment.size_bytes,
            attachment.sha256,
        )
        for attachment in attachments
    )


class NotificationService:
    """The only normal entry point used by notification producers."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        store: NotificationStore | None = None,
        routes: RoutePolicy | None = None,
    ) -> None:
        self._settings = settings
        self.settings = settings.messaging
        self.store = store or NotificationStore(settings)
        self.routes = routes or RoutePolicy(settings)

    async def enqueue(
        self,
        request: NotificationRequest,
        *,
        scope: ProfileScope,
    ) -> NotificationRecord:
        """Validate policy and text limits, then atomically persist one request."""

        await self._validate_request(request, scope=scope)
        await self.store.initialize()
        return await self.store.enqueue(request, scope=scope)

    async def _validate_request(
        self,
        request: NotificationRequest,
        *,
        scope: ProfileScope,
    ) -> None:
        if not scope.permits(request.profile_label):
            raise ValueError("notification profile label is outside the active profile scope")
        if request.title is not None and len(request.title) > self.settings.title_char_limit:
            raise ValueError(
                f"notification title exceeds {self.settings.title_char_limit} characters"
            )
        if len(request.body) > self.settings.body_char_limit:
            raise ValueError(
                f"notification body exceeds {self.settings.body_char_limit} characters"
            )
        if len(request.attachments) > self.settings.attachment_count_limit:
            raise ValueError("notification exceeds the configured attachment count limit")
        if any(
            attachment.size_bytes > self.settings.attachment_file_byte_limit
            for attachment in request.attachments
        ):
            raise ValueError("notification attachment exceeds the configured file limit")
        if (
            sum(attachment.size_bytes for attachment in request.attachments)
            > self.settings.attachment_total_byte_limit
        ):
            raise ValueError("notification attachments exceed the configured total limit")
        for correlation in request.correlations:
            if not set(correlation.profile_label.required_profiles).issubset(
                request.profile_label.required_profiles
            ):
                raise ValueError("notification profile label omits a correlated profile")
        await self.routes.validate(request.route, request.profile_label)

    async def enqueue_with_attachments(
        self,
        request: NotificationRequest,
        *,
        attachments: list[AttachmentInput],
        cwd: Path,
        scope: ProfileScope,
    ) -> NotificationRecord:
        """Load source files once, then enqueue their immutable snapshot references."""

        if request.attachments:
            raise ValueError("notification cannot mix source and stored attachments")
        await self._validate_request(request, scope=scope)
        configured = self.settings
        loaded = await asyncio.to_thread(
            load_attachments,
            attachments,
            cwd=cwd,
            settings=self._settings,
            profile_scope=scope,
            count_limit=configured.attachment_count_limit,
            file_byte_limit=configured.attachment_file_byte_limit,
            total_byte_limit=configured.attachment_total_byte_limit,
        )
        return await self._enqueue_with_loaded_attachments(
            request,
            attachments=loaded,
            validate_request=False,
            scope=scope,
        )

    async def enqueue_with_loaded_attachments(
        self,
        request: NotificationRequest,
        *,
        attachments: list[LoadedAttachment] | tuple[LoadedAttachment, ...],
        scope: ProfileScope,
    ) -> NotificationRecord:
        """Persist one already-loaded attachment payload without reading sources again."""

        return await self._enqueue_with_loaded_attachments(
            request,
            attachments=attachments,
            validate_request=True,
            scope=scope,
        )

    async def _enqueue_with_loaded_attachments(
        self,
        request: NotificationRequest,
        *,
        attachments: list[LoadedAttachment] | tuple[LoadedAttachment, ...],
        validate_request: bool,
        scope: ProfileScope,
    ) -> NotificationRecord:
        """Perform the common snapshot/enqueue path after optional prior validation."""

        if request.attachments:
            raise ValueError("notification cannot mix loaded and stored attachments")
        self._validate_loaded_attachments(attachments)
        if validate_request:
            await self._validate_request(request, scope=scope)
        await self.store.initialize()
        snapshot = asyncio.create_task(
            asyncio.to_thread(
                snapshot_attachment_batch,
                attachments,
                settings=self._settings,
                notification_id=request.id,
            )
        )
        try:
            batch = await asyncio.shield(snapshot)
        except asyncio.CancelledError:
            # The worker thread cannot be stopped safely. Join it so every
            # created path is known, then remove the attempt-owned batch before
            # allowing cancellation to escape.
            batch = await snapshot
            await self._cleanup_snapshot_batch(batch)
            raise
        stored_request = request.model_copy(update={"attachments": list(batch.attachments)})
        enqueue = asyncio.create_task(self.store.enqueue(stored_request, scope=scope))
        try:
            record = await asyncio.shield(enqueue)
        except asyncio.CancelledError:
            try:
                record = await enqueue
            except BaseException:
                await self._cleanup_snapshot_batch(batch)
                raise
            await self._settle_enqueued_snapshot(stored_request, record, batch)
            raise
        except BaseException:
            await self._cleanup_snapshot_batch(batch)
            raise
        await self._settle_enqueued_snapshot(stored_request, record, batch)
        return record

    def _validate_loaded_attachments(
        self,
        attachments: list[LoadedAttachment] | tuple[LoadedAttachment, ...],
    ) -> None:
        if len(attachments) > self.settings.attachment_count_limit:
            raise ValueError("notification exceeds the configured attachment count limit")
        if any(
            attachment.size_bytes > self.settings.attachment_file_byte_limit
            for attachment in attachments
        ):
            raise ValueError("notification attachment exceeds the configured file limit")
        if (
            sum(attachment.size_bytes for attachment in attachments)
            > self.settings.attachment_total_byte_limit
        ):
            raise ValueError("notification attachments exceed the configured total limit")

    async def _settle_enqueued_snapshot(
        self,
        request: NotificationRequest,
        record: NotificationRecord,
        batch: AttachmentSnapshotBatch,
    ) -> None:
        incoming = _attachment_signature(request.attachments)
        persisted = _attachment_signature(record.request.attachments)
        if record.request.id != request.id:
            await self._cleanup_snapshot_batch(batch)
        elif incoming != persisted:
            # Same deterministic notification id may already exist. Remove
            # only newly-created, unreferenced files; never its prior files.
            referenced = {attachment.storage_path for attachment in record.request.attachments}
            await self._cleanup_snapshot_batch(batch, keep=referenced)
        if incoming != persisted:
            raise ValueError("notification dedupe key conflicts with a different attachment set")

    async def _cleanup_snapshot_batch(
        self,
        batch: AttachmentSnapshotBatch,
        *,
        keep: set[str] | None = None,
    ) -> None:
        removable = [
            path for path in batch.created_storage_paths if keep is None or path not in keep
        ]
        cleanup = asyncio.create_task(
            asyncio.to_thread(delete_attachment_snapshots, self._settings, removable)
        )
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise


def gateway_lifecycle(
    *,
    route: str,
    run_id: str,
    state: Literal["started", "stopping"],
    profile_label: ProfileLabel,
    created_at: datetime | None = None,
) -> NotificationRequest:
    """Build one short, deduplicated gateway lifecycle notification.

    ``run_id`` is created after the single-instance lock is taken, so every
    process lifetime gets exactly one started/stopping request pair.
    """

    title, body = {
        "started": ("Ricky gateway started", "The foreground gateway is online."),
        "stopping": ("Ricky gateway stopping", "The foreground gateway is stopping."),
    }[state]
    return _request(
        route=route,
        title=title,
        body=body,
        urgency="normal",
        source_kind="gateway_lifecycle",
        source_id=run_id,
        dedupe_key=state,
        profile_label=profile_label,
        correlations=[],
        created_at=created_at,
    )


def job_completed(
    *,
    route: str,
    job_name: str,
    run_id: str,
    summary: str,
    profile_label: ProfileLabel,
    created_at: datetime | None = None,
) -> NotificationRequest:
    return _request(
        route=route,
        title=f"Job completed: {job_name}",
        body=summary,
        urgency="normal",
        source_kind="job",
        source_id=job_name,
        dedupe_key=f"completed:{run_id}",
        profile_label=profile_label,
        correlations=[
            CorrelationRef(
                kind="job_run",
                id=run_id,
                revision=None,
                profile_label=profile_label,
            )
        ],
        created_at=created_at,
    )


def job_failed(
    *,
    route: str,
    job_name: str,
    run_id: str,
    summary: str,
    profile_label: ProfileLabel,
    created_at: datetime | None = None,
) -> NotificationRequest:
    return _request(
        route=route,
        title=f"Job failed: {job_name}",
        body=summary,
        urgency="attention",
        source_kind="job",
        source_id=job_name,
        dedupe_key=f"failed:{run_id}",
        profile_label=profile_label,
        correlations=[
            CorrelationRef(
                kind="job_run",
                id=run_id,
                revision=None,
                profile_label=profile_label,
            )
        ],
        created_at=created_at,
    )


def job_needs_approval(
    *,
    route: str,
    job_name: str,
    run_id: str,
    summary: str,
    profile_label: ProfileLabel,
    created_at: datetime | None = None,
) -> NotificationRequest:
    return _request(
        route=route,
        title=f"Job needs approval: {job_name}",
        body=summary,
        urgency="attention",
        source_kind="job",
        source_id=job_name,
        dedupe_key=f"approval:{run_id}",
        profile_label=profile_label,
        correlations=[
            CorrelationRef(
                kind="job_run",
                id=run_id,
                revision=None,
                profile_label=profile_label,
            )
        ],
        created_at=created_at,
    )


def task_waits_for_user(
    *,
    route: str,
    task_id: str,
    revision: int,
    summary: str,
    profile_label: ProfileLabel,
    created_at: datetime | None = None,
) -> NotificationRequest:
    return _request(
        route=route,
        title="Task needs your input",
        body=summary,
        urgency="attention",
        source_kind="durable_task",
        source_id=task_id,
        dedupe_key=f"waiting-user:{revision}",
        profile_label=profile_label,
        correlations=[
            CorrelationRef(
                kind="task",
                id=task_id,
                revision=revision,
                profile_label=profile_label,
            )
        ],
        created_at=created_at,
    )


def task_blocked(
    *,
    route: str,
    task_id: str,
    revision: int,
    summary: str,
    profile_label: ProfileLabel,
    created_at: datetime | None = None,
) -> NotificationRequest:
    return _request(
        route=route,
        title="Task is blocked",
        body=summary,
        urgency="attention",
        source_kind="durable_task",
        source_id=task_id,
        dedupe_key=f"blocked:{revision}",
        profile_label=profile_label,
        correlations=[
            CorrelationRef(
                kind="task",
                id=task_id,
                revision=revision,
                profile_label=profile_label,
            )
        ],
        created_at=created_at,
    )


def workflow_completed(
    *,
    route: str,
    workflow_name: str,
    run_id: str,
    summary: str,
    profile_label: ProfileLabel,
    created_at: datetime | None = None,
) -> NotificationRequest:
    return _request(
        route=route,
        title=f"Workflow completed: {workflow_name}",
        body=summary,
        urgency="normal",
        source_kind="workflow",
        source_id=workflow_name,
        dedupe_key=f"completed:{run_id}",
        profile_label=profile_label,
        correlations=[
            CorrelationRef(
                kind="workflow_run",
                id=run_id,
                revision=None,
                profile_label=profile_label,
            )
        ],
        created_at=created_at,
    )


def external_effect_in_doubt(
    *,
    route: str,
    source_kind: str,
    source_id: str,
    occurrence_key: str,
    summary: str,
    profile_label: ProfileLabel,
    correlations: list[CorrelationRef] | None = None,
    created_at: datetime | None = None,
) -> NotificationRequest:
    return _request(
        route=route,
        title="External action needs verification",
        body=summary,
        urgency="urgent",
        source_kind=source_kind,
        source_id=source_id,
        dedupe_key=f"effect-in-doubt:{occurrence_key}",
        profile_label=profile_label,
        correlations=correlations or [],
        created_at=created_at,
    )


def _request(
    *,
    route: str,
    title: str | None,
    body: str,
    urgency: NotificationUrgency,
    source_kind: str,
    source_id: str,
    dedupe_key: str,
    profile_label: ProfileLabel,
    correlations: list[CorrelationRef],
    created_at: datetime | None,
) -> NotificationRequest:
    return NotificationRequest(
        id=f"notification_{uuid4().hex}",
        route=route,
        title=title,
        body=body,
        body_format="portable_markdown_v1",
        urgency=urgency,
        source_kind=source_kind,
        profile_label=profile_label,
        source_id=source_id,
        dedupe_key=dedupe_key,
        correlations=correlations,
        created_at=created_at or datetime.now(UTC),
        expires_at=None,
    )
