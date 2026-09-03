"""Focused regressions for protected-value policy and catalog review findings."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast

import pytest
from pydantic import SecretStr

from ricky.config import ProtectedValuesSettings, RickySettings
from ricky.profiles import ProfileResourceRef, ProfileScope
from ricky.protected_values import (
    ProtectedDestinationPolicy,
    ProtectedFieldDescriptor,
    ProtectedUseRequest,
    ProtectedValueBroker,
    ProtectedValuesCatalogParams,
    ProtectedValuesCatalogTool,
    ProtectedValueStoreError,
)
from ricky.protected_values.types import ProtectedValueKind
from ricky.tools import ToolContext

_PASSPHRASE = SecretStr("review-regression-passphrase")
_PUBLIC_ADDRESS = "93.184.216.34"


def _settings(tmp_path: Path, *, catalog_limit: int = 100) -> RickySettings:
    return RickySettings(
        user_data_dir=str(tmp_path / "user"),
        project_data_dir=str(tmp_path / "project" / ".ricky"),
        protected_values=ProtectedValuesSettings(
            enabled=True,
            catalog_limit=catalog_limit,
            argon2_iterations=1,
            argon2_lanes=1,
            argon2_memory_kib=8_192,
        ),
    )


def _field() -> tuple[ProtectedFieldDescriptor, ...]:
    return (
        ProtectedFieldDescriptor(
            name="secret",
            label="Secret",
            mode="stored",
            compatible_controls=("generic_secret",),
        ),
    )


async def _create(
    broker: ProtectedValueBroker,
    *,
    profile: str,
    name: str,
    kind: ProtectedValueKind,
    policy: ProtectedDestinationPolicy | None = None,
) -> ProfileResourceRef:
    descriptor = await broker.create(
        profile=profile,
        name=name,
        kind=kind,
        label=name,
        description="",
        fields=_field(),
        policy=policy or ProtectedDestinationPolicy(mode="strict"),
        values={"secret": SecretStr(f"value-for-{name}")},
    )
    return descriptor.ref


async def _secure_web_broker(
    tmp_path: Path,
    resolver: Callable[[str, int], Awaitable[tuple[str, ...]]],
    *,
    resolution_timeout_seconds: float = 10.0,
) -> tuple[ProtectedValueBroker, ProfileResourceRef]:
    broker = ProtectedValueBroker(
        _settings(tmp_path),
        scope=ProfileScope.create("personal"),
        consumer_ids=frozenset({"browser.fill"}),
        host_resolver=resolver,
        resolution_timeout_seconds=resolution_timeout_seconds,
    )
    await broker.initialize("personal", _PASSPHRASE)
    await broker.unlock("personal", _PASSPHRASE)
    ref = await _create(
        broker,
        profile="personal",
        name="secure-web-secret",
        kind="generic",
        policy=ProtectedDestinationPolicy(mode="secure_web"),
    )
    return broker, ref


def _request(ref: ProfileResourceRef) -> ProtectedUseRequest:
    return ProtectedUseRequest(
        ref=ref,
        field="secret",
        consumer_id="browser.fill",
        control_kind="generic_secret",
        top_level_origin="https://secure-top.example",
        frame_origin="https://secure-frame.example",
        occurrence="session/page/snapshot/ref",
    )


@pytest.mark.asyncio
async def test_secure_web_resolves_both_origins_and_revalidates_dns(
    tmp_path: Path,
) -> None:
    answers = {
        "secure-top.example": (_PUBLIC_ADDRESS,),
        "secure-frame.example": (_PUBLIC_ADDRESS,),
    }
    calls: list[tuple[str, int]] = []

    async def resolve(host: str, port: int) -> tuple[str, ...]:
        calls.append((host, port))
        return answers[host]

    broker, ref = await _secure_web_broker(tmp_path, resolve)
    material = await broker.prepare(_request(ref))

    assert calls == [
        ("secure-top.example", 443),
        ("secure-frame.example", 443),
    ]
    answers["secure-frame.example"] = ("127.0.0.1",)
    with pytest.raises(ProtectedValueStoreError, match="public HTTPS origin"):
        await broker.revalidate(material)
    assert calls[-2:] == [
        ("secure-top.example", 443),
        ("secure-frame.example", 443),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        (_PUBLIC_ADDRESS, "10.0.0.1"),
        ("224.0.0.1",),
    ],
)
async def test_secure_web_rejects_any_private_resolved_address(
    tmp_path: Path,
    addresses: tuple[str, ...],
) -> None:
    async def resolve(_host: str, _port: int) -> tuple[str, ...]:
        return addresses

    broker, ref = await _secure_web_broker(tmp_path, resolve)
    with pytest.raises(ProtectedValueStoreError, match="public HTTPS origin"):
        await broker.prepare(_request(ref))
    assert await broker.uses("personal") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["empty", "error", "timeout"])
async def test_secure_web_resolution_failures_are_closed(
    tmp_path: Path,
    failure: str,
) -> None:
    async def resolve(_host: str, _port: int) -> tuple[str, ...]:
        if failure == "empty":
            return ()
        if failure == "error":
            raise OSError("synthetic resolver failure")
        await asyncio.Event().wait()
        raise AssertionError("cancelled resolver unexpectedly resumed")

    broker, ref = await _secure_web_broker(
        tmp_path,
        resolve,
        resolution_timeout_seconds=0.01,
    )
    expected = {
        "empty": "did not resolve",
        "error": "could not be resolved",
        "timeout": "timed out",
    }[failure]
    with pytest.raises(ProtectedValueStoreError, match=expected):
        await broker.prepare(_request(ref))


@pytest.mark.asyncio
async def test_catalog_filters_each_scoped_store_before_global_limit(
    tmp_path: Path,
) -> None:
    broker = ProtectedValueBroker(
        _settings(tmp_path, catalog_limit=2),
        scope=ProfileScope.create("personal"),
    )
    for profile in ("shared", "personal"):
        await broker.initialize(profile, _PASSPHRASE)
        await broker.unlock(profile, _PASSPHRASE)
        await _create(
            broker,
            profile=profile,
            name="a-generic",
            kind="generic",
        )
        await _create(
            broker,
            profile=profile,
            name="z-credential",
            kind="credential",
        )

    selected = await broker.catalog(kind="credential", limit=1)

    assert [item.ref.qualified for item in selected] == ["personal/z-credential"]


@pytest.mark.asyncio
async def test_catalog_tool_omitted_limit_defers_to_broker_configuration(
    tmp_path: Path,
) -> None:
    broker = ProtectedValueBroker(
        _settings(tmp_path, catalog_limit=1),
        scope=ProfileScope.create("personal"),
    )
    params = ProtectedValuesCatalogParams()

    result = await ProtectedValuesCatalogTool(broker).run(
        params,
        cast(ToolContext, None),
    )

    assert params.limit is None
    assert result.data == []


@pytest.mark.asyncio
async def test_broker_status_counts_resources_beyond_catalog_limit(
    tmp_path: Path,
) -> None:
    broker = ProtectedValueBroker(
        _settings(tmp_path, catalog_limit=1),
        scope=ProfileScope.create("personal"),
    )
    await broker.initialize("personal", _PASSPHRASE)
    await broker.unlock("personal", _PASSPHRASE)
    for index in range(3):
        await _create(
            broker,
            profile="personal",
            name=f"secret-{index}",
            kind="generic",
        )

    status = await broker.status("personal")

    assert status.resource_count == 3
