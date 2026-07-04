from __future__ import annotations

import os
import re
import shutil
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .models import Jid

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
DEFAULT_SEND_TIMEOUT_SECONDS = 15
DEFAULT_VERIFICATION_TIMEOUT_SECONDS = 10.0


class WhatsAppError(RuntimeError):
    pass


def host_home() -> Path:
    return Path(os.environ.get("WHATSAPP_WRAPPER_HOST_HOME", str(Path.home()))).expanduser()


def candidate_data_roots(home: str | Path | None = None) -> list[Path]:
    root = Path(home).expanduser() if home else host_home()
    return [
        root / "Library" / "Group Containers" / "group.net.whatsapp.WhatsApp.shared",
        root / "Library" / "Containers" / "net.whatsapp.WhatsApp" / "Data" / "Library" / "Application Support" / "WhatsApp",
        root / "Library" / "Containers" / "WhatsApp" / "Data" / "Library" / "Application Support" / "WhatsApp",
        root / "Library" / "Application Support" / "WhatsApp",
    ]


def discover_data_root(home: str | Path | None = None) -> Path:
    roots = candidate_data_roots(home)
    for root in roots:
        if (root / "ChatStorage.sqlite").exists():
            return root
    return roots[0]


def default_chat_db_path(home: str | Path | None = None) -> Path:
    return discover_data_root(home) / "ChatStorage.sqlite"


def default_contacts_db_path(home: str | Path | None = None) -> Path:
    return discover_data_root(home) / "ContactsV2.sqlite"


def default_lid_db_path(home: str | Path | None = None) -> Path:
    return discover_data_root(home) / "LID.sqlite"


def default_media_root(home: str | Path | None = None) -> Path:
    return discover_data_root(home) / "Message"


def open_readonly(path: str | Path, *, timeout: float = 5.0) -> sqlite3.Connection:
    db_path = Path(path).expanduser()
    if not db_path.exists():
        raise WhatsAppError(f"SQLite database does not exist: {db_path}")
    uri = f"file:{quote(str(db_path), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    register_lookup_functions(conn)
    return conn


def snapshot_sqlite(path: str | Path, destination_dir: str | Path) -> Path:
    src = Path(path).expanduser()
    dest_dir = Path(destination_dir).expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    shutil.copy2(src, dest)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(src) + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, Path(str(dest) + suffix))
    return dest


def whatsapp_timestamp_to_datetime(value: Any) -> datetime | None:
    if value in (None, "", 0, "0"):
        return None
    raw = float(value)
    absolute = abs(raw)
    if absolute > 10**15:
        seconds = raw / 1_000_000_000
    elif absolute > 10**12:
        seconds = raw / 1_000_000
    else:
        seconds = raw
    return APPLE_EPOCH + timedelta(seconds=seconds)


def datetime_to_whatsapp_timestamp(value: datetime) -> float:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return (aware.astimezone(timezone.utc) - APPLE_EPOCH).total_seconds()


def parse_jid(value: str | Jid | None) -> Jid | None:
    return Jid.parse(value)


def normalize_phone(value: str | None, region: str = "US") -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        import phonenumbers
    except ImportError:
        digits = re.sub(r"\D+", "", text)
        return digits or None
    try:
        parsed = phonenumbers.parse(text, region)
        if phonenumbers.is_possible_number(parsed):
            return str(parsed.country_code) + str(parsed.national_number)
    except Exception:
        pass
    digits = re.sub(r"\D+", "", text)
    return digits or None


def _normalize_lookup_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _compact_lookup_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def query_lookup_terms(value: str) -> list[str]:
    normalized = _normalize_lookup_text(value)
    if not normalized:
        return []
    terms = normalized.split()
    compact = _compact_lookup_text(value)
    if compact and compact not in terms:
        terms.append(compact)
    return terms


def register_lookup_functions(conn: sqlite3.Connection) -> None:
    conn.create_function("whatsapp_lookup_normalize", 1, _normalize_lookup_text)
    conn.create_function("whatsapp_lookup_compact", 1, _compact_lookup_text)


def lookup_match_score(query: str, values: list[Any]) -> int | None:
    query_normalized = _normalize_lookup_text(query)
    query_compact = _compact_lookup_text(query)
    query_tokens = [token for token in query_normalized.split() if token]
    if not query_normalized and not query_compact:
        return None

    normalized_values = [_normalize_lookup_text(value) for value in values if str(value or "").strip()]
    compact_values = [_compact_lookup_text(value) for value in values if str(value or "").strip()]
    if not normalized_values and not compact_values:
        return None

    combined_normalized = " ".join(item for item in normalized_values if item)
    combined_compact = " ".join(item for item in compact_values if item)
    combined_tokens = {token for item in normalized_values for token in item.split() if token}

    score: int | None = None
    if query_normalized and any(item == query_normalized for item in normalized_values):
        score = 1000
    elif query_compact and any(item == query_compact for item in compact_values):
        score = 980
    elif query_normalized and any(item.startswith(query_normalized) for item in normalized_values):
        score = 920
    elif query_normalized and query_normalized in combined_normalized:
        score = 860
    elif query_compact and query_compact in combined_compact:
        score = 840

    if query_tokens:
        exact_hits = sum(1 for token in query_tokens if token in combined_tokens)
        prefix_hits = sum(1 for token in query_tokens if any(candidate.startswith(token) for candidate in combined_tokens))
        substring_hits = sum(1 for token in query_tokens if token in combined_normalized)
        token_count = len(query_tokens)
        if exact_hits == token_count:
            score = max(score or 0, 760 + token_count)
        elif prefix_hits == token_count:
            score = max(score or 0, 720 + token_count)
        elif substring_hits == token_count:
            score = max(score or 0, 680 + token_count)
        elif exact_hits:
            score = max(score or 0, 520 + exact_hits)
        elif prefix_hits:
            score = max(score or 0, 460 + prefix_hits)
        elif substring_hits:
            score = max(score or 0, 420 + substring_hits)
    return score


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def coerce_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


WHATSAPP_MESSAGE_TYPE_NAMES: dict[int, str] = {
    0: "text",
    1: "image",
    2: "video",
    3: "voice_message",
    4: "contact_card",
    5: "location",
    6: "group_event",
    7: "url",
    8: "file",
    10: "system_information",
    28: "disappearing_messages_notice",
    59: "video_call",
}


WHATSAPP_MESSAGE_TYPE_DISPLAY_TEXT: dict[int, str] = {
    1: "Image",
    2: "Video",
    3: "Voice message",
    4: "Contact card",
    5: "Location",
    6: "Group event",
    7: "Link",
    8: "File",
    10: "System information message",
    28: "Disappearing messages setting changed",
    59: "Video call",
}


def whatsapp_message_type_name(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        raw_type = int(value)
    except (TypeError, ValueError):
        return str(value)
    return WHATSAPP_MESSAGE_TYPE_NAMES.get(raw_type, f"type_{raw_type}")


def whatsapp_message_display_text(raw_type: Any, text: str | None) -> str:
    body = str(text or "")
    if body:
        return body
    try:
        message_type = int(raw_type)
    except (TypeError, ValueError):
        return body
    return WHATSAPP_MESSAGE_TYPE_DISPLAY_TEXT.get(message_type, body)


WHATSAPP_SEND_TEXT_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    (":)", "🙂"),
    (":-)", "🙂"),
)


def normalize_sent_text_for_verification(value: str | None) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for before, after in WHATSAPP_SEND_TEXT_REPLACEMENTS:
        text = text.replace(before, after)
    return text


def sent_text_matches(expected: str | None, actual: str | None) -> bool:
    expected_text = str(expected or "")
    if not expected_text:
        return True
    actual_text = str(actual or "")
    if expected_text == actual_text:
        return True
    return normalize_sent_text_for_verification(expected_text) == normalize_sent_text_for_verification(actual_text)


def safe_resolve_media_path(raw_path: str | None, media_root: str | Path) -> str | None:
    if not raw_path:
        return None
    root = Path(media_root).expanduser().resolve(strict=False)
    path = Path(str(raw_path).replace("file://", "")).expanduser()
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    return str(resolved)
