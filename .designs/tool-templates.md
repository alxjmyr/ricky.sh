# Canonical tool templates

These are starting shapes, not alternate framework APIs. Keep a tool small,
inject its client or store, and let `ToolRegistry` own argument normalization,
validation, result enforcement, and offloading. Every top-level `Params` model
uses `ConfigDict(extra="forbid", strict=True)`; nested input models also forbid
extra fields.

Replace the illustrative `builtin.example.*` ids with an existing coherent
capability, or add one provider-neutral `CapabilitySpec` at the composition
root. A tool never creates a new policy group merely by spelling its id.

## Read-only tool

```python
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ricky.tools import Risk, ToolContext, ToolResult


class LookupClient(Protocol):
    async def lookup(self, item_id: str) -> str: ...


class LookupParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    item_id: str = Field(min_length=1)


class LookupTool:
    name: ClassVar[str] = "example_lookup"
    description: ClassVar[str] = "Read one example by its exact id."
    Params: ClassVar[type[BaseModel]] = LookupParams
    risk: ClassVar[Risk] = "read_only"
    capability_id = "builtin.example.read"
    effect_kind = "none"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, client: LookupClient) -> None:
        self._client = client

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = LookupParams.model_validate(params)
        return ToolResult(content=await self._client.lookup(args.item_id))
```

Read-only tools return no `EffectReceipt`. For stable machine-readable output,
declare a Pydantic `Result` and put its JSON-compatible dump in
`ToolResult.data`.

## Ricky-state mutation

```python
class RenameParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    item_id: str = Field(min_length=1)
    new_name: str = Field(min_length=1)


class RenameTool:
    name: ClassVar[str] = "example_rename"
    description: ClassVar[str] = "Rename one Ricky-owned example record."
    Params: ClassVar[type[BaseModel]] = RenameParams
    risk: ClassVar[Risk] = "mutating"
    capability_id = "builtin.example.mutate"
    effect_kind = "ricky_state"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, store: ExampleStore) -> None:
        self._store = store

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = RenameParams.model_validate(params)
        record = await self._store.rename(args.item_id, args.new_name)
        return ToolResult(content=f"Renamed {record.id}.")
```

The owning store enforces its transaction and concurrency invariants. Use a
stable `state_guard_id` only when unattended execution requires a registered
subsystem wrapper. Ricky-state mutations do not use external-effect receipts.

## External mutation

```python
from ricky.tools import EffectReceipt, make_effect_identity


class SendParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    recipient: str = Field(min_length=1)
    body: str = Field(min_length=1)
    request_id: str = Field(min_length=1)


class SendTool:
    name: ClassVar[str] = "example_send"
    description: ClassVar[str] = "Send one message to an exact recipient."
    Params: ClassVar[type[BaseModel]] = SendParams
    risk: ClassVar[Risk] = "mutating"
    capability_id = "builtin.example.mutate"
    effect_kind = "external"
    unattended = "allowed"
    state_guard_id = None

    def __init__(self, client: SendClient) -> None:
        self._client = client

    def effect_identity(self, args: dict[str, object], ctx: ToolContext):
        del ctx
        parsed = SendParams.model_validate(args)
        self._client.validate_recipient(parsed.recipient)
        return make_effect_identity(
            operation=self.name,
            target=parsed.recipient,
            occurrence=parsed.request_id,
            summary=f"Send one example message to {parsed.recipient}",
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        del ctx
        args = SendParams.model_validate(params)
        provider_id = await self._client.send(args.recipient, args.body)
        return ToolResult(
            content=f"Sent message {provider_id}.",
            effect_receipt=EffectReceipt(
                disposition="performed",
                provider_reference=provider_id,
            ),
        )
```

Complete deterministic local validation in `effect_identity`, before effect
reservation. Map deterministic provider rejection to `not_performed` and an
ambiguous outcome after possible dispatch to `in_doubt`. Never return
`performed` without provider evidence.

## External mutation with attachments

Use `AttachmentInput`; never accept or reconstruct an internal storage path.
Resolve and hash attachments during `effect_identity`. Also prepare the exact
bytes before reservation so dispatch cannot reread different content from a
mutable source path.

```python
import asyncio
import hashlib
import json

from ricky.attachments import (
    AttachmentInput,
    LoadedAttachment,
    PreparedAttachmentEffect,
    load_attachments,
)
from ricky.tools import PreparedEffect


class SendFilesParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    recipient: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    attachments: list[AttachmentInput] = Field(min_length=1)


class SendFilesTool(SendTool):
    name: ClassVar[str] = "example_send_files"
    description: ClassVar[str] = "Send exact confined files to one recipient."
    Params: ClassVar[type[BaseModel]] = SendFilesParams

    def _load(self, args: SendFilesParams, ctx: ToolContext):
        limits = ctx.settings.messaging
        return load_attachments(
            args.attachments,
            cwd=ctx.cwd,
            settings=ctx.settings,
            profile_scope=ctx.session.profile_scope,
            count_limit=limits.attachment_count_limit,
            file_byte_limit=limits.attachment_file_byte_limit,
            total_byte_limit=limits.attachment_total_byte_limit,
        )

    def _identity(
        self,
        parsed: SendFilesParams,
        loaded: tuple[LoadedAttachment, ...],
    ):
        occurrence = hashlib.sha256(
            json.dumps(
                {
                    "request_id": parsed.request_id,
                    "attachment_sha256": [item.sha256 for item in loaded],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return make_effect_identity(
            operation=self.name,
            target=parsed.recipient,
            occurrence=occurrence,
            summary=f"Send {len(loaded)} attachment(s) to {parsed.recipient}",
        )

    def effect_identity(self, args: dict[str, object], ctx: ToolContext):
        parsed = SendFilesParams.model_validate(args)
        return self._identity(parsed, tuple(self._load(parsed, ctx)))

    async def prepare_effect(
        self, args: dict[str, object], ctx: ToolContext
    ) -> PreparedAttachmentEffect:
        parsed = SendFilesParams.model_validate(args)
        loaded = tuple(await asyncio.to_thread(self._load, parsed, ctx))
        return PreparedAttachmentEffect(
            tool_name=self.name,
            identity=self._identity(parsed, loaded),
            attachments=loaded,
        )

    async def run(self, params: BaseModel, ctx: ToolContext) -> ToolResult:
        args = SendFilesParams.model_validate(params)
        loaded = tuple(await asyncio.to_thread(self._load, args, ctx))
        return await self._run_with_attachments(args, loaded)

    async def run_prepared(
        self,
        params: BaseModel,
        prepared: PreparedEffect,
        ctx: ToolContext,
    ) -> ToolResult:
        del ctx
        args = SendFilesParams.model_validate(params)
        if (
            not isinstance(prepared, PreparedAttachmentEffect)
            or prepared.tool_name != self.name
        ):
            raise ValueError(f"prepared effect does not belong to {self.name}")
        return await self._run_with_attachments(args, prepared.attachments)

    async def _run_with_attachments(
        self,
        args: SendFilesParams,
        loaded: tuple[LoadedAttachment, ...],
    ) -> ToolResult:
        provider_id = await self._client.send_files(args.recipient, loaded)
        return ToolResult(
            content=f"Sent {len(loaded)} attachment(s).",
            effect_receipt=EffectReceipt(
                disposition="performed",
                provider_reference=provider_id,
            ),
        )
```

## Required behavioral test

```python
from ricky.tools.testing import assert_tool_contract


async def test_example_send_contract(fake_client, test_context):
    tool = SendTool(fake_client)
    result = await assert_tool_contract(
        tool,
        valid_args={
            "recipient": "person@example.com",
            "body": "Hello",
            "request_id": "test-occurrence-1",
        },
        ctx=test_context,
        secret_values=(fake_client.api_token,),
    )
    assert result.effect_receipt is not None
    assert result.effect_receipt.provider_reference == "fake-message-1"
```

The fake records calls and returns deterministic evidence. Add separate tests
for deterministic local rejection, `not_performed`, `in_doubt`, and
cancellation at every phase that owns a reservation or external request.
