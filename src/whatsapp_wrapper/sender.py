from __future__ import annotations

import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

from . import core
from .models import Chat, Jid, SendResult


class WhatsAppSender:
    def __init__(
        self,
        *,
        app_name: str = "WhatsApp",
        send_timeout: int = core.DEFAULT_SEND_TIMEOUT_SECONDS,
    ) -> None:
        self.app_name = app_name
        self.send_timeout = send_timeout

    def send(
        self,
        *,
        chat: Chat,
        text: str = "",
        file_paths: Iterable[str | Path] | None = None,
        dry_run: bool = False,
        allow_experimental_group: bool = False,
    ) -> SendResult:
        files = [str(Path(path).expanduser()) for path in (file_paths or [])]
        self._validate_payload(text, files)
        recipient = chat.identifier

        if dry_run:
            return SendResult(
                recipient=recipient,
                text=text,
                file_paths=files,
                sent=False,
                verified=None,
                delivery_status="dry_run",
                dry_run=True,
                chat_id=chat.id if chat.id > 0 else None,
            )

        if platform.system() != "Darwin":
            raise core.WhatsAppError("WhatsApp send automation is only supported on macOS")

        direct_text_jid: Jid | None = None
        direct_file_jid: Jid | None = None
        if chat.kind == "group":
            if not allow_experimental_group:
                raise core.WhatsAppError("group sends require allow_experimental_group=True")
            self._open_group_chat(chat)
        else:
            jid = chat.jid
            if not jid or not jid.phone:
                raise core.WhatsAppError("direct sends require a phone JID or phone number")
            self._open_direct_chat(jid)
            if text and not files:
                direct_text_jid = jid
            if files:
                direct_file_jid = jid

        self._wait_for_app()
        try:
            self._assert_focused_chat(chat)
        except core.WhatsAppError:
            if direct_text_jid is None and direct_file_jid is None:
                raise
        self._clear_reply_context()

        if direct_text_jid is not None:
            self._open_direct_chat(direct_text_jid, text)
            self._wait_for_prefilled_text()
            self._press_return()
        elif files:
            if direct_file_jid is not None:
                self._open_direct_chat(direct_file_jid)
                self._wait_for_direct_chat()
            self._focus_composer_area()
            self._paste_files(files)
            self._wait_for_attachment_preview()
            if text:
                self._paste_text(text)
                self._wait_for_caption_text()
            self._press_return()
        else:
            self._paste_text(text)
            self._press_return()

        return SendResult(
            recipient=recipient,
            text=text,
            file_paths=files,
            sent=True,
            verified=None,
            delivery_status="sent_unverified",
            dry_run=False,
            chat_id=chat.id if chat.id > 0 else None,
        )

    def _validate_payload(self, text: str, file_paths: list[str]) -> None:
        if not text and not file_paths:
            raise ValueError("text or at least one file path is required")
        for path in file_paths:
            candidate = Path(path).expanduser()
            if not candidate.exists():
                raise FileNotFoundError(str(candidate))
            if not candidate.is_file():
                raise core.WhatsAppError(f"attachment is not a file: {candidate}")

    def _open_direct_chat(self, jid: Jid, text: str = "") -> None:
        if not jid.phone:
            raise core.WhatsAppError("click-to-chat requires a phone JID")
        url = f"whatsapp://send?phone={quote(jid.phone)}"
        if text:
            url += f"&text={quote(text)}"
        self._open_url(url)

    def _open_group_chat(self, chat: Chat) -> None:
        self._run_osascript(
            [
                f'tell application "{self.app_name}" to activate',
                'tell application "System Events"',
                f'  tell process "{self.app_name}"',
                "    keystroke \"f\" using command down",
                f"    keystroke {self._osascript_string(chat.display_name or chat.name)}",
                "    delay 0.5",
                "    key code 36",
                "  end tell",
                "end tell",
            ]
        )

    def _open_url(self, url: str) -> None:
        subprocess.run(["/usr/bin/open", "-g", url], check=True, timeout=self.send_timeout)

    def _wait_for_app(self) -> None:
        deadline = time.monotonic() + self.send_timeout
        while time.monotonic() < deadline:
            result = subprocess.run(
                ["/usr/bin/pgrep", "-x", self.app_name],
                text=True,
                capture_output=True,
                check=False,
                timeout=2,
            )
            if result.returncode == 0:
                return
            time.sleep(0.25)
        raise core.WhatsAppError("timed out waiting for WhatsApp.app")

    def _assert_focused_chat(self, chat: Chat) -> None:
        candidates = self._expected_focus_tokens(chat)
        if not candidates:
            raise core.WhatsAppError("cannot AX-confirm chat without a name or phone token")
        script = [
            'tell application "System Events"',
            f'  tell process "{self.app_name}"',
            "    if not (exists window 1) then error \"WhatsApp has no front window\"",
            "    set collectedText to \"\"",
            "    try",
            "      set collectedText to collectedText & (value of static texts of window 1 as text)",
            "    end try",
            "    try",
            "      set collectedText to collectedText & \" \" & (name of buttons of window 1 as text)",
            "    end try",
            "    return collectedText",
            "  end tell",
            "end tell",
        ]
        visible_text = self._run_osascript(script).casefold()
        if not any(token.casefold() in visible_text for token in candidates):
            raise core.WhatsAppError("AX confirmation failed; focused WhatsApp chat did not match target")

    def _expected_focus_tokens(self, chat: Chat) -> list[str]:
        tokens = [chat.display_name or "", chat.name or ""]
        if chat.jid:
            if chat.jid.phone:
                tokens.extend([chat.jid.phone, f"+{chat.jid.phone}"])
            tokens.append(chat.jid.raw)
        return [token for token in dict.fromkeys(tokens) if token]

    def _paste_files(self, file_paths: list[str]) -> None:
        try:
            from AppKit import NSPasteboard, NSURL
        except ImportError as exc:
            raise core.WhatsAppError("file sends require PyObjC AppKit on macOS") from exc
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()
        urls = [NSURL.fileURLWithPath_(str(Path(path).expanduser())) for path in file_paths]
        if not pasteboard.writeObjects_(urls):
            raise core.WhatsAppError("failed to write attachment URLs to the pasteboard")
        self._run_osascript(
            [
                'tell application "System Events"',
                f'  tell process "{self.app_name}"',
                "    keystroke \"v\" using command down",
                "  end tell",
                "end tell",
            ]
        )

    def _paste_text(self, text: str) -> None:
        escaped = self._osascript_string(text)
        self._run_osascript(
            [
                "set the clipboard to " + escaped,
                'tell application "System Events"',
                f'  tell process "{self.app_name}"',
                "    keystroke \"v\" using command down",
                "  end tell",
                "end tell",
            ]
        )

    def _clear_reply_context(self) -> None:
        self._run_osascript(
            [
                'tell application "System Events"',
                f'  tell process "{self.app_name}"',
                "    key code 53",
                "    delay 0.1",
                "  end tell",
                "end tell",
            ]
        )

    def _wait_for_prefilled_text(self) -> None:
        time.sleep(2.0)

    def _wait_for_direct_chat(self) -> None:
        time.sleep(1.0)

    def _wait_for_attachment_preview(self) -> None:
        time.sleep(2.0)

    def _wait_for_caption_text(self) -> None:
        time.sleep(0.5)

    def _focus_composer_area(self) -> None:
        left, top, width, height = self._window_position_and_size()
        if width < 200 or height < 120:
            raise core.WhatsAppError("WhatsApp window is too small to focus the composer safely")
        self._click_screen_point(left + (width / 2), top + height - 35)
        time.sleep(0.2)

    def _press_return(self) -> None:
        self._run_osascript(
            [
                'tell application "System Events"',
                f'  tell process "{self.app_name}"',
                "    key code 36",
                "  end tell",
                "end tell",
            ]
        )

    def _window_position_and_size(self) -> tuple[float, float, float, float]:
        output = self._run_osascript(
            [
                f'tell application "{self.app_name}" to activate',
                'tell application "System Events"',
                f'  tell process "{self.app_name}"',
                "    if not (exists window 1) then error \"WhatsApp has no front window\"",
                "    set windowPosition to position of window 1",
                "    set windowSize to size of window 1",
                "    return (item 1 of windowPosition as text) & \",\" & (item 2 of windowPosition as text) & \",\" & (item 1 of windowSize as text) & \",\" & (item 2 of windowSize as text)",
                "  end tell",
                "end tell",
            ]
        )
        try:
            left, top, width, height = [float(part.strip()) for part in output.split(",")]
        except ValueError as exc:
            raise core.WhatsAppError(f"could not read WhatsApp window bounds: {output}") from exc
        return left, top, width, height

    def _click_screen_point(self, x: float, y: float) -> None:
        self._run_jxa(
            [
                'ObjC.import("ApplicationServices");',
                f"const point = $.CGPointMake({float(x):.2f}, {float(y):.2f});",
                "const down = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseDown, point, $.kCGMouseButtonLeft);",
                "const up = $.CGEventCreateMouseEvent(null, $.kCGEventLeftMouseUp, point, $.kCGMouseButtonLeft);",
                "$.CGEventPost($.kCGHIDEventTap, down);",
                "$.CGEventPost($.kCGHIDEventTap, up);",
            ]
        )

    def _run_jxa(self, lines: list[str]) -> str:
        script = "\n".join(lines)
        result = subprocess.run(
            ["/usr/bin/osascript", "-l", "JavaScript", "-e", script],
            text=True,
            capture_output=True,
            timeout=self.send_timeout,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or "JXA osascript failed"
            raise core.WhatsAppError(detail)
        return result.stdout.strip()

    def _run_osascript(self, lines: list[str]) -> str:
        script = "\n".join(lines)
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            text=True,
            capture_output=True,
            timeout=self.send_timeout,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or "osascript failed"
            raise core.WhatsAppError(detail)
        return result.stdout.strip()

    @staticmethod
    def _osascript_string(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n")
        return f'"{escaped}"'
