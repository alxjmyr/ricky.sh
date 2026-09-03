"""Permission-gated agent tool for durable user notifications."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ricky.attachments import (
    TASK_ARTIFACT_ATTACHMENT_HELP,
    AttachmentArgument,
    LoadedAttachment,
    PreparedAttachmentEffect,
    attachment_source_label,
    load_attachments,
)
from ricky.notifications.service import NotificationService
from ricky.notifications.types import NotificationRequest
from ricky.permissions.types import GrantScope
from ricky.tools.base import (
    EffectIdentity,
    EffectReceipt,
    PreparedEffect,
    Risk,
    ToolContext,
    ToolResult,
)


class NotifyUserParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    route: str = Field(min_length=1, max_length=200)
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
        description="Plain-text notification title.",
    )
    body: str = Field(
        min_length=1,
        max_length=20_000,
        description=(
            "Mobile-first portable Markdown. Use attachments for files or media; do not "
            "include raw HTML, Markdown images, platform links, mentions, or controls."
        ),
    )
    occurrence_key: str = Field(min_length=1, max_length=500)
    attachments: list[AttachmentArgument] = Field(
        default_factory=list,
        description=(
            "Absolute, home-relative, or project-relative host files, or logical "
            "durable-task artifacts, to attach to the "
            f"routed message. {TASK_ARTIFACT_ATTACHMENT_HELP}"
        ),
    )


class NotifyUserTool:
    """Enqueue a message on one explicitly enabled logical route."""

    name: ClassVar[str] = "notify_user"
    description: ClassVar[str] = (
        "Queue a durable notification, optionally with local-file attachments, to the "
        "user through an allowed logical route. "
        "Provide a stable occurrence_key so retries do not create duplicates."
    )
    Params: ClassVar[type[BaseModel]] = NotifyUserParams
    risk: ClassVar[Risk] = "mutating"
    capability_id = "builtin.notification.mutate"
    effect_kind = "ricky_state"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, service: NotificationService, *, allowed_routes: set[str]) -> None:
        self._service = service
        self._allowed_routes = frozenset(allowed_routes)

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = NotifyUserParams.model_validate(params)
        if args.route not in self._allowed_routes:
            return ToolResult(
                content=f"notification route is not allowed: {args.route}",
                is_error=True,
            )
        prepared = await self.prepare_effect(args.model_dump(mode="python"), ctx)
        return await self.run_prepared(args, prepared, ctx)

    async def run_prepared(
        self,
        params: BaseModel,
        prepared: PreparedEffect,
        ctx: ToolContext,
    ) -> ToolResult:
        args = NotifyUserParams.model_validate(params)
        if not isinstance(prepared, PreparedAttachmentEffect) or prepared.tool_name != self.name:
            raise ValueError(f"prepared effect does not belong to {self.name}")
        if args.route not in self._allowed_routes:
            return ToolResult(
                content=f"notification route is not allowed: {args.route}",
                is_error=True,
            )
        profile_label = ctx.session.profile_scope.label()
        request = NotificationRequest(
            id=_notification_id(ctx.session.id, args.occurrence_key),
            route=args.route,
            title=args.title,
            body=args.body,
            body_format="portable_markdown_v1",
            urgency="normal",
            source_kind="agent_session",
            profile_label=profile_label,
            source_id=ctx.session.id,
            dedupe_key=args.occurrence_key,
            correlations=[],
            created_at=datetime.now(UTC),
            expires_at=None,
        )
        record = await self._service.enqueue_with_loaded_attachments(
            request,
            attachments=prepared.attachments,
            scope=ctx.session.profile_scope,
        )
        return ToolResult(
            content=f"notification queued: {record.request.id}",
            effect_receipt=EffectReceipt(
                disposition="performed",
                provider_reference=record.request.id,
            ),
        )

    def normalize_permission_args(
        self,
        args: dict[str, object],
        ctx: ToolContext,
    ) -> dict[str, object]:
        del ctx
        return {
            "route": args.get("route"),
            "title": args.get("title"),
            "body": args.get("body"),
            "attachments": args.get("attachments", []),
        }

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        lines = [
            f"queue notification on exact route {args.get('route')!r}: {args.get('title') or ''}"
        ]
        raw_attachments = args.get("attachments")
        if isinstance(raw_attachments, list) and raw_attachments:
            lines.append("Attachments:")
            for item in raw_attachments:
                if isinstance(item, dict):
                    source = attachment_source_label(item)
                    lines.append(f"- {item.get('filename') or source} (source: {source})")
        return "\n".join(lines)

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope:
        del ctx
        return GrantScope(
            params_equal={"route": args.get("route")},
            label=f"allow notifications to route {args.get('route')!r} for this session",
            allow_unconstrained=False,
        )

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        route = str(args.get("route", ""))
        occurrence = str(args.get("occurrence_key", ""))
        parsed = NotifyUserParams.model_validate(args)
        if parsed.route not in self._allowed_routes:
            raise ValueError(f"notification route is not allowed: {parsed.route}")
        configured = ctx.settings.messaging
        attachments = load_attachments(
            parsed.attachments,
            cwd=ctx.cwd,
            settings=ctx.settings,
            profile_scope=ctx.session.profile_scope,
            count_limit=configured.attachment_count_limit,
            file_byte_limit=configured.attachment_file_byte_limit,
            total_byte_limit=configured.attachment_total_byte_limit,
        )
        return self._effect_identity(route, occurrence, attachments, ctx)

    async def prepare_effect(
        self, args: dict[str, object], ctx: ToolContext
    ) -> PreparedAttachmentEffect:
        parsed = NotifyUserParams.model_validate(args)
        if parsed.route not in self._allowed_routes:
            raise ValueError(f"notification route is not allowed: {parsed.route}")
        configured = ctx.settings.messaging
        attachments = tuple(
            await asyncio.to_thread(
                load_attachments,
                parsed.attachments,
                cwd=ctx.cwd,
                settings=ctx.settings,
                profile_scope=ctx.session.profile_scope,
                count_limit=configured.attachment_count_limit,
                file_byte_limit=configured.attachment_file_byte_limit,
                total_byte_limit=configured.attachment_total_byte_limit,
            )
        )
        preview = self.summarize_permission(parsed.model_dump(mode="python"), ctx)
        if attachments:
            preview += "\nPrepared attachments:\n" + "\n".join(
                f"- {attachment.filename} "
                f"({attachment.size_bytes} bytes, sha256 {attachment.sha256})"
                for attachment in attachments
            )
        return PreparedAttachmentEffect(
            tool_name=self.name,
            identity=self._effect_identity(
                parsed.route,
                parsed.occurrence_key,
                attachments,
                ctx,
            ),
            permission_summary=preview,
            attachments=attachments,
        )

    def _effect_identity(
        self,
        route: str,
        occurrence: str,
        attachments: list[LoadedAttachment] | tuple[LoadedAttachment, ...],
        ctx: ToolContext,
    ) -> EffectIdentity:
        digests = ",".join(attachment.sha256 for attachment in attachments)
        canonical = f"notify_user\0{ctx.session.id}\0{route}\0{occurrence}\0{digests}"
        return EffectIdentity(
            operation="notify_user",
            target=route,
            occurrence=occurrence,
            summary=f"Queue notification to logical route {route}",
            action_key=hashlib.sha256(canonical.encode()).hexdigest(),
        )


def _notification_id(session_id: str, occurrence_key: str) -> str:
    digest = hashlib.sha256(f"{session_id}\0{occurrence_key}".encode()).hexdigest()[:32]
    return f"notification_{digest}"
