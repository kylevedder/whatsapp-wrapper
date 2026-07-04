from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PermissionStatus:
    name: str
    ok: bool
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_whatsapp_app(app_path: str | Path | None = None) -> PermissionStatus:
    if app_path and Path(app_path).expanduser().exists():
        return PermissionStatus("whatsapp_app", True, str(Path(app_path).expanduser()))
    candidates = [
        Path("/Applications/WhatsApp.app"),
        Path.home() / "Applications" / "WhatsApp.app",
    ]
    for candidate in candidates:
        if candidate.exists():
            return PermissionStatus("whatsapp_app", True, str(candidate))
    if shutil.which("open"):
        return PermissionStatus("whatsapp_app", True, "open can resolve WhatsApp.app if installed")
    return PermissionStatus("whatsapp_app", False, "WhatsApp.app was not found")


def data_access_status(path: str | Path) -> PermissionStatus:
    db_path = Path(path).expanduser()
    if not db_path.exists():
        return PermissionStatus("data_access", False, f"missing: {db_path}")
    if not db_path.is_file():
        return PermissionStatus("data_access", False, f"not a file: {db_path}")
    try:
        with db_path.open("rb"):
            pass
    except PermissionError:
        return PermissionStatus("data_access", False, "permission denied; grant Full Disk Access to the calling app")
    return PermissionStatus("data_access", True, str(db_path))


def accessibility_status() -> PermissionStatus:
    if platform.system() != "Darwin":
        return PermissionStatus("accessibility", False, "send automation is macOS-only")
    if not shutil.which("osascript"):
        return PermissionStatus("accessibility", False, "osascript is unavailable")
    script = 'tell application "System Events" to count processes'
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", script], text=True, capture_output=True, timeout=5, check=False)
    except Exception as exc:
        return PermissionStatus("accessibility", False, str(exc))
    if result.returncode == 0:
        return PermissionStatus("accessibility", True, "System Events is scriptable")
    detail = (result.stderr or result.stdout or "").strip() or "System Events automation failed"
    return PermissionStatus("accessibility", False, detail)


def automation_status() -> PermissionStatus:
    if platform.system() != "Darwin":
        return PermissionStatus("automation", False, "send automation is macOS-only")
    if not shutil.which("osascript"):
        return PermissionStatus("automation", False, "osascript is unavailable")
    script = 'tell application "WhatsApp" to get name'
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", script], text=True, capture_output=True, timeout=5, check=False)
    except Exception as exc:
        return PermissionStatus("automation", False, str(exc))
    if result.returncode == 0:
        return PermissionStatus("automation", True, "WhatsApp is scriptable")
    detail = (result.stderr or result.stdout or "").strip() or "WhatsApp automation probe failed"
    return PermissionStatus("automation", False, detail)


def diagnostics(chat_db_path: str | Path, app_path: str | Path | None = None) -> list[PermissionStatus]:
    return [
        find_whatsapp_app(app_path),
        data_access_status(chat_db_path),
        accessibility_status(),
        automation_status(),
    ]

