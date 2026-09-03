"""Application service for desired schedules, approval, sync, and invocation."""

from __future__ import annotations

import json
import os
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from ricky.config import (
    RickySettings,
    find_project_root,
    load_settings,
    profile_data_path,
    user_data_path,
)
from ricky.jobs.registry import LoadedJob, context_definition_digest
from ricky.jobs.runner import JobConfigurationError, JobRunner, runtime_policy_digest
from ricky.jobs.store import JobRunStore
from ricky.jobs.types import JobApprovalEnvelope, JobRun, RunOutcome
from ricky.profiles import ProfileScope
from ricky.schedules.cron import (
    CronError,
    UserCrontabBackend,
    parse_managed_crontab,
    render_fragment,
    resolve_ricky_executable,
)
from ricky.schedules.store import ScheduleStore
from ricky.schedules.types import (
    DoctorIssue,
    ScheduleDoctorReport,
    ScheduleInspection,
    ScheduleSpec,
    ScheduleSyncReport,
    validate_cron_expression,
    validate_schedule_id,
)


class ScheduleServiceError(RuntimeError):
    """A bounded schedule validation, approval, or invocation failure."""


class ScheduleService:
    """Own schedule desired state and delegate due-time execution to cron/jobs."""

    def __init__(
        self,
        settings: RickySettings,
        *,
        profile_scope: ProfileScope,
        project_root: Path | None = None,
        store: ScheduleStore | None = None,
        backend: UserCrontabBackend | None = None,
        ricky_executable: Path | None = None,
    ) -> None:
        self.settings = settings
        self.profile_scope = profile_scope
        self.project_root = find_project_root(project_root)
        self.store = store or ScheduleStore(settings, scope=profile_scope)
        self.backend = backend or UserCrontabBackend(settings)
        self._ricky_executable = ricky_executable

    async def list(self) -> list[ScheduleInspection]:
        return [await self.inspect(schedule) for schedule in await self.store.list()]

    async def show(self, schedule_id: str) -> ScheduleInspection:
        return await self.inspect(await self.store.get(schedule_id))

    async def create(
        self,
        job_name: str,
        cron: str,
        *,
        profile_scope: ProfileScope,
        project_root: Path | None = None,
    ) -> ScheduleSpec:
        root = self._resolve_project(project_root or self.project_root)
        project_settings = self._settings_for(root)
        runner = JobRunner(project_settings, project_root=root)
        loaded, authority = await runner.validate_revision(job_name, profile_scope=profile_scope)
        await runner.validate_context_decision(loaded, profile_scope=profile_scope)
        runner.registry_for(profile_scope).snapshot(loaded)
        now = datetime.now(UTC)
        schedule = ScheduleSpec(
            id=f"sched_{uuid4().hex[:24]}",
            job_name=loaded.resource.qualified,
            cron=validate_cron_expression(cron),
            enabled=True,
            project_root=str(root),
            profile_scope=profile_scope,
            approved_spec_digest=loaded.digest,
            approved_runtime_policy_digest=runtime_policy_digest(
                loaded.spec,
                project_settings,
                profile_scope,
            ),
            approved_authority=authority,
            approved_cron=validate_cron_expression(cron),
            created_at=now,
            updated_at=now,
        )
        return await self.store.create(schedule)

    async def update_cron(self, schedule_id: str, cron: str) -> ScheduleSpec:
        schedule = await self.store.get(schedule_id)
        updated = schedule.model_copy(
            update={
                "cron": validate_cron_expression(cron),
                "approved_cron": schedule.approved_cron or schedule.cron,
                "updated_at": datetime.now(UTC),
            }
        )
        return await self.store.replace(ScheduleSpec.model_validate(updated))

    async def set_enabled(self, schedule_id: str, enabled: bool) -> ScheduleSpec:
        schedule = await self.store.get(schedule_id)
        updated = schedule.model_copy(update={"enabled": enabled, "updated_at": datetime.now(UTC)})
        return await self.store.replace(ScheduleSpec.model_validate(updated))

    async def remove(self, schedule_id: str) -> ScheduleSpec:
        return await self.store.remove(schedule_id)

    async def approve(self, schedule_id: str) -> tuple[ScheduleInspection, ScheduleSpec, list[str]]:
        schedule = await self.store.get(schedule_id)
        before = await self.inspect(schedule)
        root = self._resolve_project(Path(schedule.project_root))
        project_settings = self._settings_for(root)
        runner = JobRunner(project_settings, project_root=root)
        loaded, authority = await runner.validate_revision(
            schedule.job_name, profile_scope=schedule.profile_scope
        )
        await runner.validate_context_decision(loaded, profile_scope=schedule.profile_scope)
        changes = self._approval_changes(schedule, loaded, project_settings)
        runner.registry_for(schedule.profile_scope).snapshot(loaded)
        updated = schedule.model_copy(
            update={
                "approved_spec_digest": loaded.digest,
                "approved_runtime_policy_digest": runtime_policy_digest(
                    loaded.spec,
                    project_settings,
                    schedule.profile_scope,
                ),
                "approved_authority": authority,
                "approved_cron": schedule.cron,
                "updated_at": datetime.now(UTC),
            }
        )
        approved = await self.store.replace(ScheduleSpec.model_validate(updated))
        return before, approved, changes

    async def refresh(self, schedule_id: str) -> tuple[ScheduleInspection, ScheduleSpec, list[str]]:
        """Validate and pin a changed execution revision without expanding approval."""

        schedule = await self.store.get(schedule_id)
        before = await self.inspect(schedule)
        root = self._resolve_project(Path(schedule.project_root))
        project_settings = self._settings_for(root)
        runner = JobRunner(project_settings, project_root=root)
        loaded, authority = await runner.validate_revision(
            schedule.job_name, profile_scope=schedule.profile_scope
        )
        await runner.validate_context_decision(loaded, profile_scope=schedule.profile_scope)
        reasons = self._reapproval_reasons(schedule, authority, revision_changed=True)
        if reasons:
            raise ScheduleServiceError("schedule reapproval is required: " + "; ".join(reasons))
        changes = self._approval_changes(schedule, loaded, project_settings)
        runner.registry_for(schedule.profile_scope).snapshot(loaded)
        updated = schedule.model_copy(
            update={
                "approved_spec_digest": loaded.digest,
                "approved_runtime_policy_digest": runtime_policy_digest(
                    loaded.spec,
                    project_settings,
                    schedule.profile_scope,
                ),
                "updated_at": datetime.now(UTC),
            }
        )
        refreshed = await self.store.replace(ScheduleSpec.model_validate(updated))
        return before, refreshed, changes

    async def inspect(self, schedule: ScheduleSpec) -> ScheduleInspection:
        if not schedule.enabled:
            return ScheduleInspection(schedule=schedule, state="disabled")
        try:
            root = self._resolve_project(Path(schedule.project_root))
            project_settings = self._settings_for(root)
            runner = JobRunner(project_settings, project_root=root)
            loaded, authority = await runner.validate_revision(
                schedule.job_name, profile_scope=schedule.profile_scope
            )
            policy_digest = runtime_policy_digest(
                loaded.spec,
                project_settings,
                schedule.profile_scope,
            )
        except (OSError, ValueError, JobConfigurationError, ScheduleServiceError) as exc:
            return ScheduleInspection(
                schedule=schedule,
                state="unavailable",
                detail=str(exc)[:2_000],
            )
        try:
            await runner.validate_context_decision(loaded, profile_scope=schedule.profile_scope)
        except JobConfigurationError as exc:
            return ScheduleInspection(
                schedule=schedule,
                state="lineage_required",
                current_spec_digest=loaded.digest,
                current_runtime_policy_digest=policy_digest,
                detail=str(exc)[:2_000],
            )
        revision_changed = (
            loaded.digest != schedule.approved_spec_digest
            or policy_digest != schedule.approved_runtime_policy_digest
        )
        reasons = self._reapproval_reasons(
            schedule,
            authority,
            revision_changed=revision_changed,
        )
        if reasons:
            return ScheduleInspection(
                schedule=schedule,
                state="approval_required",
                current_spec_digest=loaded.digest,
                current_runtime_policy_digest=policy_digest,
                detail="; ".join(reasons)[:2_000],
            )
        if revision_changed:
            return ScheduleInspection(
                schedule=schedule,
                state="validation_required",
                current_spec_digest=loaded.digest,
                current_runtime_policy_digest=policy_digest,
                detail="job execution revision changed and must be validated and refreshed",
            )
        return ScheduleInspection(
            schedule=schedule,
            state="ready",
            current_spec_digest=loaded.digest,
            current_runtime_policy_digest=policy_digest,
        )

    async def sync(self) -> ScheduleSyncReport:
        self._require_installation_scope()
        async with self.backend.reconciliation_lock():
            inspections = await self.list()
            ready = [item.schedule for item in inspections if item.state == "ready"]
            executable = self._executable()
            self.backend.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.backend.log_dir, 0o700)
            fragment = render_fragment(
                ready,
                ricky_executable=executable,
                log_dir=self.backend.log_dir.resolve(),
            )
            applied = await self.backend.sync_locked(fragment)
        return ScheduleSyncReport(
            changed=applied.changed,
            installed=[item.id for item in ready],
            disabled=[item.schedule.id for item in inspections if item.state == "disabled"],
            validation_required=[
                item.schedule.id for item in inspections if item.state == "validation_required"
            ],
            lineage_required=[
                item.schedule.id for item in inspections if item.state == "lineage_required"
            ],
            approval_required=[
                item.schedule.id for item in inspections if item.state == "approval_required"
            ],
            unavailable=[item.schedule.id for item in inspections if item.state == "unavailable"],
            backup_path=str(applied.backup_path) if applied.backup_path else None,
            fragment_path=str(self.backend.fragment_path),
        )

    async def uninstall(self) -> str | None:
        self._require_installation_scope()
        applied = await self.backend.uninstall()
        return str(applied.backup_path) if applied.backup_path else None

    async def invoke(self, schedule_id: str) -> JobRun:
        validate_schedule_id(schedule_id)
        self._rotate_launcher_log(schedule_id)
        schedule = await self.store.get(schedule_id)
        if Path(schedule.project_root).resolve() != self.project_root.resolve():
            return await self._record_rejection(
                schedule,
                "failed",
                "launcher project root does not match approved schedule project root",
            )
        inspection = await self.inspect(schedule)
        if inspection.state != "ready":
            outcome = (
                "approval_required"
                if inspection.state
                in {
                    "approval_required",
                    "validation_required",
                    "lineage_required",
                    "disabled",
                }
                else "failed"
            )
            return await self._record_rejection(
                schedule,
                outcome,
                inspection.detail or f"schedule is {inspection.state}",
            )
        try:
            return await JobRunner(
                self.settings,
                project_root=self.project_root,
            ).run(
                schedule.job_name,
                trigger="schedule",
                trigger_id=schedule.id,
                expected_spec_digest=schedule.approved_spec_digest,
                expected_runtime_policy_digest=schedule.approved_runtime_policy_digest,
                profile_scope=schedule.profile_scope,
            )
        except JobConfigurationError as exc:
            return await self._record_rejection(
                schedule,
                "approval_required",
                str(exc),
            )

    async def doctor(self) -> ScheduleDoctorReport:
        self._require_installation_scope()
        issues: list[DoctorIssue] = []
        executable: Path | None = None
        try:
            executable = self._executable()
        except CronError as exc:
            issues.append(DoctorIssue(severity="error", code="executable", message=str(exc)))
        inspections = await self.list()
        ready = [item.schedule for item in inspections if item.state == "ready"]
        for inspection in inspections:
            authority = inspection
            if inspection.state == "disabled":
                authority = await self.inspect(
                    inspection.schedule.model_copy(update={"enabled": True})
                )
            if authority.state in {
                "approval_required",
                "validation_required",
                "lineage_required",
                "unavailable",
            }:
                issues.append(
                    DoctorIssue(
                        severity="error",
                        code=authority.state,
                        message=authority.detail or authority.state,
                        schedule_id=inspection.schedule.id,
                    )
                )
            log = self.backend.log_dir / f"{inspection.schedule.id}.log"
            if (
                log.exists()
                and log.stat().st_size > self.settings.schedules.launcher_log_byte_limit
            ):
                issues.append(
                    DoctorIssue(
                        severity="warning",
                        code="launcher_log_oversize",
                        message=f"launcher log exceeds configured bound: {log}",
                        schedule_id=inspection.schedule.id,
                    )
                )
        installed_count = 0
        try:
            current = await self.backend.read()
            block = parse_managed_crontab(current).block
            installed_count = 0 if block is None else max(0, len(block.splitlines()) - 2)
            if executable is not None:
                expected = render_fragment(
                    ready,
                    ricky_executable=executable,
                    log_dir=self.backend.log_dir.resolve(),
                )
                if block != expected:
                    issues.append(
                        DoctorIssue(
                            severity="error",
                            code="installed_drift",
                            message="installed Ricky crontab block differs from desired state",
                        )
                    )
        except CronError as exc:
            issues.append(DoctorIssue(severity="error", code="crontab", message=str(exc)))
        return ScheduleDoctorReport(
            healthy=not any(issue.severity == "error" for issue in issues),
            executable_path=str(executable) if executable else None,
            desired_count=len(inspections),
            installed_count=installed_count,
            issues=issues,
        )

    async def _record_rejection(
        self,
        schedule: ScheduleSpec,
        outcome: RunOutcome,
        message: str,
    ) -> JobRun:
        now = datetime.now(UTC)
        loaded: LoadedJob | None = None
        with suppress(OSError, ValueError):
            loaded = (
                JobRunner(
                    self.settings,
                    project_root=self.project_root,
                )
                .registry_for(schedule.profile_scope)
                .load(schedule.job_name)
            )
        selection = self.settings.resolve_profile_selection(
            schedule.profile_scope,
            loaded.spec.provider if loaded else None,
            loaded.spec.model if loaded else None,
        )
        run = JobRun(
            id=f"jobrun_{uuid4().hex}",
            job_name=schedule.job_name,
            spec_digest=loaded.digest if loaded else None,
            provider=selection.provider,
            model=selection.model,
            profile_scope=schedule.profile_scope,
            session_id=f"schedule_{schedule.id}",
            outcome=outcome,
            started_at=now,
            finished_at=now,
            error=message[:2_000],
            runtime_policy_digest=(
                runtime_policy_digest(
                    loaded.spec,
                    self.settings,
                    schedule.profile_scope,
                )
                if loaded
                else None
            ),
            result_notification=(
                loaded.spec.result_notification if loaded is not None else "always"
            ),
            context_lineage=loaded.spec.context.lineage if loaded else None,
            context_revision=loaded.spec.context.revision if loaded else None,
            context_definition_digest=(context_definition_digest(loaded) if loaded else None),
            trigger="schedule",
            trigger_id=schedule.id,
        )
        store = JobRunStore(self.settings)
        await store.initialize()
        await store.insert(run, scope=schedule.profile_scope)
        return run

    def _settings_for(self, root: Path) -> RickySettings:
        settings = load_settings()
        if user_data_path(settings) != self.store.root:
            raise ScheduleServiceError(
                "scheduled project resolves a different user_data_dir than this schedule store"
            )
        return settings

    def _require_installation_scope(self) -> None:
        missing = sorted(set(self.settings.profiles.enabled) - set(self.profile_scope.profiles))
        if missing:
            raise ScheduleServiceError(
                "schedule installation operations require every enabled profile; missing: "
                + ", ".join(missing)
            )

    @staticmethod
    def _resolve_project(root: Path) -> Path:
        resolved = root.expanduser().resolve()
        if not resolved.is_dir():
            raise ScheduleServiceError(f"project root is not a directory: {resolved}")
        discovered = find_project_root(resolved)
        if discovered != resolved:
            raise ScheduleServiceError(
                f"project path is not a Ricky project root; resolved root is {discovered}"
            )
        return resolved

    def _executable(self) -> Path:
        return (
            self._ricky_executable.resolve()
            if self._ricky_executable
            else resolve_ricky_executable()
        )

    def _rotate_launcher_log(self, schedule_id: str) -> None:
        self.backend.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.backend.log_dir, 0o700)
        path = self.backend.log_dir / f"{schedule_id}.log"
        if path.exists():
            os.chmod(path, 0o600)
            if path.stat().st_size > self.settings.schedules.launcher_log_byte_limit:
                path.write_bytes(b"")

    def _approval_changes(
        self,
        schedule: ScheduleSpec,
        current: LoadedJob,
        project_settings: RickySettings,
    ) -> list[str]:
        previous_path = (
            profile_data_path(
                project_settings,
                schedule.job_name.partition("/")[0],
            )
            / project_settings.jobs.run_dir
            / "specs"
            / schedule.approved_spec_digest
            / "resolved.json"
        )
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ["prior approved snapshot unavailable"]
        current_payload = json.loads(dict(current.source_files)["resolved.json"])
        changes: list[str] = []
        for field in (
            "goal",
            "description",
            "provider",
            "model",
            "context",
            "tools",
            "permissions",
            "workflow",
            "browser",
            "task_sources",
            "stream_sources",
            "budget",
        ):
            old = previous.get(field) if field == "goal" else previous.get("spec", {}).get(field)
            new = (
                current_payload.get(field)
                if field == "goal"
                else current_payload.get("spec", {}).get(field)
            )
            if old != new:
                changes.append(
                    f"{field}: {json.dumps(old, sort_keys=True)[:500]} -> "
                    f"{json.dumps(new, sort_keys=True)[:500]}"
                )
        previous_workflow_digest_path = previous_path.with_name("workflow.digest")
        current_workflow_digest = dict(current.source_files).get("workflow.digest")
        try:
            previous_workflow_digest = previous_workflow_digest_path.read_bytes()
        except OSError:
            previous_workflow_digest = None
        if previous_workflow_digest != current_workflow_digest:
            changes.append("workflow bundle content changed")
        previous_workflow_identity_path = previous_path.with_name("workflow.identity")
        current_workflow_identity = dict(current.source_files).get("workflow.identity")
        try:
            previous_workflow_identity = previous_workflow_identity_path.read_bytes()
        except OSError:
            previous_workflow_identity = None
        if previous_workflow_identity != current_workflow_identity:
            changes.append("resolved workflow identity changed")
        return changes or ["no material authority fields changed"]

    @staticmethod
    def _reapproval_reasons(
        schedule: ScheduleSpec,
        current: JobApprovalEnvelope,
        *,
        revision_changed: bool,
    ) -> list[str]:
        reasons: list[str] = []
        approved_cron = schedule.approved_cron or schedule.cron
        if schedule.cron != approved_cron:
            reasons.append(f"schedule timing changed: {approved_cron} -> {schedule.cron}")
        previous = schedule.approved_authority
        if previous is None:
            if revision_changed:
                reasons.append("prior approval envelope is unavailable")
            return reasons
        if current.provider != previous.provider:
            reasons.append(f"provider changed: {previous.provider} -> {current.provider}")
        for name, tool in current.tools.items():
            approved_tool = previous.tools.get(name)
            if approved_tool is None:
                reasons.append(f"tool added: {name}")
            elif approved_tool != tool:
                reasons.append(f"tool contract changed: {name}")
        for name in sorted(set(current.mutating_tools) - set(previous.mutating_tools)):
            reasons.append(f"standing mutation added: {name}")
        for name, source_scope in current.source_scopes.items():
            approved_scope = previous.source_scopes.get(name)
            if approved_scope is None:
                reasons.append(f"data source added: {name}")
            elif _source_scope_expands(name, approved_scope, source_scope):
                reasons.append(f"data source scope changed: {name}")
        for name, email in current.google_accounts.items():
            approved_email = previous.google_accounts.get(name)
            if approved_email is None:
                reasons.append(f"Google account became available: {name}")
            elif approved_email != email:
                reasons.append(f"Google account identity changed: {name}")
        if current.workflow_args != previous.workflow_args:
            reasons.append("locked workflow arguments changed")
        if current.browser_scope != previous.browser_scope:
            reasons.append("browser resource or disclosure scope changed")
        if current.effect_calls > previous.effect_calls:
            reasons.append(
                f"effect budget increased: {previous.effect_calls} -> {current.effect_calls}"
            )
        return reasons


def _source_scope_expands(name: str, previous_json: str, current_json: str) -> bool:
    """Return whether a changed source selector can expose previously unavailable data."""

    previous = json.loads(previous_json)
    current = json.loads(current_json)
    if name.startswith("stream:"):
        if previous.get("adapter") != current.get("adapter"):
            return True
        if previous.get("channel_id") != current.get("channel_id"):
            return True
        return float(current.get("initial_lookback_hours", 0)) > float(
            previous.get("initial_lookback_hours", 0)
        )
    if not name.startswith("task:"):
        return previous != current
    if _selection_expands(previous.get("tags_any", []), current.get("tags_any", [])):
        return True
    if set(previous.get("tags_all", [])) - set(current.get("tags_all", [])):
        return True
    if set(previous.get("tags_none", [])) - set(current.get("tags_none", [])):
        return True
    for field in ("execution_modes", "statuses", "waiting_on"):
        if _selection_expands(previous.get(field, []), current.get(field, [])):
            return True
    previous_due = previous.get("due_before")
    current_due = current.get("due_before")
    if previous_due is not None and (
        current_due is None or _scope_datetime(current_due) > _scope_datetime(previous_due)
    ):
        return True
    previous_text = previous.get("text")
    current_text = current.get("text")
    return previous_text is not None and current_text != previous_text


def _selection_expands(previous: list[str], current: list[str]) -> bool:
    if not previous:
        return False
    if not current:
        return True
    return bool(set(current) - set(previous))


def _scope_datetime(value: str) -> datetime:
    """Normalize current and legacy source-scope timestamps for safe comparison."""

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
