"""One-shot local vault unlock handoff for managed gateway startup."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
import stat
import struct
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from ricky.config import RickySettings, ensure_private_user_data_root, user_data_path
from ricky.gateway.lock import GatewayLock, lock_path
from ricky.profiles import ProfileName
from ricky.protected_values import ResidentProtectedValueRegistry

STARTUP_UNLOCK_FAILURE_EXIT_CODE = 78
_PROTOCOL_VERSION = 1
_MAX_FRAME_BYTES = 262_144
_MAX_PROFILES = 64
_MAX_PASSPHRASE_CHARS = 4_096
_SO_PEERCRED_SIZE = struct.calcsize("3i")


class GatewayVaultBootstrapError(RuntimeError):
    """A requested startup unlock could not be completed safely."""


class _Hello(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1


class _Credential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    profile: ProfileName
    passphrase: SecretStr = Field(min_length=1, max_length=_MAX_PASSPHRASE_CHARS)


class _UnlockPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    credentials: tuple[_Credential, ...] = Field(
        min_length=1,
        max_length=_MAX_PROFILES,
    )

    @model_validator(mode="after")
    def _unique_profiles(self) -> _UnlockPayload:
        profiles = [item.profile for item in self.credentials]
        if len(profiles) != len(set(profiles)):
            raise ValueError("startup unlock profiles must be unique")
        return self


class _UnlockAck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    version: Literal[1] = 1
    ok: bool
    profiles: tuple[ProfileName, ...] = Field(default=(), max_length=_MAX_PROFILES)
    error: str | None = Field(default=None, max_length=200)


def vault_bootstrap_socket_path(settings: RickySettings) -> Path:
    """Resolve the non-secret rendezvous beside the configured gateway lock."""

    return lock_path(settings).with_name("vault-unlock-bootstrap.sock")


class GatewayVaultBootstrapServer:
    """Transfer selected passphrases once to the exact new gateway owner."""

    def __init__(
        self,
        settings: RickySettings,
        passphrases: Mapping[str, SecretStr],
    ) -> None:
        if not passphrases:
            raise ValueError("startup unlock requires at least one profile")
        self.settings = settings
        self.path = vault_bootstrap_socket_path(settings)
        self._passphrases = dict(passphrases)
        self._server: asyncio.AbstractServer | None = None
        self._result: asyncio.Future[tuple[str, ...]] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._accepted = False

    async def __aenter__(self) -> GatewayVaultBootstrapServer:
        _require_peer_credentials()
        ensure_private_user_data_root(self.settings)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        await _remove_stale_bootstrap_socket(self.path)
        self._result = asyncio.get_running_loop().create_future()
        try:
            self._server = await asyncio.start_unix_server(
                self._connected,
                path=self.path,
            )
            os.chmod(self.path, 0o600)
        except BaseException:
            with suppress(OSError):
                self.path.unlink()
            raise
        return self

    async def exchange(self) -> tuple[str, ...]:
        """Wait for one authenticated gateway, one transfer, and one acknowledgement."""

        if self._result is None:
            raise RuntimeError("gateway vault bootstrap server has not started")
        try:
            async with asyncio.timeout(self.settings.gateway.service.start_timeout_seconds):
                return await asyncio.shield(self._result)
        except TimeoutError as exc:
            raise GatewayVaultBootstrapError(
                "timed out waiting for the new gateway to unlock its vault"
            ) from exc

    async def aclose(self) -> None:
        """Join connection handlers and remove the rendezvous on every outcome."""

        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        for task in tuple(self._tasks):
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        self._tasks.clear()
        if self._result is not None and not self._result.done():
            self._result.cancel()
        elif self._result is not None and not self._result.cancelled():
            # Retrieve a handler failure even when the service-manager side
            # failed first and cancelled its waiter.
            self._result.exception()
        self._passphrases.clear()
        with suppress(OSError):
            self.path.unlink()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> None:
        await self.aclose()

    def _connected(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        result = self._result
        try:
            if result is None or result.done() or self._accepted:
                return
            peer_pid, peer_uid = _peer_credentials(writer)
            owner = GatewayLock(self.settings).read_owner()
            if (
                peer_uid != os.getuid()
                or owner is None
                or owner.pid != peer_pid
                or owner.user_data_dir != str(user_data_path(self.settings))
                or not GatewayLock(self.settings).is_active()
            ):
                return
            self._accepted = True
            hello = _Hello.model_validate_json(await _read_frame(reader), strict=True)
            if hello.version != _PROTOCOL_VERSION:  # pragma: no cover - Literal is authoritative
                raise GatewayVaultBootstrapError("unsupported vault bootstrap protocol")
            await _write_frame(writer, _payload_bytes(self._passphrases))
            # Drop the server's references as soon as the outbound protocol has
            # copied the payload into its bounded transport buffer.
            expected = tuple(self._passphrases)
            self._passphrases.clear()
            ack = _UnlockAck.model_validate_json(await _read_frame(reader), strict=True)
            if not ack.ok:
                raise GatewayVaultBootstrapError(
                    ack.error or "the new gateway could not unlock its requested vaults"
                )
            if ack.profiles != expected:
                raise GatewayVaultBootstrapError(
                    "the new gateway acknowledged a different vault profile set"
                )
            if not result.done():
                result.set_result(ack.profiles)
        except asyncio.CancelledError:
            raise
        except (ValidationError, ValueError, OSError, asyncio.IncompleteReadError):
            if result is not None and not result.done():
                result.set_exception(
                    GatewayVaultBootstrapError("gateway vault-unlock handoff failed")
                )
        except GatewayVaultBootstrapError as exc:
            if result is not None and not result.done():
                result.set_exception(exc)
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


async def consume_gateway_vault_bootstrap(
    settings: RickySettings,
    registry: ResidentProtectedValueRegistry,
) -> tuple[str, ...]:
    """Consume a pending managed-start handoff, or return when none exists."""

    path = vault_bootstrap_socket_path(settings)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ()
    if not stat.S_ISSOCK(info.st_mode):
        raise GatewayVaultBootstrapError("gateway vault bootstrap path is not a socket")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise GatewayVaultBootstrapError("gateway vault bootstrap socket is not owner-only")
    _require_peer_credentials()
    try:
        async with asyncio.timeout(settings.gateway.service.start_timeout_seconds):
            try:
                reader, writer = await asyncio.open_unix_connection(path)
            except FileNotFoundError:
                return ()
            except ConnectionRefusedError:
                _unlink_unchanged_socket(path, info)
                return ()
            try:
                _, peer_uid = _peer_credentials(writer)
                if peer_uid != os.getuid():
                    raise GatewayVaultBootstrapError(
                        "gateway vault bootstrap peer is not the current user"
                    )
                await _write_frame(writer, _Hello().model_dump_json().encode("utf-8"))
                payload = _UnlockPayload.model_validate_json(
                    await _read_frame(reader),
                    strict=True,
                )
                passphrases = {item.profile: item.passphrase for item in payload.credentials}
                try:
                    await registry.unlock_many(passphrases)
                    profiles = tuple(passphrases)
                    await _write_frame(
                        writer,
                        _UnlockAck(ok=True, profiles=profiles).model_dump_json().encode("utf-8"),
                    )
                except BaseException as exc:
                    await registry.lock()
                    with suppress(BaseException):
                        await _write_frame(
                            writer,
                            _UnlockAck(
                                ok=False,
                                error="requested vault unlock failed",
                            )
                            .model_dump_json()
                            .encode("utf-8"),
                        )
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise GatewayVaultBootstrapError("requested vault unlock failed") from exc
                finally:
                    passphrases.clear()
                return profiles
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
    except asyncio.CancelledError:
        await registry.lock()
        raise
    except TimeoutError as exc:
        await registry.lock()
        raise GatewayVaultBootstrapError("gateway vault-unlock handoff timed out") from exc
    except (ValidationError, OSError, asyncio.IncompleteReadError) as exc:
        await registry.lock()
        raise GatewayVaultBootstrapError("gateway vault-unlock handoff failed") from exc


def _payload_bytes(passphrases: Mapping[str, SecretStr]) -> bytes:
    if len(passphrases) > _MAX_PROFILES:
        raise GatewayVaultBootstrapError("too many vault profiles were requested")
    credentials: list[dict[str, str]] = []
    for profile, passphrase in passphrases.items():
        raw = passphrase.get_secret_value()
        if not raw or len(raw) > _MAX_PASSPHRASE_CHARS:
            raise GatewayVaultBootstrapError("vault passphrase is outside bootstrap bounds")
        credentials.append({"profile": profile, "passphrase": raw})
    payload = json.dumps(
        {"version": _PROTOCOL_VERSION, "credentials": credentials},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_FRAME_BYTES:
        raise GatewayVaultBootstrapError("vault bootstrap payload is too large")
    return payload


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    header = await reader.readexactly(4)
    length = struct.unpack("!I", header)[0]
    if length < 1 or length > _MAX_FRAME_BYTES:
        raise GatewayVaultBootstrapError("gateway vault bootstrap frame is outside bounds")
    return await reader.readexactly(length)


async def _write_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    if not payload or len(payload) > _MAX_FRAME_BYTES:
        raise GatewayVaultBootstrapError("gateway vault bootstrap frame is outside bounds")
    writer.write(struct.pack("!I", len(payload)))
    writer.write(payload)
    await writer.drain()


def _peer_credentials(writer: asyncio.StreamWriter) -> tuple[int, int]:
    raw_socket = writer.get_extra_info("socket")
    if raw_socket is None:
        raise GatewayVaultBootstrapError("gateway vault bootstrap peer is unavailable")
    try:
        raw = raw_socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _SO_PEERCRED_SIZE)
    except (AttributeError, OSError) as exc:
        raise GatewayVaultBootstrapError(
            "gateway vault bootstrap peer credentials are unavailable"
        ) from exc
    pid, uid, _ = struct.unpack("3i", raw)
    return pid, uid


def _require_peer_credentials() -> None:
    if os.name != "posix" or not hasattr(socket, "SO_PEERCRED"):
        raise GatewayVaultBootstrapError(
            "managed gateway vault unlock requires POSIX peer credentials"
        )


async def _remove_stale_bootstrap_socket(path: Path) -> None:
    """Remove only an unchanged, owner-only socket with no listening peer."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if (
        not stat.S_ISSOCK(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise GatewayVaultBootstrapError("a gateway vault-unlock bootstrap handoff already exists")
    try:
        reader, writer = await asyncio.open_unix_connection(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno != errno.ECONNREFUSED:
            raise GatewayVaultBootstrapError(
                "a gateway vault-unlock bootstrap handoff already exists"
            ) from exc
        _unlink_unchanged_socket(path, info)
        return
    del reader
    writer.close()
    with suppress(OSError):
        await writer.wait_closed()
    raise GatewayVaultBootstrapError("a gateway vault-unlock bootstrap handoff already exists")


def _unlink_unchanged_socket(path: Path, expected: os.stat_result) -> None:
    """Unlink a stale rendezvous only when no replacement won the race."""

    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if (
        current.st_dev != expected.st_dev
        or current.st_ino != expected.st_ino
        or not stat.S_ISSOCK(current.st_mode)
        or current.st_uid != os.getuid()
    ):
        return
    with suppress(FileNotFoundError):
        path.unlink()
