from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


_PHONE_JID_RE = re.compile(r"^(?P<phone>\d+)@(?:s|c)\.whatsapp\.net$", re.IGNORECASE)
_LID_JID_RE = re.compile(r"^(?P<lid>[0-9A-Za-z_.-]+)@lid$", re.IGNORECASE)
_GROUP_JID_RE = re.compile(r"^(?P<group>[0-9A-Za-z_.-]+)@g\.us$", re.IGNORECASE)


@dataclass(frozen=True)
class Jid:
    raw: str
    kind: str = "unknown"
    phone: str | None = None
    lid: str | None = None
    user: str | None = None
    server: str | None = None

    @classmethod
    def parse(cls, value: str | "Jid" | None) -> "Jid | None":
        if isinstance(value, Jid):
            return value
        raw = str(value or "").strip()
        if not raw:
            return None
        if "@" not in raw and raw.lstrip("+").isdigit():
            digits = raw.lstrip("+")
            return cls(raw=f"{digits}@s.whatsapp.net", kind="phone", phone=digits, user=digits, server="s.whatsapp.net")

        lowered = raw.lower()
        user, _, server = lowered.partition("@")
        if match := _PHONE_JID_RE.match(lowered):
            phone = match.group("phone")
            return cls(raw=lowered, kind="phone", phone=phone, user=phone, server=server)
        if match := _LID_JID_RE.match(lowered):
            lid = match.group("lid")
            return cls(raw=lowered, kind="lid", lid=lid, user=lid, server=server)
        if _GROUP_JID_RE.match(lowered):
            return cls(raw=lowered, kind="group", user=user or None, server=server or None)
        if lowered == "status@broadcast":
            return cls(raw=lowered, kind="status", user=user or None, server=server or None)
        if lowered.endswith("@broadcast"):
            return cls(raw=lowered, kind="broadcast", user=user or None, server=server or None)
        return cls(raw=lowered, kind="unknown", user=user or None, server=server or None)

    def equivalent_keys(self) -> tuple[str, ...]:
        keys = [self.raw]
        if self.phone:
            keys.extend((self.phone, f"{self.phone}@s.whatsapp.net", f"+{self.phone}"))
        if self.lid:
            keys.extend((self.lid, f"{self.lid}@lid"))
        return tuple(dict.fromkeys(keys))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Contact:
    id: str
    display_name: str
    jid: Jid | None = None
    phone: str | None = None
    lid: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    nickname: str | None = None
    organization: str | None = None
    raw_jids: list[Jid] = field(default_factory=list)
    source_db_path: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["jid"] = self.jid.to_dict() if self.jid else None
        data["raw_jids"] = [jid.to_dict() for jid in self.raw_jids]
        return data


@dataclass(frozen=True)
class Attachment:
    id: int | None = None
    message_id: int | None = None
    filename: str | None = None
    path: str | None = None
    mime_type: str | None = None
    byte_size: int | None = None
    media_kind: str | None = None
    missing: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Chat:
    id: int
    jid: Jid | None
    name: str
    display_name: str | None = None
    kind: str = "direct"
    unread_count: int = 0
    is_archived: bool = False
    is_hidden: bool = False
    participants: list[Jid] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)
    last_message_at: datetime | None = None
    message_count: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def identifier(self) -> str:
        return self.jid.raw if self.jid else str(self.id)

    @property
    def is_group(self) -> bool:
        return self.kind == "group"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["jid"] = self.jid.to_dict() if self.jid else None
        data["participants"] = [jid.to_dict() for jid in self.participants]
        data["contacts"] = [contact.to_dict() for contact in self.contacts]
        data["last_message_at"] = self.last_message_at.isoformat() if self.last_message_at else None
        data["identifier"] = self.identifier
        data["is_group"] = self.is_group
        return data


@dataclass(frozen=True)
class Message:
    id: int
    chat_id: int
    stanza_id: str | None
    sender: str | None
    text: str
    created_at: datetime | None
    is_from_me: bool
    sender_jid: Jid | None = None
    chat_jid: Jid | None = None
    chat_name: str | None = None
    raw_type: int | str | None = None
    is_starred: bool = False
    is_deleted: bool = False
    contact: Contact | None = None
    attachments: list[Attachment] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat() if self.created_at else None
        data["sender_jid"] = self.sender_jid.to_dict() if self.sender_jid else None
        data["chat_jid"] = self.chat_jid.to_dict() if self.chat_jid else None
        data["contact"] = self.contact.to_dict() if self.contact else None
        data["attachments"] = [attachment.to_dict() for attachment in self.attachments]
        return data


@dataclass(frozen=True)
class SendResult:
    recipient: str
    text: str = ""
    file_paths: list[str] = field(default_factory=list)
    sent: bool = False
    verified: bool | None = None
    delivery_status: str | None = None
    dry_run: bool = False
    chat_id: int | None = None
    message_id: int | None = None
    stanza_id: str | None = None
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
