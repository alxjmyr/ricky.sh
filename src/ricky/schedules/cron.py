"""Deterministic cron rendering and preserving user-crontab reconciliation."""

from __future__ import annotations

import asyncio
import fcntl
import os
import shlex
import shutil
import tempfile
from collections.abc import Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from ricky.config import RickySettings, user_data_path
from ricky.schedules.types import ScheduleSpec

BEGIN_MARKER = "# BEGIN RICKY MANAGED SCHEDULES"
END_MARKER = "# END RICKY MANAGED SCHEDULES"


class CronError(RuntimeError):
    """A bounded platform, marker, command, or verification failure."""


class CommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    async def run(self, args: Sequence[str]) -> CommandResult: ...


class SubprocessCommandRunner:
    """Shell-free asynchronous subprocess adapter for the crontab program."""

    async def run(self, args: Sequence[str]) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
            await process.communicate()
            raise
        return CommandResult(
            returncode=process.returncode or 0,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


@dataclass(frozen=True)
class ManagedCrontab:
    prefix: str
    separator: str
    block: str | None
    suffix: str

    @property
    def without_block(self) -> str:
        return self.prefix + self.suffix


@dataclass(frozen=True)
class CronApplyResult:
    changed: bool
    backup_path: Path | None
    installed_text: str


def resolve_ricky_executable() -> Path:
    """Resolve the exact Ricky console script at reconciliation time."""

    candidate = shutil.which("ricky")
    if candidate is None:
        raise CronError("Ricky executable is not available on PATH")
    path = Path(candidate).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise CronError(f"Ricky executable is not executable: {path}")
    return path


def render_fragment(
    schedules: Sequence[ScheduleSpec], *, ricky_executable: Path, log_dir: Path
) -> str:
    """Render a complete inspectable managed block, sorted by opaque id."""

    executable = _command_atom(ricky_executable, "Ricky executable")
    lines = [BEGIN_MARKER]
    for schedule in sorted(schedules, key=lambda item: item.id):
        project = _command_atom(Path(schedule.project_root), "project root")
        log = _command_atom(log_dir / f"{schedule.id}.log", "launcher log")
        command = [
            str(executable),
            "schedule",
            "invoke",
            schedule.id,
            "--project",
            str(project),
            "--profile",
            schedule.profile_scope.primary,
        ]
        for profile in schedule.profile_scope.profiles:
            if profile not in {"shared", schedule.profile_scope.primary}:
                command.extend(("--access-profile", profile))
        quoted = " ".join(_cron_quote(item) for item in command)
        quoted_log = _cron_quote(str(log))
        lines.append(f"{schedule.cron} umask 077; : > {quoted_log}; {quoted} >> {quoted_log} 2>&1")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def parse_managed_crontab(text: str) -> ManagedCrontab:
    """Locate one well-formed managed block or reject ambiguous marker state."""

    lines = text.splitlines(keepends=True)
    begins: list[int] = []
    ends: list[int] = []
    offset = 0
    spans: list[tuple[int, int]] = []
    for line in lines:
        end = offset + len(line)
        content = line.rstrip("\r\n")
        if BEGIN_MARKER in content and content != BEGIN_MARKER:
            raise CronError("malformed Ricky begin marker in user crontab")
        if END_MARKER in content and content != END_MARKER:
            raise CronError("malformed Ricky end marker in user crontab")
        if content == BEGIN_MARKER:
            begins.append(len(spans))
        if content == END_MARKER:
            ends.append(len(spans))
        spans.append((offset, end))
        offset = end
    if not begins and not ends:
        return ManagedCrontab(prefix=text, separator="", block=None, suffix="")
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        raise CronError("Ricky crontab markers are missing, duplicated, reversed, or nested")
    start = spans[begins[0]][0]
    end = spans[ends[0]][1]
    separator = ""
    prefix_end = start
    if start > 0 and text[start - 1] == "\n":
        separator = "\n"
        prefix_end -= 1
    return ManagedCrontab(
        prefix=text[:prefix_end],
        separator=separator,
        block=text[start:end],
        suffix=text[end:],
    )


def merge_managed_block(current: str, fragment: str) -> str:
    parsed = parse_managed_crontab(current)
    if parsed.block is not None:
        return parsed.prefix + parsed.separator + fragment + parsed.suffix
    separator = "" if not current else "\n"
    return current + separator + fragment


class UserCrontabBackend:
    """Own only Ricky's marked block in the current POSIX user's crontab."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        runner: CommandRunner | None = None,
        crontab_executable: str = "crontab",
    ) -> None:
        self.root = user_data_path(settings) / "cron"
        self.fragment_path = self.root / "ricky.crontab"
        self.backup_dir = self.root / "backups"
        self.log_dir = self.root / "logs"
        self.lock_path = self.root / "lock"
        self.runner = runner or SubprocessCommandRunner()
        self.crontab_executable = crontab_executable

    async def read(self) -> str:
        self._platform_check()
        try:
            result = await self.runner.run([self.crontab_executable, "-l"])
        except OSError as exc:
            raise CronError(f"cannot execute crontab reader: {exc}") from exc
        if result.returncode == 0:
            return result.stdout
        if result.returncode == 1 and "no crontab" in result.stderr.lower():
            return ""
        raise CronError(f"cannot read user crontab: {result.stderr.strip() or result.returncode}")

    async def sync(self, fragment: str) -> CronApplyResult:
        async with self.reconciliation_lock():
            return await self.sync_locked(fragment)

    async def uninstall(self) -> CronApplyResult:
        async with self.reconciliation_lock():
            return await self._reconcile_locked(fragment=None, uninstall=True)

    @asynccontextmanager
    async def reconciliation_lock(self):
        """Hold the one schedule/crontab lock across desired-state generation."""

        self._platform_check()
        descriptor = await asyncio.to_thread(self._acquire_lock)
        try:
            yield
        finally:
            await asyncio.to_thread(self._release_lock, descriptor)

    async def sync_locked(self, fragment: str) -> CronApplyResult:
        """Install a fragment while the caller holds reconciliation_lock."""

        return await self._reconcile_locked(fragment=fragment, uninstall=False)

    async def _reconcile_locked(self, *, fragment: str | None, uninstall: bool) -> CronApplyResult:
        current = await self.read()
        parsed = parse_managed_crontab(current)
        desired = (
            parsed.without_block if uninstall else merge_managed_block(current, fragment or "")
        )
        if fragment is not None:
            await asyncio.to_thread(self._write_atomic, self.fragment_path, fragment)
        if desired == current:
            return CronApplyResult(False, None, current)
        backup = await asyncio.to_thread(self._backup, current)
        temporary = await asyncio.to_thread(self._temporary_crontab, desired)
        cancelled = False
        try:
            operation = asyncio.create_task(
                self._install_and_verify(
                    temporary,
                    desired=desired,
                    fragment=fragment,
                    uninstall=uninstall,
                    backup=backup,
                )
            )
            while True:
                try:
                    installed = await asyncio.shield(operation)
                    break
                except asyncio.CancelledError:
                    cancelled = True
                    current_task = asyncio.current_task()
                    if current_task is not None:
                        current_task.uncancel()
        finally:
            temporary.unlink(missing_ok=True)
        if cancelled:
            raise asyncio.CancelledError
        return CronApplyResult(True, backup, installed)

    async def _install_and_verify(
        self,
        temporary: Path,
        *,
        desired: str,
        fragment: str | None,
        uninstall: bool,
        backup: Path,
    ) -> str:
        try:
            result = await self.runner.run([self.crontab_executable, str(temporary)])
        except OSError as exc:
            raise CronError(f"cannot execute crontab installer: {exc}") from exc
        if result.returncode != 0:
            raise CronError(
                f"cannot install user crontab: {result.stderr.strip() or result.returncode}"
            )
        installed = await self.read()
        if installed != desired:
            raise CronError(
                "installed crontab did not match the verified complete candidate; "
                f"backup retained at {backup}"
            )
        installed_block = parse_managed_crontab(installed).block
        if uninstall:
            if installed_block is not None:
                raise CronError(f"Ricky block remained after uninstall; backup: {backup}")
        elif installed_block != fragment:
            raise CronError(f"installed Ricky block failed exact verification; backup: {backup}")
        return installed

    def _platform_check(self) -> None:
        if os.name != "posix":
            raise CronError("managed schedules require POSIX crontab and fcntl")

    def _acquire_lock(self) -> int:
        self._secure_dir(self.root)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise CronError("another Ricky schedule operation holds the cron lock") from exc
        return descriptor

    @staticmethod
    def _release_lock(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    def _backup(self, content: str) -> Path:
        self._secure_dir(self.backup_dir)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        path = self.backup_dir / f"{timestamp}-{uuid4().hex[:8]}-crontab"
        self._write_atomic(path, content)
        return path

    def _temporary_crontab(self, content: str) -> Path:
        self._secure_dir(self.root)
        descriptor, raw_path = tempfile.mkstemp(prefix=".install-", dir=self.root)
        path = Path(raw_path)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return path

    def _write_atomic(self, path: Path, content: str) -> None:
        self._secure_dir(path.parent)
        descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        temporary = Path(raw_path)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _secure_dir(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)


def _command_atom(path: Path, label: str) -> Path:
    raw = str(path)
    has_control = any(ord(character) < 32 or ord(character) == 127 for character in raw)
    has_marker = BEGIN_MARKER in raw or END_MARKER in raw
    if not path.is_absolute() or has_control or has_marker:
        raise CronError(f"{label} must be an absolute path without control characters")
    return path


def _cron_quote(value: str) -> str:
    """Quote one fixed argv atom and escape cron's pre-shell percent syntax."""

    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise CronError("generated cron command atom contains a control character")
    return shlex.quote(value).replace("%", r"\%")
