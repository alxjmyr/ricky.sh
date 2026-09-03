"""Stable release resolution and authenticated artifact caching tests."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from ricky.upgrades.models import ReleaseDescriptor
from ricky.upgrades.releases import (
    GitHubReleaseResolver,
    LocalReleaseResolver,
    ReleaseResolutionError,
    cache_release_artifacts,
)
from ricky.upgrades.versions import ReleaseVersion

_DOWNLOAD_ROOT = "https://github.com/alxjmyr/ricky.sh/releases/download"


def _artifact(path: Path, payload: bytes) -> dict[str, object]:
    path.write_bytes(payload)
    return {
        "name": path.name,
        "url": path.as_uri(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


def _local_descriptor(tmp_path: Path, version: str = "0.6.1") -> tuple[Path, dict[str, object]]:
    tmp_path.mkdir(parents=True)
    wheel = tmp_path / f"ricky-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"ricky-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.3\nName: ricky\nVersion: {version}\n",
        )
    constraints = tmp_path / f"ricky-{version}-constraints.txt"
    constraint_payload = b"pydantic==2.12.5\n"
    constraints.write_bytes(constraint_payload)
    document: dict[str, object] = {
        "format_version": 1,
        "repository": "alxjmyr/ricky.sh",
        "channel": "stable",
        "source": "local_drill",
        "software_version": version,
        "supported_source_data_generations": [1],
        "target_data_generation": 1,
        "python_requirement": ">=3.12",
        "minimum_uv_version": "0.6.0",
        "wheel": {
            "name": wheel.name,
            "url": wheel.as_uri(),
            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "size": wheel.stat().st_size,
        },
        "constraints": {
            "name": constraints.name,
            "url": constraints.as_uri(),
            "sha256": hashlib.sha256(constraint_payload).hexdigest(),
            "size": len(constraint_payload),
        },
    }
    descriptor = tmp_path / f"ricky-{version}-release.json"
    descriptor.write_text(json.dumps(document), encoding="utf-8")
    return descriptor, document


async def test_local_resolver_selects_latest_or_exact_and_caches_verified_pair(
    tmp_path: Path,
) -> None:
    old_path, _old = _local_descriptor(tmp_path / "old", "0.6.0")
    new_path, _new = _local_descriptor(tmp_path / "new", "0.6.1")
    resolver = LocalReleaseResolver((old_path, new_path))

    latest = await resolver.resolve(None)
    exact = await resolver.resolve(ReleaseVersion.parse("0.6.0"))

    assert latest is not None and str(latest.software_version) == "0.6.1"
    assert exact is not None and str(exact.software_version) == "0.6.0"
    wheel, constraints = await cache_release_artifacts(latest, tmp_path / "cache")
    assert wheel.name == latest.wheel.name
    assert constraints.name == latest.constraints.name
    assert wheel.stat().st_mode & 0o777 == 0o600


async def test_cache_refuses_wrong_checksum_and_wrong_wheel_identity(tmp_path: Path) -> None:
    descriptor_path, document = _local_descriptor(tmp_path / "wrong-checksum")
    wheel_document = document["wheel"]
    assert isinstance(wheel_document, dict)
    document["wheel"] = {**wheel_document, "sha256": "0" * 64}
    descriptor_path.write_text(json.dumps(document), encoding="utf-8")
    descriptor = await LocalReleaseResolver((descriptor_path,)).resolve(None)
    assert descriptor is not None
    with pytest.raises(ReleaseResolutionError, match="checksum"):
        await cache_release_artifacts(descriptor, tmp_path / "cache-one")

    descriptor_path, document = _local_descriptor(tmp_path / "wrong-version")
    wheel_document = document["wheel"]
    assert isinstance(wheel_document, dict)
    wheel = Path(str(wheel_document["url"])[7:])
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "ricky-9.9.9.dist-info/METADATA",
            "Metadata-Version: 2.3\nName: ricky\nVersion: 9.9.9\n",
        )
    document["wheel"] = _artifact(wheel, wheel.read_bytes())
    descriptor_path.write_text(json.dumps(document), encoding="utf-8")
    descriptor = await LocalReleaseResolver((descriptor_path,)).resolve(None)
    assert descriptor is not None
    with pytest.raises(ReleaseResolutionError, match="identity"):
        await cache_release_artifacts(descriptor, tmp_path / "cache-two")


async def test_github_resolver_validates_stable_tag_descriptor_and_redirect_hosts() -> None:
    version = "0.6.1"
    descriptor_name = f"ricky-{version}-release.json"
    wheel_url = (
        f"https://github.com/alxjmyr/ricky.sh/releases/download/v{version}/"
        f"ricky-{version}-py3-none-any.whl"
    )
    constraints_url = (
        f"https://github.com/alxjmyr/ricky.sh/releases/download/v{version}/"
        f"ricky-{version}-constraints.txt"
    )
    descriptor = {
        "format_version": 1,
        "repository": "alxjmyr/ricky.sh",
        "channel": "stable",
        "source": "github_release",
        "software_version": version,
        "supported_source_data_generations": [1],
        "target_data_generation": 1,
        "python_requirement": ">=3.12",
        "minimum_uv_version": "0.6.0",
        "wheel": {
            "name": wheel_url.rsplit("/", 1)[1],
            "url": wheel_url,
            "sha256": "1" * 64,
            "size": 1,
        },
        "constraints": {
            "name": constraints_url.rsplit("/", 1)[1],
            "url": constraints_url,
            "sha256": "2" * 64,
            "size": 1,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(
                200,
                json={
                    "tag_name": f"v{version}",
                    "draft": False,
                    "prerelease": False,
                    "assets": [
                        {
                            "name": descriptor_name,
                            "browser_download_url": (
                                f"https://github.com/alxjmyr/ricky.sh/releases/download/"
                                f"v{version}/{descriptor_name}"
                            ),
                        }
                    ],
                },
            )
        return httpx.Response(200, json=descriptor)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        selected = await GitHubReleaseResolver(client).resolve(None)
    assert selected == ReleaseDescriptor.model_validate_json(json.dumps(descriptor))

    def hostile(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return handler(request)
        return httpx.Response(302, headers={"location": "https://evil.example/artifact"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(hostile)) as client:
        with pytest.raises(ReleaseResolutionError, match="not approved"):
            await GitHubReleaseResolver(client).resolve(None)


@pytest.mark.parametrize("tag", ["0.6.1", "v0.6", "v0.6.1rc1"])
async def test_github_resolver_refuses_non_stable_release_tags(tag: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"tag_name": tag, "draft": False, "prerelease": False, "assets": []},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ReleaseResolutionError, match="stable release"):
            await GitHubReleaseResolver(client).resolve(None)


class _CountingStream(httpx.AsyncByteStream):
    """A response body that records how many bytes a client actually pulled."""

    def __init__(self, chunk: bytes, chunks: int) -> None:
        self._chunk = chunk
        self._chunks = chunks
        self.produced = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            self.produced += len(self._chunk)
            yield self._chunk


def _wheel_payload(version: str, description: str = "") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            f"ricky-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.3\nName: ricky\nVersion: {version}\n"
            f"Description-Content-Type: text/markdown\n\n{description}",
        )
    return buffer.getvalue()


def _github_document(
    version: str,
    wheel_payload: bytes,
    constraints_payload: bytes,
) -> dict[str, object]:
    def artifact(name: str, payload: bytes) -> dict[str, object]:
        return {
            "name": name,
            "url": f"{_DOWNLOAD_ROOT}/v{version}/{name}",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }

    return {
        "format_version": 1,
        "repository": "alxjmyr/ricky.sh",
        "channel": "stable",
        "source": "github_release",
        "software_version": version,
        "supported_source_data_generations": [1],
        "target_data_generation": 1,
        "python_requirement": ">=3.12",
        "minimum_uv_version": "0.6.0",
        "wheel": artifact(f"ricky-{version}-py3-none-any.whl", wheel_payload),
        "constraints": artifact(f"ricky-{version}-constraints.txt", constraints_payload),
    }


def _release_listing(version: str) -> dict[str, object]:
    name = f"ricky-{version}-release.json"
    return {
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": False,
        "assets": [{"name": name, "browser_download_url": f"{_DOWNLOAD_ROOT}/v{version}/{name}"}],
    }


async def test_release_response_over_its_limit_aborts_without_buffering_the_body() -> None:
    offered = 64 * 1024 * 1024
    stream = _CountingStream(b"x" * (64 * 1024), offered // (64 * 1024))

    def flood(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(flood)) as client:
        with pytest.raises(ReleaseResolutionError, match="size limit"):
            await GitHubReleaseResolver(client).resolve(None)

    assert 0 < stream.produced <= 512 * 1024
    assert stream.produced < offered


async def test_release_response_declaring_an_oversized_length_is_refused_unread() -> None:
    stream = _CountingStream(b"x" * 1024, 16)

    def declared(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=stream,
            headers={"content-length": str(64 * 1024 * 1024)},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(declared)) as client:
        with pytest.raises(ReleaseResolutionError, match="size limit"):
            await GitHubReleaseResolver(client).resolve(None)

    assert stream.produced == 0


async def test_artifact_download_accepts_its_exact_size_and_aborts_an_oversized_body(
    tmp_path: Path,
) -> None:
    version = "0.6.1"
    wheel_payload = _wheel_payload(version)
    constraints_payload = b"pydantic==2.12.5\n"
    document = _github_document(version, wheel_payload, constraints_payload)
    descriptor = ReleaseDescriptor.model_validate_json(json.dumps(document))

    def exact(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".whl"):
            return httpx.Response(200, content=wheel_payload)
        return httpx.Response(200, content=constraints_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(exact)) as client:
        wheel, constraints = await cache_release_artifacts(
            descriptor,
            tmp_path / "cache",
            client=client,
        )
    assert wheel.read_bytes() == wheel_payload
    assert constraints.read_bytes() == constraints_payload

    stream = _CountingStream(b"x" * 4096, 4096)

    def flood(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".whl"):
            return httpx.Response(200, stream=stream)
        return httpx.Response(200, content=constraints_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(flood)) as client:
        with pytest.raises(ReleaseResolutionError, match="size limit"):
            await cache_release_artifacts(descriptor, tmp_path / "flooded", client=client)

    assert 0 < stream.produced <= 8 * 1024
    assert not (tmp_path / "flooded" / descriptor.wheel.name).exists()


async def test_release_requests_follow_approved_hops_and_refuse_unbounded_chains() -> None:
    version = "0.6.1"
    document = _github_document(version, _wheel_payload(version), b"pydantic==2.12.5\n")

    def approved_hop(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, json=_release_listing(version))
        if request.url.host == "github.com":
            return httpx.Response(
                302,
                headers={"location": "https://objects.githubusercontent.com/descriptor"},
            )
        return httpx.Response(200, json=document)

    async with httpx.AsyncClient(transport=httpx.MockTransport(approved_hop)) as client:
        selected = await GitHubReleaseResolver(client).resolve(None)
    assert selected == ReleaseDescriptor.model_validate_json(json.dumps(document))

    def endless(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://objects.githubusercontent.com/next"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(endless)) as client:
        with pytest.raises(ReleaseResolutionError, match="redirect chain is invalid"):
            await GitHubReleaseResolver(client).resolve(None)

    def locationless(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    async with httpx.AsyncClient(transport=httpx.MockTransport(locationless)) as client:
        with pytest.raises(ReleaseResolutionError, match="redirect chain is invalid"):
            await GitHubReleaseResolver(client).resolve(None)

    def downgrade(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://github.com/descriptor"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(downgrade)) as client:
        with pytest.raises(ReleaseResolutionError, match="not approved"):
            await GitHubReleaseResolver(client).resolve(None)


async def test_cache_accepts_a_wheel_whose_description_body_looks_like_metadata_headers(
    tmp_path: Path,
) -> None:
    version = "0.6.1"
    descriptor_path, document = _local_descriptor(tmp_path / "described", version)
    wheel_document = document["wheel"]
    assert isinstance(wheel_document, dict)
    wheel = Path(str(wheel_document["url"])[7:])
    description = "# ricky.sh\n\nAn installed release exposes:\n\nName: ricky\nVersion: 9.9.9\n"
    wheel.write_bytes(_wheel_payload(version, description))
    document["wheel"] = _artifact(wheel, wheel.read_bytes())
    descriptor_path.write_text(json.dumps(document), encoding="utf-8")
    descriptor = await LocalReleaseResolver((descriptor_path,)).resolve(None)
    assert descriptor is not None

    cached_wheel, _constraints = await cache_release_artifacts(descriptor, tmp_path / "cache")

    assert cached_wheel.name == descriptor.wheel.name
