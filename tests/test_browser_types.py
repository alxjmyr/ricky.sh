"""Strict browser boundary model tests."""

from __future__ import annotations

from typing import Any, Literal

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from ricky.attachments import BrowserDownloadRef
from ricky.browser.types import (
    BrowserActionContext,
    BrowserActionEvidence,
    BrowserActionRequest,
    BrowserActionResult,
    BrowserActionTarget,
    BrowserBoundingBox,
    BrowserCommitEnvelope,
    BrowserCoordinateContext,
    BrowserCoordinateTarget,
    BrowserDialogObservation,
    BrowserDialogPolicy,
    BrowserDownloadResult,
    BrowserFailure,
    BrowserFinancialComponent,
    BrowserFinancialTransactionEnvelope,
    BrowserFundingSource,
    BrowserHandoff,
    BrowserInstallStatus,
    BrowserMoney,
    BrowserNavigation,
    BrowserPage,
    BrowserPageChanges,
    BrowserPageList,
    BrowserPostcondition,
    BrowserProtectedValueFundingSource,
    BrowserRecurrence,
    BrowserScroll,
    BrowserSession,
    BrowserSessionClosed,
    BrowserSiteFundingSource,
    BrowserSnapshot,
    BrowserTarget,
    BrowserTargetDescriptor,
    BrowserTransactionEnvelope,
    BrowserTransactionEvidence,
    BrowserViewport,
    BrowserVisualCandidate,
    BrowserVisualSnapshot,
)
from ricky.llm import MediaArtifactRef
from ricky.profiles import ProfileLabel, ProfileResourceRef

SESSION_ID = "browser_session_" + "a" * 32
PAGE_ID = "browser_page_" + "b" * 32
SNAPSHOT_ID = "browser_snapshot_" + "c" * 32
ACTION_ID = "browser_action_" + "d" * 32


def _browser_envelope() -> BrowserTransactionEnvelope:
    return BrowserTransactionEnvelope(
        kind="browser",
        intent="Submit the completed volunteer application",
        destination="Example Community Food Bank",
        consequences=("Creates an application for review",),
        disclosures=("Contact details", "Attached résumé"),
        expected_result="The site displays an application reference",
    )


def _financial_envelope(
    *,
    timing: Literal["one_time", "recurring"] = "one_time",
    recurrence: BrowserRecurrence | None = None,
) -> BrowserFinancialTransactionEnvelope:
    return BrowserFinancialTransactionEnvelope(
        kind="financial",
        intent="Purchase one synthetic acceptance-test ticket",
        payee="Example Events",
        total=BrowserMoney(amount="19.50", currency="USD"),
        components=(
            BrowserFinancialComponent(
                label="Ticket",
                amount=BrowserMoney(amount="18", currency="USD"),
            ),
        ),
        fees=(
            BrowserFinancialComponent(
                label="Booking fee",
                amount=BrowserMoney(amount="1.50", currency="USD"),
            ),
        ),
        timing=timing,
        recurrence=recurrence,
        source=BrowserProtectedValueFundingSource(
            kind="protected_value",
            protected_value=ProfileResourceRef(profile="personal", name="test-card"),
        ),
        consequences=("The purchase cannot be cancelled",),
        expected_result="The site displays an order reference",
    )


def _page() -> BrowserPage:
    return BrowserPage(
        session_id=SESSION_ID,
        page_id=PAGE_ID,
        selected=True,
        url="https://example.com/path?key=present",
        origin="https://example.com",
        title="Example",
        navigation_generation=2,
    )


def _models() -> tuple[BaseModel, ...]:
    page = _page()
    target = BrowserTarget(
        ref="e12",
        session_id=SESSION_ID,
        page_id=PAGE_ID,
        navigation_generation=2,
        snapshot_id=SNAPSHOT_ID,
    )
    descriptor = BrowserTargetDescriptor(
        ref="e12",
        role="textbox",
        name="Email",
        control_kind="email",
        frame_origin="https://example.com",
        editable=True,
    )
    action_target = BrowserActionTarget(
        session_id=SESSION_ID,
        page_id=PAGE_ID,
        snapshot_id=SNAPSHOT_ID,
        ref="e12",
    )
    snapshot = BrowserSnapshot(
        snapshot_id=SNAPSHOT_ID,
        page=page,
        content='- textbox "Email" [ref=e12]',
        targets=(target,),
        descriptors=(descriptor,),
        depth_limit=20,
        character_limit=20_000,
        character_truncated=False,
    )
    dialog = BrowserDialogObservation(
        kind="confirm",
        message="Continue?",
        response="accepted",
        matched_policy=True,
    )
    postcondition = BrowserPostcondition(
        navigation_occurred=True,
        page_changes=BrowserPageChanges(selected_popup_page_id=PAGE_ID),
    )
    visual_target = BrowserTarget(
        ref="d1",
        session_id=SESSION_ID,
        page_id=PAGE_ID,
        navigation_generation=2,
        snapshot_id=SNAPSHOT_ID,
    )
    visual_descriptor = BrowserTargetDescriptor(
        ref="d1",
        role="canvas",
        name="Visual control",
    )
    visual_candidate = BrowserVisualCandidate(
        number=1,
        target=visual_target,
        descriptor=visual_descriptor,
        bounding_box=BrowserBoundingBox(x=10, y=20, width=30, height=40),
    )
    viewport = BrowserViewport(
        width=800,
        height=600,
        scroll_x=0,
        scroll_y=10,
        device_scale_factor=1,
        image_scale=1,
    )
    coordinate_target = BrowserCoordinateTarget(
        session_id=SESSION_ID,
        page_id=PAGE_ID,
        screenshot_id=SNAPSHOT_ID,
        x=25.25,
        y=30.75,
    )
    download = BrowserDownloadRef(
        id="browser_download_" + "e" * 32,
        profile="personal",
        filename="report.txt",
        media_type="text/plain",
        size_bytes=4,
        sha256="f" * 64,
    )
    browser_envelope = _browser_envelope()
    financial_envelope = _financial_envelope()
    recurrence = BrowserRecurrence(
        amount=BrowserMoney(amount="12.99", currency="USD"),
        cadence="monthly",
        start_date="2026-09-01",
        open_ended=True,
        cancellation="Cancel in account settings before the next renewal",
    )
    transaction = BrowserTransactionEvidence(
        envelope_kind="financial",
        envelope_sha256="4" * 64,
        top_level_origin="https://example.com",
        target_frame_origin="https://pay.example.com",
        effective_destinations=("https://example.com/checkout/submit",),
    )
    return (
        BrowserFailure(code="backend_error", message="safe failure"),
        page,
        BrowserSession(
            session_id=SESSION_ID,
            resource=ProfileResourceRef(profile="personal", name=SESSION_ID),
            headless=True,
            selected_page_id=PAGE_ID,
            pages=(page,),
        ),
        BrowserSessionClosed(session_id=SESSION_ID),
        BrowserPageList(session_id=SESSION_ID, selected_page_id=PAGE_ID, pages=(page,)),
        BrowserNavigation(page=page),
        BrowserScroll(page=page, direction="down", amount=700),
        target,
        descriptor,
        action_target,
        BrowserDialogPolicy(response="accept", prompt_text="safe answer"),
        BrowserActionRequest(kind="fill", value="ordinary model input"),
        dialog,
        BrowserPageChanges(selected_popup_page_id=PAGE_ID),
        postcondition,
        BrowserActionContext(
            target=action_target,
            resource=ProfileResourceRef(profile="personal", name=SESSION_ID),
            navigation_generation=2,
            url="https://example.com/path?key=present",
            origin="https://example.com",
            descriptor=descriptor,
            headless=False,
        ),
        snapshot,
        BrowserActionEvidence(
            action_id=ACTION_ID,
            kind="fill",
            disposition="performed",
        ),
        BrowserActionResult(
            action_id=ACTION_ID,
            kind="fill",
            disposition="performed",
            page=page,
            snapshot=snapshot,
            dialogs=(dialog,),
            postcondition=postcondition,
        ),
        BrowserMoney(amount="19.50", currency="USD"),
        BrowserFinancialComponent(
            label="Booking fee",
            amount=BrowserMoney(amount="1.50", currency="USD"),
        ),
        recurrence,
        BrowserProtectedValueFundingSource(
            kind="protected_value",
            protected_value=ProfileResourceRef(profile="personal", name="test-card"),
        ),
        BrowserSiteFundingSource(kind="site", label="Saved Visa ending in masked digits"),
        browser_envelope,
        financial_envelope,
        transaction,
        BrowserActionEvidence(
            action_id=ACTION_ID,
            kind="commit",
            disposition="performed",
            transaction=transaction,
        ),
        BrowserActionResult(
            action_id=ACTION_ID,
            kind="commit",
            disposition="performed",
            page=page,
            transaction=transaction,
        ),
        viewport,
        visual_candidate,
        BrowserVisualSnapshot(
            snapshot_id=SNAPSHOT_ID,
            page=page,
            image=MediaArtifactRef(
                id="media_" + "1" * 32,
                byte_count=100,
                sha256="2" * 64,
                width=800,
                height=600,
                source_label=ProfileLabel.owned_by("personal"),
            ),
            width=800,
            height=600,
            viewport=viewport,
            candidates=(visual_candidate,),
            masked_base_sha256="3" * 64,
        ),
        coordinate_target,
        BrowserCoordinateContext(
            target=coordinate_target,
            resource=ProfileResourceRef(profile="personal", name=SESSION_ID),
            navigation_generation=2,
            url="https://example.com/path?key=present",
            origin="https://example.com",
            image_width=800,
            image_height=600,
            css_x=25.25,
            css_y=30.75,
            masked_base_sha256="3" * 64,
        ),
        download,
        BrowserDownloadResult(
            action_id=ACTION_ID,
            disposition="performed",
            page=page,
            download=download,
        ),
        BrowserHandoff(
            session_id=SESSION_ID,
            page_id=PAGE_ID,
            reason="captcha",
            prompt="Complete the CAPTCHA in the headed browser, then reply when ready.",
        ),
        BrowserInstallStatus(
            enabled=True,
            ready=True,
            install_dir="/safe/browser/root",
            executable="/safe/browser/root/chromium",
        ),
    )


@pytest.mark.parametrize("model", _models(), ids=lambda model: type(model).__name__)
def test_boundary_models_are_frozen_strict_and_json_round_trip(model: BaseModel) -> None:
    restored = type(model).model_validate_json(model.model_dump_json())

    assert restored == model
    with pytest.raises(ValidationError, match="frozen"):
        model.__setattr__(next(iter(type(model).model_fields)), "changed")


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (BrowserFailure, {"code": "backend_error", "message": "safe", "extra": True}),
        (
            BrowserPage,
            {
                "session_id": SESSION_ID,
                "page_id": PAGE_ID,
                "selected": True,
                "url": "https://example.com/",
                "title": "",
                "navigation_generation": "2",
            },
        ),
        (
            BrowserTarget,
            {
                "ref": "css=#unsafe",
                "session_id": SESSION_ID,
                "page_id": PAGE_ID,
                "navigation_generation": 0,
                "snapshot_id": SNAPSHOT_ID,
            },
        ),
    ],
)
def test_boundary_models_reject_extra_coercion_and_non_opaque_references(
    model: type[BaseModel], values: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(values)


def test_snapshot_target_is_bound_to_session_page_generation_and_snapshot() -> None:
    target = BrowserTarget(
        ref="e1",
        session_id=SESSION_ID,
        page_id=PAGE_ID,
        navigation_generation=7,
        snapshot_id=SNAPSHOT_ID,
    )

    assert target.model_dump(mode="json") == {
        "ref": "e1",
        "session_id": SESSION_ID,
        "page_id": PAGE_ID,
        "navigation_generation": 7,
        "snapshot_id": SNAPSHOT_ID,
    }


def test_action_refs_accept_playwright_frame_prefixes_but_not_selectors() -> None:
    target = BrowserActionTarget(
        session_id=SESSION_ID,
        page_id=PAGE_ID,
        snapshot_id=SNAPSHOT_ID,
        ref="f12e34",
    )

    assert target.ref == "f12e34"
    with pytest.raises(ValidationError):
        BrowserActionTarget(
            session_id=SESSION_ID,
            page_id=PAGE_ID,
            snapshot_id=SNAPSHOT_ID,
            ref="aria-ref=f12e34",
        )


@pytest.mark.parametrize(
    ("action_request", "payload_field"),
    [
        (BrowserActionRequest(kind="click"), None),
        (BrowserActionRequest(kind="fill", value=""), "value"),
        (BrowserActionRequest(kind="select", option_label="Large"), "option_label"),
        (BrowserActionRequest(kind="set_checked", checked=False), "checked"),
        (BrowserActionRequest(kind="press_key", key="Tab"), "key"),
        (BrowserActionRequest(kind="commit", activation="enter"), "activation"),
        (BrowserActionRequest(kind="upload"), None),
        (BrowserActionRequest(kind="download"), None),
        (BrowserActionRequest(kind="coordinate_commit"), None),
    ],
)
def test_action_requests_have_one_specialized_payload(
    action_request: BrowserActionRequest,
    payload_field: str | None,
) -> None:
    present = {
        name
        for name in ("value", "option_label", "checked", "key", "activation")
        if getattr(action_request, name) is not None
    }

    assert present == ({payload_field} if payload_field is not None else set())


@pytest.mark.parametrize(
    "values",
    [
        {"kind": "fill"},
        {"kind": "click", "value": "unexpected"},
        {"kind": "select", "option_label": "Large", "checked": True},
        {"kind": "set_checked"},
        {"kind": "press_key", "key": "Enter"},
        {"kind": "commit", "activation": "click", "value": "secret"},
        {
            "kind": "click",
            "dialog": {"response": "accept"},
        },
    ],
)
def test_action_request_rejects_missing_extra_or_unbounded_payload(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BrowserActionRequest.model_validate(values)


def test_dialog_policy_rejects_prompt_text_on_dismiss() -> None:
    with pytest.raises(ValidationError, match="prompt_text requires"):
        BrowserDialogPolicy(response="dismiss", prompt_text="must not be sent")


def test_action_target_does_not_accept_model_supplied_generation_or_selector() -> None:
    values = {
        "session_id": SESSION_ID,
        "page_id": PAGE_ID,
        "snapshot_id": SNAPSHOT_ID,
        "ref": "e1",
    }

    with pytest.raises(ValidationError):
        BrowserActionTarget.model_validate({**values, "navigation_generation": 7})
    with pytest.raises(ValidationError):
        BrowserActionTarget.model_validate({**values, "selector": "#submit"})


def test_provider_target_descriptor_has_only_bounded_safe_selection_facts() -> None:
    schema_fields = set(BrowserTargetDescriptor.model_fields)

    assert schema_fields == {
        "ref",
        "role",
        "name",
        "control_kind",
        "frame_origin",
        "checked",
        "disabled",
        "editable",
        "option_labels",
        "consequential",
        "protected",
        "protected_kind",
        "file",
        "multiple",
        "accept",
    }
    assert not {"selector", "value", "href", "frame_key"} & schema_fields


def test_snapshot_descriptors_remain_optional_for_phase_one_callers() -> None:
    snapshot = BrowserSnapshot(
        snapshot_id=SNAPSHOT_ID,
        page=_page(),
        content="- document",
        targets=(),
        depth_limit=20,
        character_limit=20_000,
        character_truncated=False,
    )

    assert snapshot.descriptors == ()
    assert snapshot.page.latest_action is None


def test_commit_envelope_union_has_exactly_two_discriminated_variants() -> None:
    adapter = TypeAdapter(BrowserCommitEnvelope)
    schema = adapter.json_schema()

    assert schema["discriminator"]["propertyName"] == "kind"
    assert set(schema["discriminator"]["mapping"]) == {"browser", "financial"}
    assert "kind" in schema["$defs"]["BrowserTransactionEnvelope"]["required"]
    assert "kind" in schema["$defs"]["BrowserFinancialTransactionEnvelope"]["required"]
    source_schema = TypeAdapter(BrowserFundingSource).json_schema()
    assert "kind" in source_schema["$defs"]["BrowserProtectedValueFundingSource"]["required"]
    assert "kind" in source_schema["$defs"]["BrowserSiteFundingSource"]["required"]
    assert adapter.validate_json(_browser_envelope().model_dump_json()) == _browser_envelope()
    financial = _financial_envelope()
    assert adapter.validate_json(financial.model_dump_json()) == financial

    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "intent": "Missing the discriminator",
                "destination": "Example",
                "consequences": ("Submits a form",),
                "disclosures": (),
                "expected_result": "Confirmation",
            }
        )
    with pytest.raises(ValidationError):
        adapter.validate_python({"kind": "reservation"})


@pytest.mark.parametrize(
    "amount",
    ["0", "1", "1.23", "0.000001", "999999999999999999"],
)
def test_money_accepts_canonical_nonnegative_bounded_amounts(amount: str) -> None:
    money = BrowserMoney(amount=amount, currency="USD")

    assert BrowserMoney.model_validate_json(money.model_dump_json()) == money


@pytest.mark.parametrize(
    "amount",
    [
        "-1",
        "+1",
        "01",
        "1.",
        ".5",
        "1e2",
        "0.0000001",
        "1234567890123456789",
        "999999999999999999.1",
    ],
)
def test_money_rejects_noncanonical_or_unbounded_amounts(amount: str) -> None:
    with pytest.raises(ValidationError):
        BrowserMoney(amount=amount, currency="USD")


@pytest.mark.parametrize("currency", ["usd", "US", "USDX", "123", "€UR"])
def test_money_requires_an_uppercase_three_letter_currency(currency: str) -> None:
    with pytest.raises(ValidationError):
        BrowserMoney(amount="1", currency=currency)

    with pytest.raises(ValidationError):
        BrowserMoney.model_validate({"amount": 1, "currency": "USD"})


def test_generic_transaction_requires_material_consequences_and_explicit_disclosures() -> None:
    with pytest.raises(ValidationError):
        BrowserTransactionEnvelope(
            kind="browser",
            intent="Submit",
            destination="Example",
            consequences=(),
            disclosures=(),
            expected_result="Confirmation",
        )
    with pytest.raises(ValidationError):
        BrowserTransactionEnvelope.model_validate(
            {
                "kind": "browser",
                "intent": "Submit",
                "destination": "Example",
                "consequences": ("Creates a record",),
                "expected_result": "Confirmation",
            }
        )
    with pytest.raises(ValidationError):
        BrowserTransactionEnvelope(
            kind="browser",
            intent="Submit",
            destination="Example",
            consequences=("   ",),
            disclosures=(),
            expected_result="Confirmation",
        )


def test_financial_transaction_requires_explicit_fees_and_rejects_extra_fields() -> None:
    values = _financial_envelope().model_dump(mode="python")
    del values["fees"]

    with pytest.raises(ValidationError):
        BrowserFinancialTransactionEnvelope.model_validate(values)
    with pytest.raises(ValidationError):
        BrowserFinancialTransactionEnvelope.model_validate(
            {**_financial_envelope().model_dump(mode="python"), "account_number": "unsafe"}
        )


def test_financial_transaction_requires_one_currency_across_nested_values() -> None:
    with pytest.raises(ValidationError, match="total currency"):
        BrowserFinancialTransactionEnvelope(
            kind="financial",
            intent="Purchase a ticket",
            payee="Example Events",
            total=BrowserMoney(amount="20", currency="USD"),
            components=(
                BrowserFinancialComponent(
                    label="Ticket",
                    amount=BrowserMoney(amount="18", currency="EUR"),
                ),
            ),
            fees=(),
            timing="one_time",
            source=BrowserSiteFundingSource(kind="site", label="Saved card"),
            consequences=("Creates a non-refundable purchase",),
            expected_result="Order reference",
        )


def test_recurring_transaction_requires_coherent_positive_bounded_terms() -> None:
    recurrence = BrowserRecurrence(
        amount=BrowserMoney(amount="12.99", currency="USD"),
        cadence="monthly on the first day",
        start_date="2026-09-01",
        end_date="2027-09-01",
        open_ended=False,
        cancellation="Cancel before the next renewal",
    )
    recurring = _financial_envelope(timing="recurring", recurrence=recurrence)

    assert recurring.recurrence == recurrence
    with pytest.raises(ValidationError, match="recurrence"):
        _financial_envelope(timing="recurring")
    with pytest.raises(ValidationError, match="recurrence"):
        _financial_envelope(recurrence=recurrence)
    with pytest.raises(ValidationError, match="total currency"):
        _financial_envelope(
            timing="recurring",
            recurrence=BrowserRecurrence(
                amount=BrowserMoney(amount="12.99", currency="EUR"),
                cadence="monthly",
                start_date="2026-09-01",
                open_ended=True,
                cancellation="Cancel before renewal",
            ),
        )


@pytest.mark.parametrize(
    "values",
    [
        {
            "amount": BrowserMoney(amount="0", currency="USD"),
            "cadence": "monthly",
            "start_date": "2026-09-01",
            "open_ended": True,
            "cancellation": "Cancel before renewal",
        },
        {
            "amount": BrowserMoney(amount="1", currency="USD"),
            "cadence": "monthly",
            "start_date": "2026-02-30",
            "open_ended": True,
            "cancellation": "Cancel before renewal",
        },
        {
            "amount": BrowserMoney(amount="1", currency="USD"),
            "cadence": "monthly",
            "start_date": "2026-09-01",
            "end_date": "2027-09-01",
            "open_ended": True,
            "cancellation": "Cancel before renewal",
        },
        {
            "amount": BrowserMoney(amount="1", currency="USD"),
            "cadence": "monthly",
            "start_date": "2026-09-01",
            "open_ended": False,
            "cancellation": "Cancel before renewal",
        },
        {
            "amount": BrowserMoney(amount="1", currency="USD"),
            "cadence": "monthly",
            "start_date": "2026-09-01",
            "end_date": "2026-09-01",
            "open_ended": False,
            "cancellation": "Cancel before renewal",
        },
    ],
)
def test_recurrence_rejects_incoherent_amounts_and_dates(values: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        BrowserRecurrence.model_validate(values)


def test_funding_source_union_exposes_only_a_safe_alias_or_site_label() -> None:
    adapter = TypeAdapter(BrowserFundingSource)
    protected = BrowserProtectedValueFundingSource(
        kind="protected_value",
        protected_value=ProfileResourceRef(profile="personal", name="travel-card"),
    )

    assert protected.model_dump(mode="json") == {
        "kind": "protected_value",
        "protected_value": {"profile": "personal", "name": "travel-card"},
    }
    assert adapter.validate_json(protected.model_dump_json()) == protected
    assert set(BrowserProtectedValueFundingSource.model_fields) == {
        "kind",
        "protected_value",
    }
    assert set(BrowserSiteFundingSource.model_fields) == {"kind", "label"}
    with pytest.raises(ValidationError):
        adapter.validate_python(
            {
                "kind": "protected_value",
                "protected_value": {
                    "profile": "personal",
                    "name": "travel-card",
                },
                "value": "must never be representable",
            }
        )


@pytest.mark.parametrize(
    "label",
    [
        "Visa 4242 4242 4242 4242",
        "account number 123456789",
        "IBAN DE89 3704 0044 0532 0130 00",
        "CVV: 123",
        "card security code 1234",
    ],
)
def test_site_funding_source_rejects_unmasked_financial_identifiers(label: str) -> None:
    with pytest.raises(ValidationError, match="unmasked financial identifiers"):
        BrowserSiteFundingSource(kind="site", label=label)


@pytest.mark.parametrize(
    "label",
    [
        "Visa ending in 4242",
        "Visa •••• 4242",
        "Checking account ending in 6789",
    ],
)
def test_site_funding_source_accepts_safe_masked_labels(label: str) -> None:
    assert BrowserSiteFundingSource(kind="site", label=label).label == label


def test_transaction_evidence_is_compact_and_limited_to_commit_actions() -> None:
    evidence = BrowserTransactionEvidence(
        envelope_kind="browser",
        envelope_sha256="a" * 64,
        top_level_origin="https://example.com",
        target_frame_origin="https://example.com",
        effective_destinations=("https://example.com/forms/submit",),
    )
    fields = set(BrowserTransactionEvidence.model_fields)

    assert fields == {
        "envelope_kind",
        "envelope_sha256",
        "top_level_origin",
        "target_frame_origin",
        "effective_destinations",
    }
    assert not {"envelope", "source", "protected_value", "value", "path"} & fields
    assert isinstance(evidence.effective_destinations, tuple)
    round_tripped = BrowserTransactionEvidence.model_validate_json(evidence.model_dump_json())
    assert round_tripped == evidence
    missing_origin = evidence.model_dump(mode="python")
    del missing_origin["target_frame_origin"]
    with pytest.raises(ValidationError):
        BrowserTransactionEvidence.model_validate(missing_origin)
    with pytest.raises(ValidationError):
        BrowserTransactionEvidence.model_validate(
            {**evidence.model_dump(mode="python"), "top_level_origin": None}
        )
    assert (
        BrowserActionEvidence(
            action_id=ACTION_ID,
            kind="coordinate_commit",
            disposition="in_doubt",
            transaction=evidence,
        ).transaction
        == evidence
    )
    with pytest.raises(ValidationError, match="commit action"):
        BrowserActionEvidence(
            action_id=ACTION_ID,
            kind="click",
            disposition="performed",
            transaction=evidence,
        )
    with pytest.raises(ValidationError, match="commit action"):
        BrowserActionResult(
            action_id=ACTION_ID,
            kind="fill",
            disposition="performed",
            page=_page(),
            transaction=evidence,
        )
    with pytest.raises(ValidationError, match="require transaction evidence"):
        BrowserActionEvidence(
            action_id=ACTION_ID,
            kind="commit",
            disposition="not_performed",
        )
    with pytest.raises(ValidationError, match="require transaction evidence"):
        BrowserActionResult(
            action_id=ACTION_ID,
            kind="coordinate_commit",
            disposition="not_performed",
            page=_page(),
        )
