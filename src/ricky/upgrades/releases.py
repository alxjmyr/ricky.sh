"""Anonymous stable-release resolution and authenticated artifact caching."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from dataclasses import dataclass
from email.parser import Parser
from email.policy import compat32
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx
from pydantic import ValidationError

from ricky.upgrades.models import RELEASE_REPOSITORY, ReleaseArtifact, ReleaseDescriptor
from ricky.upgrades.versions import ReleaseVersion

_API_ROOT = f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases"
_DESCRIPTOR_LIMIT = 256 * 1024
_REDIRECT_LIMIT = 5
_APPROVED_INITIAL_HOSTS = frozenset({"api.github.com", "github.com"})
_APPROVED_REDIRECT_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)


class ReleaseResolutionError(RuntimeError):
    """A release or artifact was absent, ambiguous, unsafe, or invalid."""


@dataclass(frozen=True, slots=True)
class _BoundedResponse:
    """One release response whose body was read under an enforced size limit."""

    status_code: int
    content: bytes


class GitHubReleaseResolver:
    """Resolve one public stable GitHub release without credentials."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client

    async def resolve(self, requested_version: ReleaseVersion | None) -> ReleaseDescriptor | None:
        endpoint = (
            f"{_API_ROOT}/latest"
            if requested_version is None
            else f"{_API_ROOT}/tags/v{requested_version}"
        )
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "ricky-upgrade",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            follow_redirects=False,
        )
        owns_client = self._client is None
        try:
            response = await _request(client, endpoint, max_bytes=_DESCRIPTOR_LIMIT)
            if response.status_code == 404:
                return None
            if response.status_code != 200:
                raise ReleaseResolutionError(
                    f"GitHub release lookup failed with HTTP {response.status_code}"
                )
            release = _release_document(response.content)
            tag = release["tag_name"]
            version = _tag_version(tag)
            if requested_version is not None and version != requested_version:
                raise ReleaseResolutionError(
                    "GitHub release does not match the exact requested version"
                )
            if release["draft"] or release["prerelease"]:
                raise ReleaseResolutionError("selected GitHub release is not stable")
            descriptor_name = f"ricky-{version}-release.json"
            matches = [
                asset
                for asset in release["assets"]
                if isinstance(asset, dict) and asset.get("name") == descriptor_name
            ]
            if len(matches) != 1:
                raise ReleaseResolutionError(
                    "GitHub release must contain exactly one release descriptor"
                )
            descriptor_url = matches[0].get("browser_download_url")
            if not isinstance(descriptor_url, str):
                raise ReleaseResolutionError("release descriptor URL is invalid")
            _require_exact_github_asset_url(descriptor_url, version, descriptor_name)
            descriptor_response = await _request(
                client,
                descriptor_url,
                max_bytes=_DESCRIPTOR_LIMIT,
            )
            if descriptor_response.status_code != 200:
                raise ReleaseResolutionError(
                    f"release descriptor download failed with HTTP "
                    f"{descriptor_response.status_code}"
                )
            try:
                descriptor = ReleaseDescriptor.model_validate_json(descriptor_response.content)
            except ValidationError as exc:
                raise ReleaseResolutionError("release descriptor is invalid") from exc
            if descriptor.source != "github_release":
                raise ReleaseResolutionError("GitHub release descriptor has the wrong source")
            if descriptor.software_version != version:
                raise ReleaseResolutionError("release descriptor version does not match its tag")
            return descriptor
        except httpx.HTTPError as exc:
            raise ReleaseResolutionError("GitHub release request failed") from exc
        finally:
            if owns_client:
                await client.aclose()


class LocalReleaseResolver:
    """Resolve strict local-drill descriptors without network access."""

    def __init__(self, descriptors: tuple[Path, ...]) -> None:
        self._descriptors = tuple(path.expanduser().resolve() for path in descriptors)

    async def resolve(self, requested_version: ReleaseVersion | None) -> ReleaseDescriptor | None:
        releases: list[ReleaseDescriptor] = []
        for path in self._descriptors:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > _DESCRIPTOR_LIMIT:
                raise ReleaseResolutionError("local release descriptor path is invalid")
            try:
                descriptor = ReleaseDescriptor.model_validate_json(path.read_bytes())
            except (OSError, ValidationError) as exc:
                raise ReleaseResolutionError("local release descriptor is invalid") from exc
            if descriptor.source != "local_drill":
                raise ReleaseResolutionError("local resolver requires a local-drill descriptor")
            releases.append(descriptor)
        if requested_version is not None:
            matches = [item for item in releases if item.software_version == requested_version]
            if len(matches) > 1:
                raise ReleaseResolutionError("local release selection is ambiguous")
            return matches[0] if matches else None
        return max(releases, key=lambda item: item.software_version, default=None)


async def cache_release_artifacts(
    descriptor: ReleaseDescriptor,
    destination: Path,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[Path, Path]:
    """Cache and verify an exact wheel and constraints pair in a private directory."""

    root = destination.expanduser().resolve()
    if root.exists() and (root.is_symlink() or not root.is_dir()):
        raise ReleaseResolutionError("release cache destination is invalid")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(root, 0o700)
    wheel = await _cache_artifact(descriptor.wheel, root, client=client)
    constraints = await _cache_artifact(descriptor.constraints, root, client=client)
    _verify_wheel(wheel, descriptor.software_version)
    try:
        constraints.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReleaseResolutionError("release constraints are not valid UTF-8") from exc
    return wheel, constraints


async def _cache_artifact(
    artifact: ReleaseArtifact,
    destination: Path,
    *,
    client: httpx.AsyncClient | None,
) -> Path:
    target = destination / artifact.name
    if target.exists():
        _verify_cached_file(target, artifact)
        return target
    parsed = urlsplit(artifact.url)
    if parsed.scheme == "file":
        source = Path(unquote(parsed.path))
        if (
            not source.is_absolute()
            or source != source.resolve()
            or source.is_symlink()
            or not source.is_file()
        ):
            raise ReleaseResolutionError("local release artifact is not a canonical file")
        payload = source.read_bytes()
    else:
        owned = client is None
        active_client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"User-Agent": "ricky-upgrade"},
            follow_redirects=False,
        )
        try:
            response = await _request(
                active_client,
                artifact.url,
                max_bytes=artifact.size,
            )
            if response.status_code != 200:
                raise ReleaseResolutionError(
                    f"release artifact download failed with HTTP {response.status_code}"
                )
            payload = response.content
        except httpx.HTTPError as exc:
            raise ReleaseResolutionError("release artifact download failed") from exc
        finally:
            if owned:
                await active_client.aclose()
    if len(payload) != artifact.size:
        raise ReleaseResolutionError("release artifact size does not match its descriptor")
    if hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise ReleaseResolutionError("release artifact checksum does not match its descriptor")
    _write_private_bytes(target, payload)
    return target


async def _request(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
) -> _BoundedResponse:
    current = url
    for redirect_count in range(_REDIRECT_LIMIT + 1):
        parsed = urlsplit(current)
        allowed = _APPROVED_INITIAL_HOSTS if redirect_count == 0 else _APPROVED_REDIRECT_HOSTS
        if (
            parsed.scheme != "https"
            or parsed.hostname not in allowed
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ReleaseResolutionError("release request URL or redirect host is not approved")
        async with client.stream("GET", current, follow_redirects=False) as response:
            if response.status_code not in {301, 302, 303, 307, 308}:
                return _BoundedResponse(
                    status_code=response.status_code,
                    content=await _bounded_body(response, max_bytes),
                )
            location = response.headers.get("location")
        if location is None or redirect_count == _REDIRECT_LIMIT:
            raise ReleaseResolutionError("release redirect chain is invalid")
        current = urljoin(current, location)
    raise AssertionError("bounded redirect loop escaped")


async def _bounded_body(response: httpx.Response, max_bytes: int) -> bytes:
    """Read one response body, aborting as soon as it passes its size limit."""

    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise ReleaseResolutionError("release response exceeds its size limit")
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ReleaseResolutionError("release response exceeds its size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _release_document(payload: bytes) -> dict[str, Any]:
    try:
        document = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseResolutionError("GitHub release response is invalid JSON") from exc
    if not isinstance(document, dict):
        raise ReleaseResolutionError("GitHub release response has the wrong shape")
    required = {"tag_name": str, "draft": bool, "prerelease": bool, "assets": list}
    if any(not isinstance(document.get(key), expected) for key, expected in required.items()):
        raise ReleaseResolutionError("GitHub release response is missing required fields")
    return document


def _tag_version(tag: str) -> ReleaseVersion:
    if not tag.startswith("v"):
        raise ReleaseResolutionError("GitHub release tag is not a Ricky stable release")
    try:
        return ReleaseVersion.parse(tag[1:])
    except ValueError as exc:
        raise ReleaseResolutionError("GitHub release tag is not a Ricky stable release") from exc


def _require_exact_github_asset_url(url: str, version: ReleaseVersion, filename: str) -> None:
    parsed = urlsplit(url)
    expected = f"/{RELEASE_REPOSITORY}/releases/download/v{version}/{filename}"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.port not in {None, 443}
        or unquote(parsed.path) != expected
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ReleaseResolutionError("GitHub release descriptor URL is not exact")


def _verify_cached_file(path: Path, artifact: ReleaseArtifact) -> None:
    if path.is_symlink() or not path.is_file():
        raise ReleaseResolutionError("release cache contains an invalid artifact path")
    payload = path.read_bytes()
    if len(payload) != artifact.size or hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise ReleaseResolutionError("release cache artifact does not match its descriptor")


def _verify_wheel(path: Path, version: ReleaseVersion) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_files = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA") and "/" in name
            ]
            if len(metadata_files) != 1:
                raise ReleaseResolutionError("release wheel metadata is ambiguous")
            metadata = archive.read(metadata_files[0]).decode("utf-8")
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError) as exc:
        raise ReleaseResolutionError("release wheel is invalid") from exc
    headers = Parser(policy=compat32).parsestr(metadata, headersonly=True)
    names = [str(value).strip() for value in headers.get_all("Name", [])]
    versions = [str(value).strip() for value in headers.get_all("Version", [])]
    if names != ["ricky"] or versions != [str(version)]:
        raise ReleaseResolutionError("release wheel package identity does not match descriptor")


def _write_private_bytes(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise ReleaseResolutionError("release cache target already exists")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
