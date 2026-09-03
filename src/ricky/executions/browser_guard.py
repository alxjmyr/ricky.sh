"""Execution-owned adapter for the browser runtime guard protocol."""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
from contextvars import ContextVar
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import uuid4

from ricky.browser.runtime_guard import (
    BrowserBudgetKind,
    BrowserGuardFacts,
    BrowserRuntimeEvidence,
)
from ricky.executions.browser import (
    BrowserActionEvidence,
    BrowserExecutionScope,
    BrowserLiveBinding,
    BrowserNavigationCheckpoint,
    ParkedBrowserTransaction,
)
from ricky.executions.store import ExecutionStore
from ricky.jobs.browser_store import BrowserRunLedger
from ricky.profiles import ProfileResourceRef, ProfileScope


class BrowserExecutionGuardError(RuntimeError):
    """Live browser facts fall outside the immutable execution scope."""


class DurableBrowserExecutionGuard:
    """Check live facts and persist budgets/evidence for one browser attempt."""

    def __init__(
        self,
        *,
        browser_scope: BrowserExecutionScope,
        profile_scope: ProfileScope,
        provider: str,
        request_id: str | None,
        attempt_id: str,
        run_id: str,
        owner_token: str,
        claim_token: str | None,
        claim_fence: int,
        resource: ProfileResourceRef | None,
        resource_configuration_digest: str | None,
        executions: ExecutionStore | None,
        ledger: BrowserRunLedger,
    ) -> None:
        self.browser_scope = browser_scope
        self.profile_scope = profile_scope
        self.provider = provider
        self.request_id = request_id
        self.attempt_id = attempt_id
        self.run_id = run_id
        self.owner_token = owner_token
        self.claim_token = claim_token
        self.claim_fence = claim_fence
        self.resource = resource
        self.resource_configuration_digest = resource_configuration_digest
        self.executions = executions
        self.ledger = ledger
        self._possible_page_reservations: dict[str, list[str]] = {}
        self._possible_page_lock = asyncio.Lock()
        self._bound_effect: ContextVar[tuple[str, str] | None] = ContextVar(
            f"browser_effect_{attempt_id}", default=None
        )
        self._pending_effect_evidence: dict[str, list[BrowserActionEvidence]] = {}
        self._pending_effect_lock = asyncio.Lock()
        if (request_id is None) != (executions is None) or (request_id is None) != (
            claim_token is None
        ):
            raise ValueError(
                "execution request, claim token, and execution store must be supplied together"
            )

    @property
    def execution_id(self) -> str:
        """Return the exact durable execution owning this browser runtime."""

        return self.request_id or self.run_id

    @property
    def private_origin_ceiling(self) -> tuple[str, ...]:
        """Return exact private origins admitted by the immutable contract."""

        return self.browser_scope.private_origin_ceiling

    @property
    def controlled_page_ceiling(self) -> int:
        """Return the immutable simultaneous controlled-page ceiling."""

        return self.browser_scope.budget.controlled_pages

    def bind_effect_action(self, action_id: str, action_key: str) -> None:
        """Bind this task's browser evidence to one shared effect reservation."""

        if self._bound_effect.get() is not None:
            raise BrowserExecutionGuardError("browser effect action is already bound")
        self._bound_effect.set((action_id, action_key))

    async def settle_effect_action(self, action_id: str) -> None:
        """Publish correlated evidence after its shared ledger action is terminal."""

        bound = self._bound_effect.get()
        if bound is None or bound[0] != action_id:
            raise BrowserExecutionGuardError("browser effect action binding differs at settlement")
        self._bound_effect.set(None)
        async with self._pending_effect_lock:
            pending = self._pending_effect_evidence.pop(action_id, [])
        for evidence in pending:
            await self.ledger.record_action_evidence(
                evidence,
                scope=self.profile_scope,
                owner_token=self.owner_token,
                claim_fence=self.claim_fence,
            )

    async def check(self, facts: BrowserGuardFacts) -> None:
        if self.executions is not None:
            assert self.request_id is not None and self.claim_token is not None
            request = await self.executions.get(self.request_id, scope=self.profile_scope)
            if (
                request.run_id != self.run_id
                or request.claim_token != self.claim_token
                or request.claim_fence != self.claim_fence
                or request.status
                not in {
                    "running",
                    "awaiting_protected_approval",
                    "awaiting_transaction_approval",
                }
            ):
                raise BrowserExecutionGuardError(
                    "execution claim no longer authorizes browser work"
                )
        else:
            attempt = await self.ledger.get_attempt(
                self.attempt_id,
                scope=self.profile_scope,
            )
            if (
                attempt.run_id != self.run_id
                or attempt.execution_request_id is not None
                or attempt.claim_fence != self.claim_fence
                or attempt.status != "running"
            ):
                raise BrowserExecutionGuardError("named job no longer authorizes browser work")
        if facts.tool_name not in self.browser_scope.allowed_tools:
            raise BrowserExecutionGuardError("browser tool is outside the execution contract")
        if facts.headless is False:
            raise BrowserExecutionGuardError("background browser resources must be headless")
        if facts.session_mode in {"attached_cdp", "attached_selected_tab"}:
            raise BrowserExecutionGuardError("CDP browser attachment is unavailable unattended")
        if facts.provider is not None and facts.provider != self.provider:
            raise BrowserExecutionGuardError("browser screenshot provider differs from contract")
        if facts.tool_name == "browser_visual_snapshot" and (
            not self.browser_scope.allow_masked_visual_observations
        ):
            raise BrowserExecutionGuardError("masked visual disclosure was not authorized")
        if self.resource is not None:
            if facts.resource is not None and facts.resource != self.resource:
                raise BrowserExecutionGuardError("live browser resource differs from contract")
            pin = next(
                (item for item in self.browser_scope.resources if item.resource == self.resource),
                None,
            )
            if (
                pin is None
                or pin.configuration_digest != self.resource_configuration_digest
                or (
                    facts.resource is not None
                    and facts.resource_configuration_digest != pin.configuration_digest
                )
                or facts.session_mode not in {None, "owned_persistent"}
            ):
                raise BrowserExecutionGuardError("configured browser resource revision drifted")
        else:
            if not self.browser_scope.allow_ephemeral or facts.session_mode not in {
                None,
                "owned_ephemeral",
            }:
                raise BrowserExecutionGuardError("ephemeral browser use was not authorized")
            if facts.resource_configuration_digest is not None:
                raise BrowserExecutionGuardError(
                    "ephemeral browser facts cannot carry a configured resource revision"
                )
            if facts.resource is not None and (
                facts.session_id is None
                or facts.resource.profile not in self.profile_scope.profiles
                or facts.resource.name != facts.session_id
            ):
                raise BrowserExecutionGuardError(
                    "ephemeral browser resource is not owned by this execution session"
                )
        if facts.controlled_page_count > self.browser_scope.budget.controlled_pages:
            raise BrowserExecutionGuardError("controlled browser page ceiling exceeded")
        self._check_mode(facts)
        self._check_origins(facts)
        self._check_inputs(facts)

    async def reserve(
        self,
        kind: BrowserBudgetKind,
        amount: int,
        facts: BrowserGuardFacts,
    ) -> None:
        await self.check(facts)
        if kind == "created_pages":
            raise BrowserExecutionGuardError(
                "popup-capable work must use a settleable created-page reservation"
            )
        if kind == "transaction_commits":
            if self.executions is None or self.request_id is None:
                raise BrowserExecutionGuardError("named jobs cannot authorize browser transactions")
            approvals = await self.executions.browser_approvals_for_request(
                self.request_id,
                scope=self.profile_scope,
            )
            consumed = next(
                (
                    item
                    for item in reversed(approvals)
                    if isinstance(item, ParkedBrowserTransaction) and item.state == "consumed"
                ),
                None,
            )
            if (
                consumed is None
                or facts.transaction is None
                or (consumed.envelope_digest != facts.transaction.envelope_sha256)
            ):
                raise BrowserExecutionGuardError(
                    "transaction commit lacks its exact consumed durable approval"
                )
        await self.ledger.reserve_budget(
            self.attempt_id,
            kind,
            amount=amount,
            scope=self.profile_scope,
            owner_token=self.owner_token,
            claim_fence=self.claim_fence,
        )

    async def reserve_possible_pages(
        self,
        maximum_creation_count: int,
        facts: BrowserGuardFacts,
    ) -> None:
        """Check worst-case capacity before work without overcharging known outcomes."""

        await self.check(facts)
        if (
            facts.controlled_page_count + maximum_creation_count
            > self.browser_scope.budget.controlled_pages
        ):
            raise BrowserExecutionGuardError(
                "browser action could exceed the simultaneous controlled-page ceiling"
            )
        facts_digest = _facts_digest(facts)
        reservation_key = hashlib.sha256(f"{facts_digest}\0{uuid4().hex}".encode()).hexdigest()
        await self.ledger.reserve_budget(
            self.attempt_id,
            "controlled_pages",
            amount=maximum_creation_count,
            scope=self.profile_scope,
            owner_token=self.owner_token,
            claim_fence=self.claim_fence,
        )
        try:
            reservation = await self.ledger.reserve_possible_pages(
                self.attempt_id,
                maximum=maximum_creation_count,
                reservation_key=reservation_key,
                scope=self.profile_scope,
                owner_token=self.owner_token,
                claim_fence=self.claim_fence,
            )
        except BaseException:
            await self.ledger.release_live_budget(
                self.attempt_id,
                "controlled_pages",
                amount=maximum_creation_count,
                scope=self.profile_scope,
                owner_token=self.owner_token,
                claim_fence=self.claim_fence,
            )
            raise
        async with self._possible_page_lock:
            self._possible_page_reservations.setdefault(facts_digest, []).append(reservation.id)

    async def release_controlled_pages(
        self,
        amount: int,
        facts: BrowserGuardFacts,
    ) -> None:
        """Release only pages whose closure or non-ownership is confirmed by the browser."""

        del facts
        if amount < 1:
            raise ValueError("controlled-page release amount must be positive")
        await self.ledger.release_live_budget(
            self.attempt_id,
            "controlled_pages",
            amount=amount,
            scope=self.profile_scope,
            owner_token=self.owner_token,
            claim_fence=self.claim_fence,
        )

    async def record(self, evidence: BrowserRuntimeEvidence) -> None:
        facts = evidence.facts
        occurrence_digest = _facts_digest(facts)
        reservation_id: str | None = None
        async with self._possible_page_lock:
            pending = self._possible_page_reservations.get(occurrence_digest)
            if pending:
                reservation_id = pending.pop(0)
                if not pending:
                    del self._possible_page_reservations[occurrence_digest]
        binding = _binding(facts, occurrence_digest)
        reason = evidence.failure.code if evidence.failure is not None else None
        postcondition = (
            evidence.failure.message
            if evidence.failure is not None
            else (
                f"browser runtime reported {evidence.disposition}; "
                f"bytes={evidence.result_byte_count}; pages={evidence.created_page_count}"
            )
        )
        if evidence.action_id is not None:
            postcondition = f"{postcondition}; browser_action_id={evidence.action_id}"
        disposition = "observed" if evidence.disposition == "completed" else evidence.disposition
        bound = self._bound_effect.get()
        action_evidence = BrowserActionEvidence(
            attempt_id=self.attempt_id,
            action_id=(bound[0] if bound is not None and disposition != "observed" else None),
            operation=facts.tool_name,
            logical_effect_key=(
                bound[1]
                if bound is not None and disposition != "observed"
                else occurrence_digest
                if disposition != "observed"
                else None
            ),
            live_occurrence_digest=occurrence_digest,
            disposition=disposition,
            attempt_reason=reason,
            binding=binding,
            postcondition=postcondition[:2_000],
            created_at=datetime.now(UTC),
        )
        if bound is not None and disposition != "observed":
            async with self._pending_effect_lock:
                self._pending_effect_evidence.setdefault(bound[0], []).append(action_evidence)
        else:
            await self.ledger.record_action_evidence(
                action_evidence,
                scope=self.profile_scope,
                owner_token=self.owner_token,
                claim_fence=self.claim_fence,
            )
        if reservation_id is not None:
            await self.ledger.settle_possible_pages(
                reservation_id,
                consumed=evidence.created_page_count,
                in_doubt=evidence.disposition == "in_doubt",
                scope=self.profile_scope,
                owner_token=self.owner_token,
                claim_fence=self.claim_fence,
            )
        if (
            evidence.disposition == "completed"
            and facts.navigation_generation is not None
            and facts.top_level_origin is not None
        ):
            await self.ledger.checkpoint_navigation(
                BrowserNavigationCheckpoint(
                    attempt_id=self.attempt_id,
                    page_generation=facts.navigation_generation,
                    top_level_origin=facts.top_level_origin,
                    url_projection=facts.top_level_origin,
                    created_at=datetime.now(UTC),
                ),
                scope=self.profile_scope,
                owner_token=self.owner_token,
                claim_fence=self.claim_fence,
            )

    def _check_mode(self, facts: BrowserGuardFacts) -> None:
        mutation_tools = {
            "browser_click",
            "browser_fill",
            "browser_fill_protected",
            "browser_select",
            "browser_set_checked",
            "browser_press_key",
            "browser_commit",
            "browser_upload",
            "browser_download",
            "browser_coordinate_click",
            "browser_coordinate_commit",
        }
        if self.browser_scope.mode == "read_only" and facts.tool_name in mutation_tools:
            raise BrowserExecutionGuardError("named/read-only execution cannot mutate a browser")
        if facts.tool_name in {"browser_commit", "browser_coordinate_commit"} and (
            self.browser_scope.mode != "transaction" or facts.transaction is None
        ):
            raise BrowserExecutionGuardError(
                "browser commit requires transaction mode and exact envelope evidence"
            )
        if facts.tool_name == "browser_coordinate_commit" and facts.coordinate_fallback is None:
            raise BrowserExecutionGuardError("coordinate commit lacks semantic fallback evidence")

    def _check_origins(self, facts: BrowserGuardFacts) -> None:
        origins = tuple(
            item
            for item in (
                facts.top_level_origin,
                facts.target_frame_origin,
                *facts.effective_destination_origins,
            )
            if item is not None
        )
        if not origins:
            return
        private_origins = set(facts.private_destination_origins)
        pin = (
            next(
                (item for item in self.browser_scope.resources if item.resource == self.resource),
                None,
            )
            if self.resource is not None
            else None
        )
        for origin in origins:
            if not _is_https_origin(origin):
                raise BrowserExecutionGuardError("background browser destinations require HTTPS")
            private = _is_private_origin(origin) or origin in private_origins
            if private and origin not in self.browser_scope.private_origin_ceiling:
                raise BrowserExecutionGuardError(
                    "private browser destination is outside the execution ceiling"
                )
            if (
                self.resource is None
                and not private
                and not self.browser_scope.allow_public_https_research
            ):
                raise BrowserExecutionGuardError("public HTTPS research was not authorized")
            if (
                pin is not None
                and pin.authenticated_origin_ceiling
                and (origin not in pin.authenticated_origin_ceiling)
            ):
                raise BrowserExecutionGuardError(
                    "authenticated browser origin is outside the resource ceiling"
                )

    def _check_inputs(self, facts: BrowserGuardFacts) -> None:
        if facts.tool_name == "browser_upload":
            if (
                facts.attachment_count < 1
                or len(facts.attachment_ids) != facts.attachment_count
                or len(facts.attachment_sha256) != facts.attachment_count
                or len(set(facts.attachment_ids)) != facts.attachment_count
            ):
                raise BrowserExecutionGuardError(
                    "browser upload lacks exact unique attachment identities and digests"
                )
            pins = {item.id: item for item in self.browser_scope.attachments}
            selected = []
            for attachment_id, digest in zip(
                facts.attachment_ids,
                facts.attachment_sha256,
                strict=True,
            ):
                pin = pins.get(attachment_id)
                if pin is None or pin.sha256 != digest:
                    raise BrowserExecutionGuardError(
                        "browser upload attachment differs from its exact contract pin"
                    )
                selected.append(pin)
            if facts.byte_count != sum(item.byte_count for item in selected):
                raise BrowserExecutionGuardError(
                    "browser upload bytes differ from the exact attachment pins"
                )
        elif facts.attachment_count or facts.attachment_ids or facts.attachment_sha256:
            raise BrowserExecutionGuardError("browser attachment facts belong only to an upload")
        if (
            facts.tool_name == "browser_upload"
            and facts.byte_count > self.browser_scope.budget.upload_bytes
        ):
            raise BrowserExecutionGuardError("browser byte count exceeds upload ceiling")
        if (
            facts.tool_name == "browser_download"
            and facts.byte_count > self.browser_scope.budget.download_bytes
        ):
            raise BrowserExecutionGuardError("browser byte count exceeds download ceiling")
        if facts.protected_resource is not None:
            pin = next(
                (
                    item
                    for item in self.browser_scope.protected_resources
                    if item.resource == facts.protected_resource
                ),
                None,
            )
            if pin is None or facts.protected_field not in pin.fields:
                raise BrowserExecutionGuardError(
                    "protected browser field is outside the execution contract"
                )
            if facts.protected_revision is not None and facts.protected_revision != pin.revision:
                raise BrowserExecutionGuardError(
                    "protected browser resource revision differs from the execution contract"
                )


def _facts_digest(facts: BrowserGuardFacts) -> str:
    encoded = json.dumps(
        facts.model_dump(mode="json"),
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _binding(facts: BrowserGuardFacts, occurrence_digest: str) -> BrowserLiveBinding | None:
    if (
        facts.navigation_generation is None
        or facts.top_level_origin is None
        or facts.target_frame_origin is None
    ):
        return None
    snapshot_digest = hashlib.sha256((facts.snapshot_id or "none").encode()).hexdigest()
    target_digest = hashlib.sha256((facts.target_ref or "none").encode()).hexdigest()
    return BrowserLiveBinding(
        occurrence_digest=occurrence_digest,
        page_generation=facts.navigation_generation,
        snapshot_digest=snapshot_digest,
        target_digest=target_digest,
        target_description=(f"{facts.action_kind or facts.tool_name} target {target_digest[:12]}"),
        top_level_origin=facts.top_level_origin,
        target_frame_origin=facts.target_frame_origin,
        destination_projections=tuple(sorted(facts.effective_destination_origins)),
    )


def _is_https_origin(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _is_private_origin(value: str) -> bool:
    host = urlsplit(value).hostname
    if host is None:
        return True
    normalized = host.rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return False
    return not address.is_global
