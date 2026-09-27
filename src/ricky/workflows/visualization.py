"""Offline HTML rendering and scope-confined workflow design exports."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path

from ricky.browser.chrome import ChromeDiscoveryError, chrome_environment, require_chrome
from ricky.config import RickySettings, user_data_subpath
from ricky.profiles import ProfileScope
from ricky.workflows.inspection import WorkflowInspection


def render_html(view: WorkflowInspection) -> str:
    """Embed escaped JSON into the packaged, dependency-free viewer."""
    assets = Path(__file__).with_name("assets")
    payload = (
        view.model_dump_json()
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )
    return (
        (assets / "workflow.html")
        .read_text(encoding="utf-8")
        .replace("/* WORKFLOW_CSS */", (assets / "workflow.css").read_text(encoding="utf-8"))
        .replace("/* WORKFLOW_JS */", (assets / "workflow.js").read_text(encoding="utf-8"))
        .replace("WORKFLOW_DATA", payload)
    )


def visualization_root(settings: RickySettings, scope: ProfileScope) -> Path:
    """Partition exports by the complete read scope, including attached skill sources."""
    scope_key = hashlib.sha256("\n".join(sorted(scope.profiles)).encode()).hexdigest()
    base = user_data_subpath(settings, settings.workflow.visualization_dir)
    root = base / scope_key
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("Workflow visualization scope directory must not be a symlink")
    return root


def export_visualization(
    view: WorkflowInspection, *, settings: RickySettings, scope: ProfileScope
) -> Path:
    if set(view.profiles) != set(scope.profiles):
        raise ValueError("Workflow visualization scope does not match the current scope")
    content = render_html(view).encode("utf-8")
    root = visualization_root(settings, scope)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / (hashlib.sha256(content).hexdigest() + ".html")
    with tempfile.NamedTemporaryFile(dir=root, prefix=".view-", delete=False) as output:
        temporary = Path(output.name)
        try:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                validate_visualization_path(path, settings=settings, scope=scope)
        finally:
            temporary.unlink(missing_ok=True)
    return path


def validate_visualization_path(
    path: Path, *, settings: RickySettings, scope: ProfileScope
) -> Path:
    """Accept only intact exports within this exact scope, never arbitrary HTML."""
    root = visualization_root(settings, scope)
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError("Workflow visualization is outside the current profile scope")
    if re.fullmatch(r"[0-9a-f]{64}\.html", path.name) is None or not path.is_file():
        raise ValueError("Not a workflow visualization export")
    if hashlib.sha256(path.read_bytes()).hexdigest() != path.stem:
        raise ValueError("Workflow visualization changed; render it again")
    return path.resolve()


async def open_visualization(path: Path, *, settings: RickySettings, scope: ProfileScope) -> bool:
    """Hand an exported file to ordinary desktop Chrome, outside managed automation."""
    path = validate_visualization_path(path, settings=settings, scope=scope)
    try:
        executable = await require_chrome(settings)
    except ChromeDiscoveryError as exc:
        raise ValueError(str(exc)) from exc
    # Desktop Chrome owns the window after launch. It must not retain the shell
    # tool's output pipes: communicate() would wait for EOF until Chrome closes.
    try:
        process = subprocess.Popen(
            [str(executable), path.as_uri()],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
            env=chrome_environment(),
        )
    except OSError:
        return False
    # Chrome may exit successfully after handing the URL to an existing instance.
    return process.poll() in (None, 0)
