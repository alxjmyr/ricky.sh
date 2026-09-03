"""Fine-grained Gmail tools behind the normal permission boundary."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ricky.attachments import (
    TASK_ARTIFACT_ATTACHMENT_HELP,
    AttachmentArgument,
    LoadedAttachment,
    PreparedAttachmentEffect,
    attachment_source_label,
    load_attachments,
)
from ricky.config import profile_data_subpath
from ricky.permissions.types import GrantScope
from ricky.profiles import ProfileResourceRef
from ricky.tools.base import (
    EffectIdentity,
    EffectReceipt,
    PreparedEffect,
    Risk,
    ToolContext,
    ToolResult,
    make_effect_identity,
)
from ricky.tools.integrations.gmail.client import GmailClient, GmailError
from ricky.tools.integrations.gmail.mime import build_raw_message, decode_base64url
from ricky.tools.integrations.gmail.render import (
    render_drafts,
    render_labels,
    render_message,
    render_search_results,
    render_thread,
)
from ricky.tools.integrations.gmail.types import (
    GmailAttachmentMeta,
    GmailDraftMeta,
    GmailMessage,
    GmailModifyLabelsResult,
    GmailSearchResult,
    GmailSendResult,
    GmailSentAttachment,
    GmailThread,
    GmailThreadResult,
    GmailTrashResult,
)
from ricky.tools.integrations.google.types import GoogleAccountId

_METADATA_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Message-ID"]
_FORBIDDEN_LABEL_IDS = {"SENT", "DRAFT", "DRAFTS"}
_METADATA_FETCH_CONCURRENCY = 8
_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+$")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class GmailSearchParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account: GoogleAccountId
    query: str = Field(
        description=("Gmail search syntax, for example 'from:dana newer_than:7d has:attachment'.")
    )
    label: str | None = Field(
        default=None,
        description="Optional exact Gmail label name or id to require.",
    )
    max_results: int = Field(
        default=0,
        ge=0,
        le=50,
        description="Maximum hits (0 uses gmail.default_list_limit).",
    )
    include_spam_trash: bool = Field(
        default=False,
        description="Include matches from Spam and Trash.",
    )


class GmailReadMessageParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account: GoogleAccountId
    message_id: str = Field(description="Message id from gmail_search or a thread read.")


class GmailReadThreadParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account: GoogleAccountId
    thread_id: str = Field(description="Thread id from gmail_search or a message read.")


class GmailListLabelsParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account: GoogleAccountId


class GmailListDraftsParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account: GoogleAccountId
    max_results: int = Field(
        default=0,
        ge=0,
        le=50,
        description="Maximum drafts (0 uses gmail.default_list_limit).",
    )


class OutboundMessageParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account: GoogleAccountId
    to: list[str] = Field(
        min_length=1,
        description="Explicit RFC email addresses. Use addresses from prior reads or the user.",
    )
    cc: list[str] = Field(default_factory=list, description="Explicit Cc email addresses.")
    subject: str | None = Field(
        default=None,
        description="Subject; omit on a reply to derive Re: from the original.",
    )
    body: str = Field(description="Complete plain-text message body.")
    attachments: list[AttachmentArgument] = Field(
        default_factory=list,
        description=(
            "Confined local files or logical durable-task artifacts to attach. "
            f"{TASK_ARTIFACT_ATTACHMENT_HELP}"
        ),
    )
    reply_to_message_id: str | None = Field(
        default=None,
        description="Original message id when replying; threading headers are derived from it.",
    )

    @field_validator("to", "cc")
    @classmethod
    def validate_recipients(cls, values: list[str]) -> list[str]:
        return [_validate_recipient(value) for value in values]

    @field_validator("subject")
    @classmethod
    def reject_subject_header_injection(cls, value: str | None) -> str | None:
        if value is not None and ("\r" in value or "\n" in value):
            raise ValueError("subject must not contain newlines")
        return value

    @model_validator(mode="after")
    def require_body_or_attachment(self) -> Self:
        if not self.body.strip() and not self.attachments:
            raise ValueError("message must contain a body or at least one attachment")
        return self


class GmailCreateLabelParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account: GoogleAccountId
    name: str = Field(min_length=1, description="New Gmail label name.")


class GmailModifyLabelsParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account: GoogleAccountId
    message_id: str | None = None
    thread_id: str | None = None
    add_labels: list[str] = Field(default_factory=list)
    remove_labels: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_target_and_changes(self) -> Self:
        if (self.message_id is None) == (self.thread_id is None):
            raise ValueError("provide exactly one of message_id or thread_id")
        if not self.add_labels and not self.remove_labels:
            raise ValueError("provide at least one label to add or remove")
        return self


class GmailTrashParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account: GoogleAccountId
    message_id: str | None = None
    thread_id: str | None = None

    @model_validator(mode="after")
    def validate_target(self) -> Self:
        if (self.message_id is None) == (self.thread_id is None):
            raise ValueError("provide exactly one of message_id or thread_id")
        return self


class GmailDownloadAttachmentParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    account: GoogleAccountId
    message_id: str = Field(description="Message id from the attachment metadata line.")
    attachment_id: str = Field(description="Attachment id from the attachment metadata line.")
    filename: str | None = Field(
        default=None,
        min_length=1,
        description="Exact filename from the attachment metadata line; handles rotated ids.",
    )
    mime_type: str | None = Field(
        default=None,
        min_length=1,
        description=("Exact MIME type from the attachment metadata line; disambiguates filenames."),
    )


class _GmailReadTool:
    capability_id = "builtin.email.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None


class _GmailMutationTool:
    name: ClassVar[str]
    Params: ClassVar[type[BaseModel]]
    _client: GmailClient

    capability_id = "builtin.email.mutate"
    effect_kind = "external"
    unattended = "allowed"
    state_guard_id = None

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        parsed = self.Params.model_validate(args)
        attachments = (
            tuple(_load_outbound_attachments(parsed, ctx))
            if isinstance(parsed, OutboundMessageParams)
            else ()
        )
        return self._effect_identity(parsed, attachments)

    def _effect_identity(
        self,
        parsed: BaseModel,
        attachments: tuple[LoadedAttachment, ...],
    ) -> EffectIdentity:
        account = str(getattr(parsed, "account", ""))
        self._client.validate_account(account)
        payload = parsed.model_dump(mode="json")
        if isinstance(parsed, OutboundMessageParams):
            payload["attachment_digests"] = [attachment.sha256 for attachment in attachments]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        occurrence = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return make_effect_identity(
            operation=self.name,
            target=account,
            occurrence=occurrence,
            summary=f"Run {self.name} on Gmail account {account}",
        )


class GmailSearchTool(_GmailReadTool):
    name: ClassVar[str] = "gmail_search"
    description: ClassVar[str] = (
        "Search one Gmail account using Gmail search syntax. Results include raw "
        "message and thread ids for follow-up reads, replies, labels, and trash."
    )
    Params: ClassVar[type[BaseModel]] = GmailSearchParams
    Result: ClassVar[type[BaseModel]] = GmailSearchResult
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = GmailSearchParams.model_validate(params)
        limit = args.max_results or ctx.settings.gmail.default_list_limit
        request: dict[str, str | int | bool | list[str]] = {
            "q": args.query,
            "maxResults": limit,
            "includeSpamTrash": args.include_spam_trash,
        }
        if args.label:
            request["labelIds"] = [(await self._client.resolve_label(args.account, args.label)).id]
        hits, truncated = await self._client.call_paginated(
            args.account,
            "messages",
            params=request,
            items_key="messages",
            max_items=limit,
        )
        message_ids = [str(hit.get("id") or "") for hit in hits]
        payloads, labels = await asyncio.gather(
            _fetch_metadata(
                self._client,
                args.account,
                [f"messages/{message_id}" for message_id in message_ids if message_id],
            ),
            self._client.label_map(args.account),
        )
        messages = [
            GmailMessage.from_api(
                payload,
                body_char_limit=ctx.settings.gmail.body_char_limit,
            )
            for payload in payloads
        ]
        data = GmailSearchResult(
            account=args.account,
            messages=messages,
            truncated=truncated,
        )
        return ToolResult(
            content=render_search_results(
                args.account,
                messages,
                labels,
                truncated=truncated,
            ),
            data=data.model_dump(mode="json"),
        )


class GmailReadMessageTool(_GmailReadTool):
    name: ClassVar[str] = "gmail_read_message"
    description: ClassVar[str] = (
        "Read one complete Gmail message, including its plain-text body and "
        "attachment metadata. Use a message id from gmail_search or a thread."
    )
    Params: ClassVar[type[BaseModel]] = GmailReadMessageParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = GmailReadMessageParams.model_validate(params)
        payload = await self._client.call(
            args.account,
            "GET",
            f"messages/{args.message_id}",
            params={"format": "full"},
        )
        message = GmailMessage.from_api(
            payload,
            body_char_limit=ctx.settings.gmail.body_char_limit,
        )
        return ToolResult(
            content=render_message(
                args.account,
                message,
                await self._client.label_map(args.account),
            )
        )


class GmailReadThreadTool(_GmailReadTool):
    name: ClassVar[str] = "gmail_read_thread"
    description: ClassVar[str] = (
        "Read every message in a Gmail thread in chronological order. "
        "Use a thread id returned by gmail_search or gmail_read_message."
    )
    Params: ClassVar[type[BaseModel]] = GmailReadThreadParams
    Result: ClassVar[type[BaseModel]] = GmailThreadResult
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = GmailReadThreadParams.model_validate(params)
        payload = await self._client.call(
            args.account,
            "GET",
            f"threads/{args.thread_id}",
            params={"format": "full"},
        )
        thread = GmailThread.from_api(
            payload,
            body_char_limit=ctx.settings.gmail.body_char_limit,
        )
        data = GmailThreadResult(account=args.account, thread=thread)
        return ToolResult(
            content=render_thread(
                args.account,
                thread,
                await self._client.label_map(args.account),
            ),
            data=data.model_dump(mode="json"),
        )


class GmailListLabelsTool(_GmailReadTool):
    name: ClassVar[str] = "gmail_list_labels"
    description: ClassVar[str] = (
        "List Gmail label names and ids for one account. Use exact names or ids "
        "with gmail_modify_labels; archive by removing INBOX and mark read by "
        "removing UNREAD."
    )
    Params: ClassVar[type[BaseModel]] = GmailListLabelsParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = GmailListLabelsParams.model_validate(params)
        return ToolResult(
            content=render_labels(args.account, await self._client.labels(args.account))
        )


class GmailListDraftsTool(_GmailReadTool):
    name: ClassVar[str] = "gmail_list_drafts"
    description: ClassVar[str] = (
        "List real Gmail drafts for one account with message and thread ids."
    )
    Params: ClassVar[type[BaseModel]] = GmailListDraftsParams
    risk: ClassVar[Risk] = "read_only"

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = GmailListDraftsParams.model_validate(params)
        limit = args.max_results or ctx.settings.gmail.default_list_limit
        items, truncated = await self._client.call_paginated(
            args.account,
            "drafts",
            params={"maxResults": limit},
            items_key="drafts",
            max_items=limit,
        )
        draft_ids = [str(item.get("id") or "") for item in items]
        payloads, labels = await asyncio.gather(
            _fetch_metadata(
                self._client,
                args.account,
                [f"drafts/{draft_id}" for draft_id in draft_ids if draft_id],
            ),
            self._client.label_map(args.account),
        )
        drafts = [
            GmailDraftMeta.from_api(
                payload,
                body_char_limit=ctx.settings.gmail.body_char_limit,
            )
            for payload in payloads
        ]
        return ToolResult(
            content=render_drafts(
                args.account,
                drafts,
                labels,
                truncated=truncated,
            )
        )


class GmailCreateDraftTool(_GmailMutationTool):
    name: ClassVar[str] = "gmail_create_draft"
    description: ClassVar[str] = (
        "Create a real Gmail draft for later review or editing in Gmail. "
        "Recipients are explicit addresses; optional local-file attachments are supported, "
        "and creation is permission-gated."
    )
    Params: ClassVar[type[BaseModel]] = OutboundMessageParams
    risk: ClassVar[Risk] = "mutating"

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        return _outbound_preview("create Gmail draft", args)

    async def prepare_effect(
        self, args: dict[str, object], ctx: ToolContext
    ) -> PreparedAttachmentEffect:
        parsed = OutboundMessageParams.model_validate(args)
        attachments = tuple(await asyncio.to_thread(_load_outbound_attachments, parsed, ctx))
        return PreparedAttachmentEffect(
            tool_name=self.name,
            identity=self._effect_identity(parsed, attachments),
            permission_summary=_prepared_outbound_preview(
                "create Gmail draft", parsed, attachments
            ),
            attachments=attachments,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = OutboundMessageParams.model_validate(params)
        prepared = await self.prepare_effect(args.model_dump(mode="python"), ctx)
        return await self.run_prepared(args, prepared, ctx)

    async def run_prepared(
        self,
        params: BaseModel,
        prepared: PreparedEffect,
        ctx: ToolContext,
    ) -> ToolResult:
        args = OutboundMessageParams.model_validate(params)
        attachments = _require_prepared_attachments(prepared, tool_name=self.name)
        message = await _outbound_payload(self._client, args, ctx, attachments)
        payload = await self._client.call(
            args.account,
            "POST",
            "drafts",
            json_body={"message": message},
            read_only=False,
            mutation_action="create draft",
            mutation_check="Gmail Drafts",
        )
        draft_id = str(payload.get("id") or "")
        created = payload.get("message") or {}
        message_id = str(created.get("id") or "")
        thread_id = str(created.get("threadId") or message.get("threadId") or "")
        return ToolResult(
            content=(
                f"[{args.account}] Created Gmail draft {draft_id} "
                f"(message {message_id}, thread {thread_id}) with "
                f"{len(attachments)} attachment(s){_attachment_names(attachments)}."
            ),
            effect_receipt=EffectReceipt(
                disposition="performed", provider_reference=draft_id or None
            ),
        )


class GmailSendMessageTool(_GmailMutationTool):
    name: ClassVar[str] = "gmail_send_message"
    description: ClassVar[str] = (
        "Send a Gmail message, optionally with local-file attachments, directly from the "
        "selected account. "
        "Use explicit recipient addresses; the permission prompt is the final "
        "review gate and shows the complete body."
    )
    Params: ClassVar[type[BaseModel]] = OutboundMessageParams
    Result: ClassVar[type[BaseModel]] = GmailSendResult
    risk: ClassVar[Risk] = "mutating"

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        return _outbound_preview("send Gmail message", args)

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        """Identify one exact outbound message before Gmail dispatch."""

        message = OutboundMessageParams.model_validate(args)
        attachments = tuple(_load_outbound_attachments(message, ctx))
        return self._effect_identity(message, attachments)

    def _effect_identity(
        self,
        parsed: BaseModel,
        attachments: tuple[LoadedAttachment, ...],
    ) -> EffectIdentity:
        outbound = OutboundMessageParams.model_validate(parsed)
        # Reject deterministic local configuration errors before the guarded
        # dispatcher reserves an effect slot. No provider call has happened yet,
        # so an unknown account must not become an ambiguous external action.
        self._client.validate_account(outbound.account)
        payload = outbound.model_dump(mode="json")
        payload["attachment_digests"] = [attachment.sha256 for attachment in attachments]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        action_key = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        recipients = ",".join([*outbound.to, *outbound.cc])
        subject = outbound.subject or "[derived/empty subject]"
        return EffectIdentity(
            operation="gmail.send",
            target=f"{outbound.account}:{recipients}"[:500],
            occurrence=action_key,
            summary=(
                f"Send Gmail message from {outbound.account} to {', '.join(outbound.to)} "
                f"with subject {subject!r}"
            )[:2_000],
            action_key=action_key,
        )

    async def prepare_effect(
        self, args: dict[str, object], ctx: ToolContext
    ) -> PreparedAttachmentEffect:
        parsed = OutboundMessageParams.model_validate(args)
        attachments = tuple(await asyncio.to_thread(_load_outbound_attachments, parsed, ctx))
        return PreparedAttachmentEffect(
            tool_name=self.name,
            identity=self._effect_identity(parsed, attachments),
            permission_summary=_prepared_outbound_preview(
                "send Gmail message", parsed, attachments
            ),
            attachments=attachments,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = OutboundMessageParams.model_validate(params)
        prepared = await self.prepare_effect(args.model_dump(mode="python"), ctx)
        return await self.run_prepared(args, prepared, ctx)

    async def run_prepared(
        self,
        params: BaseModel,
        prepared: PreparedEffect,
        ctx: ToolContext,
    ) -> ToolResult:
        args = OutboundMessageParams.model_validate(params)
        attachments = _require_prepared_attachments(prepared, tool_name=self.name)
        message = await _outbound_payload(self._client, args, ctx, attachments)
        payload = await self._client.call(
            args.account,
            "POST",
            "messages/send",
            json_body=message,
            read_only=False,
            mutation_action="send message",
            mutation_check="the Sent folder",
        )
        result = GmailSendResult(
            account=args.account,
            message_id=str(payload.get("id") or ""),
            thread_id=str(payload.get("threadId") or ""),
            attachments=[
                GmailSentAttachment(
                    filename=attachment.filename,
                    media_type=attachment.media_type,
                    size_bytes=attachment.size_bytes,
                    sha256=attachment.sha256,
                )
                for attachment in attachments
            ],
        )
        return ToolResult(
            content=(
                f"[{args.account}] Sent Gmail message {payload.get('id', '')} "
                f"(thread {payload.get('threadId', '')}) with "
                f"{len(attachments)} attachment(s){_attachment_names(attachments)}."
            ),
            data=result.model_dump(mode="json"),
            effect_receipt=EffectReceipt(
                disposition="performed",
                provider_reference=result.message_id or None,
            ),
        )


class GmailCreateLabelTool(_GmailMutationTool):
    name: ClassVar[str] = "gmail_create_label"
    description: ClassVar[str] = (
        "Create a new user label in one Gmail account. Creation is permission-gated."
    )
    Params: ClassVar[type[BaseModel]] = GmailCreateLabelParams
    risk: ClassVar[Risk] = "mutating"

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        parsed = GmailCreateLabelParams.model_validate(args)
        if not parsed.name.strip():
            raise ValueError("label name must not be blank")
        return super().effect_identity(args, ctx)

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        return f"account: {args.get('account', '')}\ncreate Gmail label: {args.get('name', '')}"

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope | None:
        del ctx
        account = str(args.get("account", ""))
        return GrantScope(
            params_equal={"account": account},
            label=f"gmail_create_label on {account}",
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = GmailCreateLabelParams.model_validate(params)
        name = args.name.strip()
        if not name:
            return ToolResult(content="label name must not be blank", is_error=True)
        payload = await self._client.call(
            args.account,
            "POST",
            "labels",
            json_body={"name": name},
            read_only=False,
            mutation_action="create label",
            mutation_check="the Gmail label list",
        )
        self._client.invalidate_labels(args.account)
        return ToolResult(
            content=(
                f"[{args.account}] Created Gmail label {payload.get('name', name)!r} "
                f"(id {payload.get('id', '')})."
            ),
            effect_receipt=EffectReceipt(
                disposition="performed",
                provider_reference=str(payload.get("id") or "") or None,
            ),
        )


class GmailModifyLabelsTool(_GmailMutationTool):
    name: ClassVar[str] = "gmail_modify_labels"
    description: ClassVar[str] = (
        "Add or remove exact Gmail label names/ids on exactly one message or "
        "thread. Archive by removing INBOX; mark read by removing UNREAD. "
        "SENT and DRAFT cannot be modified."
    )
    Params: ClassVar[type[BaseModel]] = GmailModifyLabelsParams
    Result: ClassVar[type[BaseModel]] = GmailModifyLabelsResult
    risk: ClassVar[Risk] = "mutating"

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        parsed = GmailModifyLabelsParams.model_validate(args)
        overlap = {item.casefold() for item in parsed.add_labels}.intersection(
            item.casefold() for item in parsed.remove_labels
        )
        if overlap:
            raise ValueError("cannot add and remove the same labels: " + ", ".join(sorted(overlap)))
        return super().effect_identity(args, ctx)

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        target = _target_preview(args)
        return (
            f"account: {args.get('account', '')}\n"
            f"modify Gmail labels on {target}\n"
            f"add: {_preview_list(args.get('add_labels'))}\n"
            f"remove: {_preview_list(args.get('remove_labels'))}"
        )

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope | None:
        del ctx
        account = str(args.get("account", ""))
        return GrantScope(
            params_equal={"account": account},
            label=f"gmail_modify_labels on {account}",
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = GmailModifyLabelsParams.model_validate(params)
        add_ids = await _resolve_mutable_label_ids(self._client, args.account, args.add_labels)
        remove_ids = await _resolve_mutable_label_ids(
            self._client, args.account, args.remove_labels
        )
        overlap = set(add_ids).intersection(remove_ids)
        if overlap:
            return ToolResult(
                content=f"cannot add and remove the same labels: {', '.join(sorted(overlap))}",
                is_error=True,
            )
        kind, target_id = _target(args.message_id, args.thread_id)
        await self._client.call(
            args.account,
            "POST",
            f"{kind}/{target_id}/modify",
            json_body={"addLabelIds": add_ids, "removeLabelIds": remove_ids},
            read_only=False,
            mutation_action="modify labels",
            mutation_check=f"the target {kind.removesuffix('s')}",
        )
        result = GmailModifyLabelsResult(
            account=args.account,
            target_kind="message" if kind == "messages" else "thread",
            target_id=target_id,
            added_label_ids=add_ids,
            removed_label_ids=remove_ids,
        )
        return ToolResult(
            content=(
                f"[{args.account}] Modified labels on {kind.removesuffix('s')} "
                f"{target_id} (added: {', '.join(add_ids) or 'none'}; "
                f"removed: {', '.join(remove_ids) or 'none'})."
            ),
            data=result.model_dump(mode="json"),
            effect_receipt=EffectReceipt(disposition="performed", provider_reference=target_id),
        )


class GmailTrashTool(_GmailMutationTool):
    name: ClassVar[str] = "gmail_trash"
    description: ClassVar[str] = (
        "Move exactly one previously read Gmail message or thread to Trash "
        "(reversible in Gmail for about 30 days). Mention the subject to the "
        "user before calling; trash is permission-gated."
    )
    Params: ClassVar[type[BaseModel]] = GmailTrashParams
    Result: ClassVar[type[BaseModel]] = GmailTrashResult
    risk: ClassVar[Risk] = "mutating"

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        del ctx
        return f"account: {args.get('account', '')}\nmove Gmail {_target_preview(args)} to Trash"

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope | None:
        del ctx
        account = str(args.get("account", ""))
        return GrantScope(
            params_equal={"account": account},
            label=f"gmail_trash on {account}",
            allow_unconstrained=True,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = GmailTrashParams.model_validate(params)
        kind, target_id = _target(args.message_id, args.thread_id)
        await self._client.call(
            args.account,
            "POST",
            f"{kind}/{target_id}/trash",
            read_only=False,
            mutation_action=f"trash {kind.removesuffix('s')}",
            mutation_check="Gmail Trash",
        )
        result = GmailTrashResult(
            account=args.account,
            target_kind="message" if kind == "messages" else "thread",
            target_id=target_id,
        )
        return ToolResult(
            content=(f"[{args.account}] Moved {kind.removesuffix('s')} {target_id} to Trash."),
            data=result.model_dump(mode="json"),
            effect_receipt=EffectReceipt(disposition="performed", provider_reference=target_id),
        )


class GmailDownloadAttachmentTool(_GmailMutationTool):
    name: ClassVar[str] = "gmail_download_attachment"
    description: ClassVar[str] = (
        "Download one Gmail attachment into the confined Gmail downloads "
        "directory. Use the message id, attachment id, filename, and MIME type from "
        "a prior read so rotated Gmail attachment ids can be reconciled safely. "
        "The local write is permission-gated; attachment content is never printed."
    )
    Params: ClassVar[type[BaseModel]] = GmailDownloadAttachmentParams
    risk: ClassVar[Risk] = "mutating"

    def __init__(self, client: GmailClient) -> None:
        self._client = client

    def effect_identity(self, args: dict[str, object], ctx: ToolContext) -> EffectIdentity:
        parsed = GmailDownloadAttachmentParams.model_validate(args)
        _download_root(ctx, parsed.account)
        return super().effect_identity(parsed.model_dump(mode="python"), ctx)

    def summarize_permission(self, args: dict[str, object], ctx: ToolContext) -> str:
        account = str(args.get("account", ""))
        message_id = str(args.get("message_id", ""))
        attachment_id = str(args.get("attachment_id", ""))
        requested_filename = args.get("filename")
        destination_name = (
            _safe_name(str(requested_filename)) if requested_filename else "<Gmail filename>"
        )
        try:
            candidate = _download_root(ctx, account) / (f"{message_id}-{destination_name}")
            destination = str(candidate)
        except ValueError:
            destination = (
                f"{ctx.settings.gmail.download_dir} (invalid: escapes profile data directory)"
            )
        return (
            f"account: {account}\n"
            f"download Gmail attachment {attachment_id} from message {message_id} "
            f"to {destination}"
        )

    def permission_scope(self, args: dict[str, object], ctx: ToolContext) -> GrantScope | None:
        del ctx
        account = str(args.get("account", ""))
        return GrantScope(
            params_equal={"account": account},
            label=f"gmail_download_attachment on {account}",
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = GmailDownloadAttachmentParams.model_validate(params)
        payload = await self._client.call(
            args.account,
            "GET",
            f"messages/{args.message_id}",
            params={"format": "full"},
        )
        message = GmailMessage.from_api(
            payload,
            body_char_limit=ctx.settings.gmail.body_char_limit,
        )
        attachment = _select_attachment(message.attachments, args)

        filename = f"{args.message_id}-{_safe_name(attachment.filename)}"
        try:
            destination = _download_root(ctx, args.account) / filename
        except ValueError as exc:
            raise GmailError(f"gmail.download_dir escapes profile data directory: {exc}") from exc
        destination = await asyncio.to_thread(_collision_safe, destination)

        attachment_payload = await self._client.call(
            args.account,
            "GET",
            f"messages/{args.message_id}/attachments/{attachment.attachment_id}",
        )
        encoded = attachment_payload.get("data")
        if not isinstance(encoded, str) or not encoded:
            raise GmailError(f"Gmail attachment {attachment.attachment_id!r} returned no content")
        try:
            content = await asyncio.to_thread(decode_base64url, encoded)
        except (ValueError, TypeError) as exc:
            raise GmailError(
                f"Gmail attachment {attachment.attachment_id!r} returned invalid base64 data"
            ) from exc
        await asyncio.to_thread(_atomic_write, destination, content)
        return ToolResult(
            content=(
                f"[{args.account}] Downloaded attachment to {destination} ({len(content)} bytes)."
            ),
            effect_receipt=EffectReceipt(
                disposition="performed", provider_reference=str(destination)
            ),
        )


async def _outbound_payload(
    client: GmailClient,
    args: OutboundMessageParams,
    ctx: ToolContext,
    attachments: tuple[LoadedAttachment, ...],
) -> dict[str, str]:
    reply_message_id: str | None = None
    reply_subject: str | None = None
    thread_id: str | None = None
    if args.reply_to_message_id:
        payload = await client.call(
            args.account,
            "GET",
            f"messages/{args.reply_to_message_id}",
            params={"format": "metadata", "metadataHeaders": _METADATA_HEADERS},
        )
        original = GmailMessage.from_api(
            payload,
            body_char_limit=ctx.settings.gmail.body_char_limit,
        )
        if not original.message_id_header:
            raise GmailError(
                f"message {args.reply_to_message_id!r} has no Message-ID header; "
                "cannot build a standards-compliant threaded reply"
            )
        reply_message_id = original.message_id_header
        reply_subject = original.subject
        thread_id = original.thread_id
    return build_raw_message(
        from_addr=client.account_email(args.account),
        to=args.to,
        cc=args.cc,
        subject=args.subject,
        body=args.body,
        attachments=list(attachments),
        reply_message_id=reply_message_id,
        reply_subject=reply_subject,
        thread_id=thread_id,
    )


def _attachment_names(
    attachments: list[LoadedAttachment] | tuple[LoadedAttachment, ...],
) -> str:
    if not attachments:
        return ""
    return f" ({', '.join(attachment.filename for attachment in attachments)})"


def _require_prepared_attachments(
    prepared: PreparedEffect,
    *,
    tool_name: str,
) -> tuple[LoadedAttachment, ...]:
    if not isinstance(prepared, PreparedAttachmentEffect) or prepared.tool_name != tool_name:
        raise ValueError(f"prepared effect does not belong to {tool_name}")
    return prepared.attachments


def _prepared_outbound_preview(
    action: str,
    args: OutboundMessageParams,
    attachments: tuple[LoadedAttachment, ...],
) -> str:
    preview = _outbound_preview(action, args.model_dump(mode="python"))
    if not attachments:
        return preview
    frozen = "\n".join(
        f"- {attachment.filename} ({attachment.size_bytes} bytes, sha256 {attachment.sha256})"
        for attachment in attachments
    )
    return f"{preview}\nPrepared attachments:\n{frozen}"


async def _fetch_metadata(
    client: GmailClient,
    account: str,
    paths: list[str],
) -> list[dict[str, Any]]:
    """Fetch per-item metadata concurrently, preserving input order."""
    semaphore = asyncio.Semaphore(_METADATA_FETCH_CONCURRENCY)

    async def fetch(path: str) -> dict[str, Any]:
        async with semaphore:
            return await client.call(
                account,
                "GET",
                path,
                params={"format": "metadata", "metadataHeaders": _METADATA_HEADERS},
            )

    return list(await asyncio.gather(*(fetch(path) for path in paths)))


async def _resolve_mutable_label_ids(
    client: GmailClient,
    account: str,
    values: list[str],
) -> list[str]:
    resolved: list[str] = []
    for value in values:
        label = await client.resolve_label(account, value)
        # Only the system labels are immutable; a user label that happens to be
        # named "Sent" or "Drafts" has an opaque id and stays modifiable.
        if label.id.upper() in _FORBIDDEN_LABEL_IDS:
            raise GmailError(f"Gmail label {label.name!r} cannot be manually added or removed")
        if label.id not in resolved:
            resolved.append(label.id)
    return resolved


def _outbound_preview(action: str, args: dict[str, object]) -> str:
    recipients = _preview_list(args.get("to"))
    cc = _preview_list(args.get("cc"))
    subject = args.get("subject")
    reply = args.get("reply_to_message_id")
    lines = [
        f"account: {args.get('account', '')}",
        f"{action}",
        f"To: {recipients}",
        f"Cc: {cc}",
        f"Subject: {subject if subject is not None else '[derived/empty]'}",
    ]
    if reply:
        lines.append(f"Reply target message: {reply}")
    raw_attachments = args.get("attachments")
    if isinstance(raw_attachments, list) and raw_attachments:
        lines.append("Attachments:")
        for item in raw_attachments:
            if isinstance(item, dict):
                filename = item.get("filename")
                source = attachment_source_label(item)
                lines.append(f"- {filename or source} (source: {source})")
    lines.extend(["--- complete body ---", str(args.get("body", "")), "--- end body ---"])
    return "\n".join(lines)


def _load_outbound_attachments(
    args: OutboundMessageParams,
    ctx: ToolContext,
) -> list[LoadedAttachment]:
    settings = ctx.settings.gmail
    return load_attachments(
        args.attachments,
        cwd=ctx.cwd,
        settings=ctx.settings,
        profile_scope=ctx.session.profile_scope,
        count_limit=settings.attachment_count_limit,
        file_byte_limit=settings.attachment_file_byte_limit,
        total_byte_limit=settings.attachment_total_byte_limit,
    )


def _validate_recipient(value: str) -> str:
    from email.utils import getaddresses

    stripped = value.strip()
    if "\r" in stripped or "\n" in stripped:
        raise ValueError("recipient must not contain newlines")
    parsed = getaddresses([stripped])
    if len(parsed) != 1 or not _EMAIL.fullmatch(parsed[0][1]):
        raise ValueError(f"invalid explicit email address: {value!r}")
    return stripped


def _target(
    message_id: str | None, thread_id: str | None
) -> tuple[Literal["messages", "threads"], str]:
    if message_id is not None:
        return "messages", message_id
    if thread_id is not None:
        return "threads", thread_id
    raise GmailError("provide exactly one of message_id or thread_id")


def _target_preview(args: dict[str, object]) -> str:
    if args.get("message_id"):
        return f"message {args.get('message_id')}"
    if args.get("thread_id"):
        return f"thread {args.get('thread_id')}"
    return "[missing target]"


def _preview_list(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "[none]"
    return str(value or "[none]")


def _select_attachment(
    attachments: list[GmailAttachmentMeta],
    args: GmailDownloadAttachmentParams,
) -> GmailAttachmentMeta:
    exact = next(
        (item for item in attachments if item.attachment_id == args.attachment_id),
        None,
    )
    if exact is not None:
        if args.filename is not None and exact.filename != args.filename:
            raise GmailError("attachment id matched a different filename; read the message again")
        if args.mime_type is not None and exact.mime_type != args.mime_type:
            raise GmailError("attachment id matched a different MIME type; read the message again")
        return exact

    candidates = attachments
    if args.filename is not None:
        candidates = [item for item in candidates if item.filename == args.filename]
    if args.mime_type is not None:
        candidates = [item for item in candidates if item.mime_type == args.mime_type]
    selector_given = args.filename is not None or args.mime_type is not None
    if selector_given and len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise GmailError(
            f"attachment {args.attachment_id!r} rotated and its metadata matches "
            f"{len(candidates)} current parts; read the message again and provide "
            "both filename and MIME type"
        )
    raise GmailError(
        f"attachment {args.attachment_id!r} is not present on message "
        f"{args.message_id!r}; read the message again and provide its filename "
        "and MIME type"
    )


def _safe_name(value: str) -> str:
    cleaned = _SAFE_NAME.sub("_", value).strip("._") or "attachment"
    return cleaned[:120]


def _download_root(ctx: ToolContext, account: str) -> Path:
    """Resolve Gmail downloads beneath the selected account's owning profile."""

    profile, separator, local_name = account.partition("/")
    if not separator:
        raise GmailError(f"Google account {account!r} is not profile-qualified")
    resource = ProfileResourceRef(profile=profile, name=local_name)
    if not ctx.session.profile_scope.includes(resource.profile):
        raise GmailError(f"Google account {account!r} belongs to an inaccessible profile")
    return profile_data_subpath(
        ctx.settings,
        resource.profile,
        ctx.settings.gmail.download_dir,
    )


def _collision_safe(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise GmailError(f"could not choose a collision-safe name under {path.parent}")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".part", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        handle = os.fdopen(descriptor, "wb")
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        with suppress(OSError):
            temporary.unlink()
        raise
    # From here the handle owns the descriptor; never close it twice.
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    except BaseException:
        with suppress(OSError):
            temporary.unlink()
        raise
