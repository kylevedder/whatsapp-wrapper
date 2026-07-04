from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from whatsapp_wrapper import Jid, WhatsAppClient
from whatsapp_wrapper.core import datetime_to_whatsapp_timestamp


def _ts(year: int, month: int, day: int, hour: int = 12) -> float:
    return datetime_to_whatsapp_timestamp(datetime(year, month, day, hour, tzinfo=timezone.utc))


@pytest.fixture()
def whatsapp_fixture(tmp_path):
    data_root = tmp_path / "group.net.whatsapp.WhatsApp.shared"
    media_root = data_root / "Message"
    media_root.mkdir(parents=True)
    (media_root / "images").mkdir()
    (media_root / "images" / "photo.jpg").write_bytes(b"fake image")

    chat_db = data_root / "ChatStorage.sqlite"
    conn = sqlite3.connect(chat_db)
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
        CREATE TABLE ZWAMEDIAITEM (
            Z_PK INTEGER PRIMARY KEY,
            ZMESSAGE INTEGER,
            ZMEDIAURL TEXT,
            ZFILENAME TEXT,
            ZTITLE TEXT,
            ZMIMETYPE TEXT,
            ZFILESIZE INTEGER,
            ZMEDIATYPE INTEGER
        );
        CREATE TABLE ZWAGROUPMEMBER (
            Z_PK INTEGER PRIMARY KEY,
            ZCHATSESSION INTEGER,
            ZMEMBERJID TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO ZWACHATSESSION VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "15550100001@s.whatsapp.net", "Alex Example", _ts(2026, 1, 2, 10), 2, 0, 0, 1),
            (2, "120363000000000001@g.us", "Project Group", _ts(2026, 1, 2, 11), 0, 1, 0, 2),
        ],
    )
    conn.executemany(
        "INSERT INTO ZWAMESSAGE VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 1, "15550100001@s.whatsapp.net", 0, _ts(2026, 1, 2, 9), "coffee tomorrow?", "stanza-in-1", 0, 0, 0, None),
            (2, 1, None, 1, _ts(2026, 1, 2, 10), "sounds good", "stanza-out-1", 0, 1, 0, 1),
            (3, 2, "15550100002@s.whatsapp.net", 0, _ts(2026, 1, 2, 11), "group update", "stanza-group-1", 0, 0, 0, None),
            (4, 1, "15550100001@s.whatsapp.net", 0, _ts(2026, 1, 2, 12), "deleted secret", "stanza-del-1", 0, 0, 1, None),
        ],
    )
    conn.executemany(
        "INSERT INTO ZWAMEDIAITEM VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, 2, "images/photo.jpg", "photo.jpg", "photo caption", "image/jpeg", 10, 1),
            (2, 2, "../outside.txt", "outside.txt", None, "text/plain", 5, 1),
        ],
    )
    conn.executemany(
        "INSERT INTO ZWAGROUPMEMBER VALUES (?, ?, ?)",
        [
            (1, 2, "15550100001@s.whatsapp.net"),
            (2, 2, "998877@lid"),
        ],
    )
    conn.commit()
    conn.close()

    contacts_db = data_root / "ContactsV2.sqlite"
    conn = sqlite3.connect(contacts_db)
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
    conn.executemany(
        "INSERT INTO ZWAADDRESSBOOKCONTACT VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (1, "15550100001@s.whatsapp.net", "+1 (555) 010-0001", None, "Alex", "Example", "Alex Example", "Lex", "Example Co"),
            (2, "15550100002@s.whatsapp.net", "+1 555 010 0002", "998877@lid", "Blair", "Example", "Blair Example", None, None),
        ],
    )
    conn.commit()
    conn.close()

    lid_db = data_root / "LID.sqlite"
    conn = sqlite3.connect(lid_db)
    conn.executescript(
        """
        CREATE TABLE ZWAPHONENUMBERLIDPAIR (
            Z_PK INTEGER PRIMARY KEY,
            ZPHONENUMBER TEXT,
            ZLID TEXT
        );
        """
    )
    conn.execute("INSERT INTO ZWAPHONENUMBERLIDPAIR VALUES (?, ?, ?)", (1, "+15550100001", "112233@lid"))
    conn.commit()
    conn.close()
    return data_root


def test_jid_parsing_and_equivalence():
    phone = Jid.parse("+15550100001")
    assert phone is not None
    assert phone.raw == "15550100001@s.whatsapp.net"
    assert phone.kind == "phone"
    assert "+15550100001" in phone.equivalent_keys()

    lid = Jid.parse("112233@lid")
    assert lid is not None
    assert lid.kind == "lid"
    assert lid.lid == "112233"

    group = Jid.parse("120363000000000001@g.us")
    assert group is not None
    assert group.kind == "group"


def test_chat_listing_search_and_group_members(whatsapp_fixture):
    client = WhatsAppClient(data_root=whatsapp_fixture, enrich_contacts=False)
    chats = client.chats(limit=10)

    assert [chat.id for chat in chats] == [2, 1]
    assert chats[0].kind == "group"
    assert chats[0].is_archived is True
    assert [jid.raw for jid in chats[0].participants] == ["15550100001@s.whatsapp.net", "998877@lid"]
    assert chats[1].message_count == 3
    assert chats[1].unread_count == 2

    hits = client.search_chats("alex")
    assert len(hits) == 1
    assert hits[0].jid and hits[0].jid.phone == "15550100001"


def test_contacts_v2_and_lid_resolution(whatsapp_fixture):
    client = WhatsAppClient(data_root=whatsapp_fixture)
    contacts = client.contacts()

    alex = client.resolve_contact("112233@lid")
    assert alex is not None
    assert alex.display_name == "Alex Example"
    assert alex.phone == "15550100001"
    assert alex.lid == "112233"

    assert [contact.display_name for contact in client.search_contacts("Example Co", limit=1)] == ["Alex Example"]
    assert {contact.display_name for contact in contacts} == {"Alex Example", "Blair Example"}


def test_messages_filter_deleted_search_and_attachments(whatsapp_fixture):
    client = WhatsAppClient(data_root=whatsapp_fixture)

    messages = client.messages(1, limit=10, attachments=True)
    assert [message.id for message in messages] == [2, 1]
    assert messages[0].is_from_me is True
    assert messages[0].is_starred is True
    assert messages[0].attachments[0].filename == "photo.jpg"
    assert messages[0].attachments[0].caption == "photo caption"
    assert messages[0].attachments[0].path and messages[0].attachments[0].path.endswith("Message/images/photo.jpg")
    assert messages[0].attachments[1].path is None
    assert messages[0].attachments[1].missing is True

    deleted = client.messages(1, limit=10, include_deleted=True)
    assert [message.id for message in deleted] == [4, 2, 1]
    assert deleted[0].is_deleted is True

    hits = client.search_messages("coffee", chat_id=1)
    assert [hit.id for hit in hits] == [1]


def test_message_type_display_text_for_known_non_text_rows(whatsapp_fixture):
    conn = sqlite3.connect(whatsapp_fixture / "ChatStorage.sqlite")
    conn.executemany(
        "INSERT INTO ZWAMESSAGE VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (19, 1, "15550100001@s.whatsapp.net", 0, _ts(2026, 1, 2, 12), None, "stanza-info", 10, 0, 0, None),
            (20, 1, None, 1, _ts(2026, 1, 2, 13), None, "stanza-system", 28, 0, 0, None),
            (21, 1, "15550100001@s.whatsapp.net", 0, _ts(2026, 1, 2, 14), None, "stanza-call", 59, 0, 0, None),
            (22, 1, None, 1, _ts(2026, 1, 2, 15), "https://example.test", "stanza-url", 7, 0, 0, None),
        ],
    )
    conn.commit()
    conn.close()
    client = WhatsAppClient(data_root=whatsapp_fixture)

    messages = client.messages(1, limit=5)

    assert messages[0].type_name == "url"
    assert messages[0].display_text == "https://example.test"
    assert messages[1].type_name == "video_call"
    assert messages[1].display_text == "Video call"
    assert messages[1].text == ""
    assert messages[2].type_name == "disappearing_messages_notice"
    assert messages[2].display_text == "Disappearing messages setting changed"
    assert messages[3].type_name == "system_information"
    assert messages[3].display_text == "System information message"


def test_iter_messages_messages_after_and_watch(whatsapp_fixture):
    client = WhatsAppClient(data_root=whatsapp_fixture)

    assert [message.id for message in client.messages_after(1, limit=2)] == [2, 3]
    assert client.messages_after(1, limit=1, attachments=True)[0].attachments[0].caption == "photo caption"
    assert [message.id for message in client.iter_messages(page_size=2)] == [1, 2, 3]

    conn = sqlite3.connect(whatsapp_fixture / "ChatStorage.sqlite")
    conn.execute(
        "INSERT INTO ZWAMESSAGE VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (5, 1, None, 1, _ts(2026, 1, 2, 13), "new watched row", "stanza-watch", 0, 0, 0, None),
    )
    conn.commit()
    conn.close()

    watched = list(client.watch(start_rowid=4, poll_interval=0.01, timeout=0.05))
    assert [message.id for message in watched] == [5]


def test_send_dry_run_resolves_contact_name(whatsapp_fixture):
    client = WhatsAppClient(data_root=whatsapp_fixture)

    result = client.send(to="Alex Example", text="preview only", dry_run=True)

    assert result.dry_run is True
    assert result.recipient == "15550100001@s.whatsapp.net"
