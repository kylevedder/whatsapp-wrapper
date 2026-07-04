from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from whatsapp_wrapper import Chat, Jid, SendResult, WhatsAppClient, WhatsAppError
from whatsapp_wrapper.core import datetime_to_whatsapp_timestamp, sent_text_matches
from whatsapp_wrapper.sender import WhatsAppSender


class RecordingSender(WhatsAppSender):
    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    def _open_direct_chat(self, jid, text=""):
        self.calls.append(f"open_direct:{jid.raw}:{text}")

    def _open_group_chat(self, chat):
        self.calls.append(f"open_group:{chat.id}")

    def _wait_for_app(self):
        self.calls.append("wait")

    def _assert_focused_chat(self, chat):
        self.calls.append(f"ax:{chat.id}")

    def _paste_files(self, file_paths):
        self.calls.append("paste_files:" + ",".join(Path(path).name for path in file_paths))

    def _paste_text(self, text):
        self.calls.append(f"paste_text:{text}")

    def _clear_reply_context(self):
        self.calls.append("clear_reply")

    def _press_return(self):
        self.calls.append("return")


def test_direct_text_send_requires_ax_before_return(monkeypatch):
    monkeypatch.setattr("whatsapp_wrapper.sender.platform.system", lambda: "Darwin")
    sender = RecordingSender()
    chat = Chat(id=1, jid=Jid.parse("15550100001@s.whatsapp.net"), name="Alex Example", display_name="Alex Example")

    result = sender.send(chat=chat, text="hello", dry_run=False)

    assert result.sent is True
    assert sender.calls == [
        "open_direct:15550100001@s.whatsapp.net:",
        "wait",
        "ax:1",
        "clear_reply",
        "open_direct:15550100001@s.whatsapp.net:hello",
        "return",
    ]
    assert sender.calls.index("ax:1") < sender.calls.index("return")
    assert sender.calls.index("clear_reply") < sender.calls.index("open_direct:15550100001@s.whatsapp.net:hello")
    assert sender.calls.index("open_direct:15550100001@s.whatsapp.net:hello") < sender.calls.index("return")


def test_direct_text_send_can_continue_when_ax_tree_is_opaque(monkeypatch):
    class OpaqueDirectTextSender(RecordingSender):
        def _assert_focused_chat(self, chat):
            self.calls.append(f"ax_fail:{chat.id}")
            raise WhatsAppError("AX confirmation failed; focused WhatsApp chat did not match target")

    monkeypatch.setattr("whatsapp_wrapper.sender.platform.system", lambda: "Darwin")
    sender = OpaqueDirectTextSender()
    chat = Chat(id=1, jid=Jid.parse("15550100001@s.whatsapp.net"), name="Alex Example", display_name="Alex Example")

    result = sender.send(chat=chat, text="hello", dry_run=False)

    assert result.sent is True
    assert sender.calls == [
        "open_direct:15550100001@s.whatsapp.net:",
        "wait",
        "ax_fail:1",
        "clear_reply",
        "open_direct:15550100001@s.whatsapp.net:hello",
        "return",
    ]


def test_file_send_pastes_after_ax_confirmation(monkeypatch, tmp_path):
    monkeypatch.setattr("whatsapp_wrapper.sender.platform.system", lambda: "Darwin")
    file_path = tmp_path / "note.txt"
    file_path.write_text("fake attachment")
    sender = RecordingSender()
    chat = Chat(id=1, jid=Jid.parse("15550100001@s.whatsapp.net"), name="Alex Example", display_name="Alex Example")

    sender.send(chat=chat, text="caption", file_paths=[file_path], dry_run=False)

    assert sender.calls == [
        "open_direct:15550100001@s.whatsapp.net:",
        "wait",
        "ax:1",
        "clear_reply",
        "paste_files:note.txt",
        "paste_text:caption",
        "return",
    ]


def test_file_send_does_not_continue_when_ax_tree_is_opaque(monkeypatch, tmp_path):
    class OpaqueFileSender(RecordingSender):
        def _assert_focused_chat(self, chat):
            self.calls.append(f"ax_fail:{chat.id}")
            raise WhatsAppError("AX confirmation failed; focused WhatsApp chat did not match target")

    monkeypatch.setattr("whatsapp_wrapper.sender.platform.system", lambda: "Darwin")
    file_path = tmp_path / "note.txt"
    file_path.write_text("fake attachment")
    sender = OpaqueFileSender()
    chat = Chat(id=1, jid=Jid.parse("15550100001@s.whatsapp.net"), name="Alex Example", display_name="Alex Example")

    with pytest.raises(WhatsAppError):
        sender.send(chat=chat, text="caption", file_paths=[file_path], dry_run=False)

    assert sender.calls == ["open_direct:15550100001@s.whatsapp.net:", "wait", "ax_fail:1"]


def test_group_send_requires_experimental_flag(monkeypatch):
    monkeypatch.setattr("whatsapp_wrapper.sender.platform.system", lambda: "Darwin")
    sender = RecordingSender()
    chat = Chat(id=2, jid=Jid.parse("120363000000000001@g.us"), name="Project Group", display_name="Project Group", kind="group")

    with pytest.raises(WhatsAppError):
        sender.send(chat=chat, text="hello")

    sender.send(chat=chat, text="hello", allow_experimental_group=True)
    assert sender.calls == ["open_group:2", "wait", "ax:2", "clear_reply", "paste_text:hello", "return"]


def test_client_dry_run_uses_sender_without_verification(tmp_path):
    data_root = _send_fixture(tmp_path)
    sender = RecordingSender()
    client = WhatsAppClient(data_root=data_root, sender=sender)

    result = client.send(chat_id=1, text="preview", dry_run=True)

    assert result.dry_run is True
    assert result.delivery_status == "dry_run"
    assert sender.calls == []


def test_direct_phone_dry_run_does_not_require_local_database(tmp_path):
    client = WhatsAppClient(data_root=tmp_path / "missing-whatsapp-data")

    result = client.send(to="+1 (555) 010-0001", text="preview", dry_run=True)

    assert result.dry_run is True
    assert result.recipient == "15550100001@s.whatsapp.net"


def test_direct_phone_send_degrades_to_unverified_without_database(tmp_path):
    class SentSender:
        def send(self, *, chat, text, file_paths, dry_run, allow_experimental_group):
            return SendResult(recipient=chat.identifier, text=text, sent=True, verified=None, delivery_status="sent_unverified")

    client = WhatsAppClient(data_root=tmp_path / "missing-whatsapp-data", sender=SentSender(), verification_timeout=0.1)

    result = client.send(to="+1 (555) 010-0001", text="sent", verify=True)

    assert result.sent is True
    assert result.verified is False
    assert result.delivery_status == "sent_unverified"
    assert result.error and result.error.startswith("verification unavailable:")


def test_phone_send_uses_existing_lid_chat_name_for_confirmation(tmp_path):
    data_root = _send_fixture(tmp_path)
    _add_contacts_and_lid_chat(data_root)
    sender = RecordingSender()
    client = WhatsAppClient(data_root=data_root, sender=sender)

    result = client.send(to="+1 (555) 010-0002", text="preview", dry_run=True)

    assert result.dry_run is True
    assert result.recipient == "15550100002@s.whatsapp.net"
    target = client._resolve_send_chat(to="+1 (555) 010-0002", chat_id=None, jid=None)
    assert target.id == 2
    assert target.display_name == "Alex Example"
    assert target.jid and target.jid.phone == "15550100002"


def test_client_verifies_sent_row_from_database(tmp_path):
    data_root = _send_fixture(tmp_path)

    class VerifyingSender:
        def send(self, *, chat, text, file_paths, dry_run, allow_experimental_group):
            conn = sqlite3.connect(data_root / "ChatStorage.sqlite")
            conn.execute(
                "INSERT INTO ZWAMESSAGE VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (2, 1, None, 1, datetime_to_whatsapp_timestamp(__import__("datetime").datetime(2026, 1, 1, 12, tzinfo=__import__("datetime").timezone.utc)), text, "stanza-verified", 0, 0, 0, None),
            )
            conn.commit()
            conn.close()
            return SendResult(recipient=chat.identifier, text=text, sent=True, verified=None, delivery_status="sent_unverified", chat_id=chat.id)

    client = WhatsAppClient(data_root=data_root, sender=VerifyingSender(), verification_timeout=0.5)

    result = client.send(chat_id=1, text="verified body", verify=True)

    assert result.sent is True
    assert result.verified is True
    assert result.delivery_status == "sent"
    assert result.message_id == 2
    assert result.stanza_id == "stanza-verified"


def test_client_verifies_sent_row_after_whatsapp_smiley_normalization(tmp_path):
    data_root = _send_fixture(tmp_path)

    class SmileyNormalizingSender:
        def send(self, *, chat, text, file_paths, dry_run, allow_experimental_group):
            conn = sqlite3.connect(data_root / "ChatStorage.sqlite")
            conn.execute(
                "INSERT INTO ZWAMESSAGE VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    2,
                    1,
                    None,
                    1,
                    datetime_to_whatsapp_timestamp(
                        __import__("datetime").datetime(2026, 1, 1, 12, tzinfo=__import__("datetime").timezone.utc)
                    ),
                    text.replace(":)", "🙂"),
                    "stanza-smiley",
                    0,
                    0,
                    0,
                    None,
                ),
            )
            conn.commit()
            conn.close()
            return SendResult(recipient=chat.identifier, text=text, sent=True, verified=None, delivery_status="sent_unverified", chat_id=chat.id)

    client = WhatsAppClient(data_root=data_root, sender=SmileyNormalizingSender(), verification_timeout=0.5)

    result = client.send(chat_id=1, text="nice work :)", verify=True)

    assert result.sent is True
    assert result.verified is True
    assert result.delivery_status == "sent"
    assert result.message_id == 2
    assert result.raw["verified_message"]["text"] == "nice work 🙂"


def test_sent_text_matcher_is_not_fuzzy():
    assert sent_text_matches("nice work :)", "nice work 🙂") is True
    assert sent_text_matches("nice work :)", "nice work :) extra") is False
    assert sent_text_matches("nice work", "not nice work") is False


def _send_fixture(tmp_path):
    data_root = tmp_path / "group.net.whatsapp.WhatsApp.shared"
    data_root.mkdir()
    conn = sqlite3.connect(data_root / "ChatStorage.sqlite")
    conn.executescript(
        """
        CREATE TABLE ZWACHATSESSION (
            Z_PK INTEGER PRIMARY KEY,
            ZCONTACTJID TEXT,
            ZPARTNERNAME TEXT,
            ZLASTMESSAGEDATE REAL,
            ZUNREADCOUNT INTEGER,
            ZARCHIVED INTEGER,
            ZHIDDEN INTEGER,
            ZSESSIONTYPE INTEGER
        );
        CREATE TABLE ZWAMESSAGE (
            Z_PK INTEGER PRIMARY KEY,
            ZCHATSESSION INTEGER,
            ZFROMJID TEXT,
            ZISFROMME INTEGER,
            ZMESSAGEDATE REAL,
            ZTEXT TEXT,
            ZSTANZAID TEXT,
            ZMESSAGETYPE INTEGER,
            ZSTARRED INTEGER,
            ZDELETED INTEGER,
            ZMEDIAITEM INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO ZWACHATSESSION VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "15550100001@s.whatsapp.net", "Alex Example", 0, 0, 0, 0, 1),
    )
    conn.execute(
        "INSERT INTO ZWAMESSAGE VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, 1, "15550100001@s.whatsapp.net", 0, 0, "before", "stanza-before", 0, 0, 0, None),
    )
    conn.commit()
    conn.close()
    return data_root


def _add_contacts_and_lid_chat(data_root):
    conn = sqlite3.connect(data_root / "ChatStorage.sqlite")
    conn.execute(
        "INSERT INTO ZWACHATSESSION VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (2, "112233@lid", "Alex Example", 0, 0, 0, 0, 1),
    )
    conn.commit()
    conn.close()

    conn = sqlite3.connect(data_root / "ContactsV2.sqlite")
    conn.executescript(
        """
        CREATE TABLE ZWAADDRESSBOOKCONTACT (
            Z_PK INTEGER PRIMARY KEY,
            ZWHATSAPPID TEXT,
            ZPHONENUMBER TEXT,
            ZLID TEXT,
            ZFIRSTNAME TEXT,
            ZLASTNAME TEXT,
            ZFULLNAME TEXT,
            ZNICKNAME TEXT,
            ZORGANIZATION TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (1, "15550100002@s.whatsapp.net", "+1 (555) 010-0002", "112233@lid", "Alex", "Example", "Alex Example", None, None),
    )
    conn.commit()
    conn.close()
