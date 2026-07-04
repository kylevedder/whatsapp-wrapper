from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import core, permissions
from .models import Attachment, Chat, Contact, Jid, Message, SendResult
from .sender import WhatsAppSender


class WhatsAppClient:
    def __init__(
        self,
        *,
        data_root: str | Path | None = None,
        chat_db_path: str | Path | None = None,
        contacts_db_path: str | Path | None = None,
        lid_db_path: str | Path | None = None,
        media_root: str | Path | None = None,
        home: str | Path | None = None,
        send_timeout: int = core.DEFAULT_SEND_TIMEOUT_SECONDS,
        verification_timeout: float = core.DEFAULT_VERIFICATION_TIMEOUT_SECONDS,
        verify_sends: bool = True,
        enrich_contacts: bool = True,
        region: str = "US",
        sender: WhatsAppSender | None = None,
    ) -> None:
        self.home = Path(home).expanduser() if home else core.host_home()
        self.data_root = Path(data_root).expanduser() if data_root else core.discover_data_root(self.home)
        self.chat_db_path = Path(chat_db_path).expanduser() if chat_db_path else self.data_root / "ChatStorage.sqlite"
        self.contacts_db_path = Path(contacts_db_path).expanduser() if contacts_db_path else self.data_root / "ContactsV2.sqlite"
        self.lid_db_path = Path(lid_db_path).expanduser() if lid_db_path else self.data_root / "LID.sqlite"
        self.media_root = Path(media_root).expanduser() if media_root else self.data_root / "Message"
        self.send_timeout = send_timeout
        self.verification_timeout = verification_timeout
        self.verify_sends = verify_sends
        self.enrich_contacts = enrich_contacts
        self.region = region
        self.sender = sender or WhatsAppSender(send_timeout=send_timeout)
        self._contacts_cache: list[Contact] | None = None
        self._contact_index: dict[str, Contact] | None = None

    def chats(self, limit: int = 100, offset: int = 0) -> list[Chat]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if offset < 0:
            raise ValueError("offset must be >= 0")
        with self._connect_chat() as conn:
            self._require_table(conn, "ZWACHATSESSION")
            cols = self._columns(conn, "ZWACHATSESSION")
            messages = self._columns(conn, "ZWAMESSAGE") if self._table_exists(conn, "ZWAMESSAGE") else set()
            chat_fk = self._first_col(messages, ["ZCHATSESSION", "ZCHATSESSION1", "ZCHAT", "ZCHATSESSION2"])
            msg_date = self._first_col(messages, ["ZMESSAGEDATE", "ZDATE", "ZTIMESTAMP", "ZMESSAGEDATESECONDS"])
            jid_expr = self._expr("c", cols, ["ZCONTACTJID", "ZJID", "ZPARTNERJID", "ZIDENTIFIER"], "NULL")
            name_expr = self._expr("c", cols, ["ZPARTNERNAME", "ZDISPLAYNAME", "ZNAME", "ZTITLE"], "NULL")
            last_expr = self._expr("c", cols, ["ZLASTMESSAGEDATE", "ZLASTMESSAGEORDERINGTIMESTAMP", "ZLASTMESSAGEDATESECONDS"], "NULL")
            unread_expr = self._expr("c", cols, ["ZUNREADCOUNT", "ZUNREADMESSAGESCOUNT"], "0")
            archived_expr = self._expr("c", cols, ["ZARCHIVED", "ZISARCHIVED"], "0")
            hidden_expr = self._expr("c", cols, ["ZHIDDEN", "ZISHIDDEN"], "0")
            kind_expr = self._expr("c", cols, ["ZSESSIONTYPE", "ZCHATTYPE", "ZTYPE"], "NULL")
            if chat_fk:
                count_expr = f"(SELECT COUNT(*) FROM ZWAMESSAGE m WHERE m.{chat_fk} = c.Z_PK)"
                if msg_date and last_expr == "NULL":
                    last_expr = f"(SELECT MAX(m.{msg_date}) FROM ZWAMESSAGE m WHERE m.{chat_fk} = c.Z_PK)"
            else:
                count_expr = "0"
            rows = conn.execute(
                f"""
                SELECT
                    c.Z_PK AS chat_id,
                    {jid_expr} AS jid,
                    {name_expr} AS name,
                    {last_expr} AS last_message_date,
                    {unread_expr} AS unread_count,
                    {archived_expr} AS archived,
                    {hidden_expr} AS hidden,
                    {kind_expr} AS raw_kind,
                    {count_expr} AS message_count
                FROM ZWACHATSESSION c
                ORDER BY last_message_date DESC, c.Z_PK DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            return [self._row_to_chat(conn, row) for row in rows]

    def iter_chats(self, page_size: int = 100) -> Iterator[Chat]:
        offset = 0
        while True:
            batch = self.chats(limit=page_size, offset=offset)
            if not batch:
                return
            yield from batch
            offset += len(batch)

    def chat(self, chat_id: int | None = None, jid: str | Jid | None = None) -> Chat | None:
        if chat_id is None and jid is None:
            raise ValueError("chat_id or jid is required")
        target_jid = Jid.parse(jid)
        with self._connect_chat() as conn:
            self._require_table(conn, "ZWACHATSESSION")
            cols = self._columns(conn, "ZWACHATSESSION")
            messages = self._columns(conn, "ZWAMESSAGE") if self._table_exists(conn, "ZWAMESSAGE") else set()
            chat_fk = self._first_col(messages, ["ZCHATSESSION", "ZCHATSESSION1", "ZCHAT", "ZCHATSESSION2"])
            msg_date = self._first_col(messages, ["ZMESSAGEDATE", "ZDATE", "ZTIMESTAMP", "ZMESSAGEDATESECONDS"])
            jid_col = self._first_col(cols, ["ZCONTACTJID", "ZJID", "ZPARTNERJID", "ZIDENTIFIER"])
            jid_expr = f"c.{jid_col}" if jid_col else "NULL"
            name_expr = self._expr("c", cols, ["ZPARTNERNAME", "ZDISPLAYNAME", "ZNAME", "ZTITLE"], "NULL")
            last_expr = self._expr("c", cols, ["ZLASTMESSAGEDATE", "ZLASTMESSAGEORDERINGTIMESTAMP", "ZLASTMESSAGEDATESECONDS"], "NULL")
            unread_expr = self._expr("c", cols, ["ZUNREADCOUNT", "ZUNREADMESSAGESCOUNT"], "0")
            archived_expr = self._expr("c", cols, ["ZARCHIVED", "ZISARCHIVED"], "0")
            hidden_expr = self._expr("c", cols, ["ZHIDDEN", "ZISHIDDEN"], "0")
            kind_expr = self._expr("c", cols, ["ZSESSIONTYPE", "ZCHATTYPE", "ZTYPE"], "NULL")
            if chat_fk:
                count_expr = f"(SELECT COUNT(*) FROM ZWAMESSAGE m WHERE m.{chat_fk} = c.Z_PK)"
                if msg_date and last_expr == "NULL":
                    last_expr = f"(SELECT MAX(m.{msg_date}) FROM ZWAMESSAGE m WHERE m.{chat_fk} = c.Z_PK)"
            else:
                count_expr = "0"
            clauses: list[str] = []
            params: list[Any] = []
            if chat_id is not None:
                clauses.append("c.Z_PK = ?")
                params.append(chat_id)
            if target_jid and jid_col:
                placeholders = ", ".join("?" for _ in target_jid.equivalent_keys())
                clauses.append(f"c.{jid_col} COLLATE NOCASE IN ({placeholders})")
                params.extend(target_jid.equivalent_keys())
            row = conn.execute(
                f"""
                SELECT
                    c.Z_PK AS chat_id,
                    {jid_expr} AS jid,
                    {name_expr} AS name,
                    {last_expr} AS last_message_date,
                    {unread_expr} AS unread_count,
                    {archived_expr} AS archived,
                    {hidden_expr} AS hidden,
                    {kind_expr} AS raw_kind,
                    {count_expr} AS message_count
                FROM ZWACHATSESSION c
                WHERE {" OR ".join(f"({item})" for item in clauses)}
                LIMIT 1
                """,
                params,
            ).fetchone()
            return self._row_to_chat(conn, row) if row else None

    def search_chats(self, query: str, limit: int = 25) -> list[Chat]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        needle = query.strip()
        if not needle:
            raise ValueError("query is required")
        scored: list[tuple[int, float, Chat]] = []
        for item in self.chats(limit=5000):
            values = [item.name, item.display_name, item.identifier, *(jid.raw for jid in item.participants)]
            if item.jid:
                values.extend(item.jid.equivalent_keys())
            score = core.lookup_match_score(needle, values)
            if score is not None:
                timestamp = (item.last_message_at or datetime.min.replace(tzinfo=timezone.utc)).timestamp()
                scored.append((score, timestamp, item))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2].name.lower()))
        return [item[2] for item in scored[:limit]]

    def messages(
        self,
        chat_id: int,
        limit: int = 100,
        start: datetime | None = None,
        end: datetime | None = None,
        participants: list[str] | None = None,
        attachments: bool = False,
        include_deleted: bool = False,
    ) -> list[Message]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        with self._connect_chat() as conn:
            rows = self._message_rows(
                conn,
                chat_id=chat_id,
                limit=limit,
                start=start,
                end=end,
                participants=participants,
                attachments=attachments,
                include_deleted=include_deleted,
                ascending=False,
            )
            return [self._row_to_message(conn, row, include_attachments=attachments) for row in rows]

    def messages_after(self, rowid: int, chat_id: int | None = None, limit: int = 100) -> list[Message]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        with self._connect_chat() as conn:
            rows = self._message_rows(conn, chat_id=chat_id, after_rowid=rowid, limit=limit, ascending=True, include_deleted=True)
            return [self._row_to_message(conn, row, include_attachments=False) for row in rows]

    def iter_messages(
        self,
        chat_id: int | None = None,
        page_size: int = 100,
        start: datetime | None = None,
        end: datetime | None = None,
        include_deleted: bool = False,
    ) -> Iterator[Message]:
        last_rowid = 0
        while True:
            with self._connect_chat() as conn:
                rows = self._message_rows(
                    conn,
                    chat_id=chat_id,
                    after_rowid=last_rowid,
                    limit=page_size,
                    start=start,
                    end=end,
                    include_deleted=include_deleted,
                    ascending=True,
                )
                messages = [self._row_to_message(conn, row, include_attachments=False) for row in rows]
            if not messages:
                return
            yield from messages
            last_rowid = messages[-1].id

    def watch(
        self,
        *,
        start_rowid: int | None = None,
        chat_id: int | None = None,
        limit: int = 100,
        poll_interval: float = 1.0,
        timeout: float | None = None,
    ) -> Iterator[Message]:
        last_rowid = start_rowid if start_rowid is not None else self._max_message_rowid()
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            batch = self.messages_after(last_rowid, chat_id=chat_id, limit=limit)
            if batch:
                for message in batch:
                    yield message
                    last_rowid = max(last_rowid, message.id)
                continue
            if deadline is not None and time.monotonic() >= deadline:
                return
            time.sleep(poll_interval)

    def search_messages(
        self,
        query: str,
        *,
        chat_id: int | None = None,
        limit: int = 100,
        start: datetime | None = None,
        end: datetime | None = None,
        participants: list[str] | None = None,
        attachments: bool = False,
        include_deleted: bool = False,
        regex: bool = False,
    ) -> list[Message]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        needle = query.strip()
        if not needle:
            raise ValueError("query is required")
        pattern = re.compile(needle, re.IGNORECASE | re.DOTALL) if regex else None
        results: list[Message] = []
        with self._connect_chat() as conn:
            rows = self._message_rows(
                conn,
                chat_id=chat_id,
                limit=max(limit * 20, 500),
                start=start,
                end=end,
                participants=participants,
                attachments=attachments,
                include_deleted=include_deleted,
                ascending=False,
            )
            for row in rows:
                text = str(row["text"] or "")
                matched = bool(pattern.search(text)) if pattern else needle.casefold() in text.casefold()
                if matched:
                    results.append(self._row_to_message(conn, row, include_attachments=attachments))
                    if len(results) >= limit:
                        break
        return results

    def contacts(self) -> list[Contact]:
        if self._contacts_cache is not None:
            return self._contacts_cache
        contacts = self._load_contacts_v2()
        if not contacts:
            contacts = self._contacts_from_chats()
        contacts = self._dedupe_contacts_with_lid_pairs(contacts)
        self._contacts_cache = contacts
        self._contact_index = self._build_contact_index(contacts)
        return contacts

    def search_contacts(self, query: str, limit: int = 10) -> list[Contact]:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        needle = query.strip()
        if not needle:
            raise ValueError("query is required")
        scored: list[tuple[int, str, Contact]] = []
        normalized_phone = core.normalize_phone(needle, self.region)
        for contact in self.contacts():
            values: list[Any] = [
                contact.display_name,
                contact.first_name,
                contact.last_name,
                contact.nickname,
                contact.organization,
                contact.phone,
                contact.lid,
                contact.jid.raw if contact.jid else None,
                *(jid.raw for jid in contact.raw_jids),
            ]
            if normalized_phone and contact.phone == normalized_phone:
                score = 1100
            else:
                score = core.lookup_match_score(needle, values)
            if score is not None:
                scored.append((score, contact.display_name.lower(), contact))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item[2] for item in scored[:limit]]

    def resolve_contact(self, value: str | Jid) -> Contact | None:
        jid = Jid.parse(value)
        try:
            index = self._contact_index if self._contact_index is not None else self._build_contact_index(self.contacts())
        except core.WhatsAppError:
            return None
        if jid:
            for key in jid.equivalent_keys():
                if key.casefold() in index:
                    return index[key.casefold()]
        phone = core.normalize_phone(str(value), self.region)
        if phone and phone.casefold() in index:
            return index[phone.casefold()]
        matches = self.search_contacts(str(value), limit=1)
        return matches[0] if matches else None

    def send(
        self,
        *,
        to: str | None = None,
        chat_id: int | None = None,
        jid: str | Jid | None = None,
        text: str = "",
        file_paths: list[str | Path] | None = None,
        verify: bool | None = None,
        dry_run: bool = False,
        allow_experimental_group: bool = False,
    ) -> SendResult:
        if not any([to, chat_id, jid]):
            raise ValueError("to, chat_id, or jid is required")
        if sum(1 for item in (to, chat_id, jid) if item is not None) > 1:
            raise ValueError("provide only one of to, chat_id, or jid")
        files = [str(Path(path).expanduser()) for path in (file_paths or [])]
        target_chat = self._resolve_send_chat(to=to, chat_id=chat_id, jid=jid)
        before_rowid = self._max_message_rowid() if (verify if verify is not None else self.verify_sends) and not dry_run else 0
        result = self.sender.send(
            chat=target_chat,
            text=text,
            file_paths=files,
            dry_run=dry_run,
            allow_experimental_group=allow_experimental_group,
        )
        should_verify = (self.verify_sends if verify is None else verify) and not dry_run and result.sent
        if not should_verify:
            return result
        return self._verify_send_result(result, target_chat, text, before_rowid)

    def doctor(self) -> dict[str, Any]:
        statuses = permissions.diagnostics(self.chat_db_path)
        return {
            "data_root": str(self.data_root),
            "chat_db_path": str(self.chat_db_path),
            "contacts_db_path": str(self.contacts_db_path),
            "lid_db_path": str(self.lid_db_path),
            "media_root": str(self.media_root),
            "permissions": [status.to_dict() for status in statuses],
        }

    def _connect_chat(self) -> sqlite3.Connection:
        return core.open_readonly(self.chat_db_path)

    def _connect_contacts(self) -> sqlite3.Connection:
        return core.open_readonly(self.contacts_db_path)

    def _connect_lid(self) -> sqlite3.Connection:
        return core.open_readonly(self.lid_db_path)

    def _row_to_chat(self, conn: sqlite3.Connection, row: sqlite3.Row) -> Chat:
        jid = Jid.parse(row["jid"])
        raw_kind = row["raw_kind"]
        kind = "group" if jid and jid.kind == "group" else "direct"
        if str(raw_kind).lower() in {"group", "2"}:
            kind = "group"
        display_name = str(row["name"] or "").strip() or None
        name = display_name or (f"+{jid.phone}" if jid and jid.phone else (jid.raw if jid else f"chat:{row['chat_id']}"))
        participants = self._chat_participants(conn, int(row["chat_id"]))
        contacts: list[Contact] = []
        if self.enrich_contacts and self._contact_index is not None:
            for candidate in [jid, *participants]:
                contact = self._contact_for_loaded_jid(candidate) if candidate else None
                if contact and contact not in contacts:
                    contacts.append(contact)
        return Chat(
            id=int(row["chat_id"]),
            jid=jid,
            name=name,
            display_name=display_name,
            kind=kind,
            unread_count=int(row["unread_count"] or 0),
            is_archived=core.coerce_bool(row["archived"]),
            is_hidden=core.coerce_bool(row["hidden"]),
            participants=participants,
            contacts=contacts,
            last_message_at=core.whatsapp_timestamp_to_datetime(row["last_message_date"]),
            message_count=int(row["message_count"] or 0),
            raw=core.row_to_dict(row),
        )

    def _contact_for_loaded_jid(self, jid: Jid | None) -> Contact | None:
        if not jid or self._contact_index is None:
            return None
        for key in jid.equivalent_keys():
            if match := self._contact_index.get(key.casefold()):
                return match
        return None

    def _chat_participants(self, conn: sqlite3.Connection, chat_id: int) -> list[Jid]:
        if not self._table_exists(conn, "ZWAGROUPMEMBER"):
            return []
        cols = self._columns(conn, "ZWAGROUPMEMBER")
        chat_fk = self._first_col(cols, ["ZCHATSESSION", "ZCHATSESSION1", "ZCHAT"])
        jid_col = self._first_col(cols, ["ZMEMBERJID", "ZCONTACTJID", "ZJID", "ZPARTICIPANTJID"])
        if not chat_fk or not jid_col:
            return []
        rows = conn.execute(f"SELECT {jid_col} AS jid FROM ZWAGROUPMEMBER WHERE {chat_fk} = ? ORDER BY Z_PK", (chat_id,)).fetchall()
        return [jid for row in rows if (jid := Jid.parse(row["jid"]))]

    def _message_rows(
        self,
        conn: sqlite3.Connection,
        *,
        chat_id: int | None = None,
        after_rowid: int | None = None,
        limit: int = 100,
        start: datetime | None = None,
        end: datetime | None = None,
        participants: list[str] | None = None,
        attachments: bool = False,
        include_deleted: bool = False,
        ascending: bool,
    ) -> list[sqlite3.Row]:
        del attachments
        self._require_table(conn, "ZWAMESSAGE")
        msg_cols = self._columns(conn, "ZWAMESSAGE")
        chat_cols = self._columns(conn, "ZWACHATSESSION") if self._table_exists(conn, "ZWACHATSESSION") else set()
        chat_fk = self._first_col(msg_cols, ["ZCHATSESSION", "ZCHATSESSION1", "ZCHAT", "ZCHATSESSION2"])
        date_col = self._first_col(msg_cols, ["ZMESSAGEDATE", "ZDATE", "ZTIMESTAMP", "ZMESSAGEDATESECONDS"])
        deleted_col = self._first_col(msg_cols, ["ZDELETED", "ZISDELETED", "ZISREVOKED"])
        sender_expr = self._expr("m", msg_cols, ["ZFROMJID", "ZSENDERJID", "ZCONTACTJID", "ZMEMBERJID"], "NULL")
        text_expr = self._expr("m", msg_cols, ["ZTEXT", "ZMESSAGETEXT", "ZMESSAGE", "ZBODY", "ZCAPTION"], "''")
        stanza_expr = self._expr("m", msg_cols, ["ZSTANZAID", "ZMESSAGEID", "ZGUID"], "NULL")
        from_me_expr = self._expr("m", msg_cols, ["ZISFROMME", "ZFROMME"], "0")
        type_expr = self._expr("m", msg_cols, ["ZMESSAGETYPE", "ZTYPE", "ZMESSAGESUBTYPE"], "NULL")
        starred_expr = self._expr("m", msg_cols, ["ZSTARRED", "ZISSTARRED"], "0")
        media_expr = self._expr("m", msg_cols, ["ZMEDIAITEM", "ZMEDIAITEM1"], "NULL")
        chat_jid_expr = self._expr("c", chat_cols, ["ZCONTACTJID", "ZJID", "ZPARTNERJID", "ZIDENTIFIER"], "NULL")
        chat_name_expr = self._expr("c", chat_cols, ["ZPARTNERNAME", "ZDISPLAYNAME", "ZNAME", "ZTITLE"], "NULL")
        joins = ""
        if chat_fk and chat_cols:
            joins = f"LEFT JOIN ZWACHATSESSION c ON c.Z_PK = m.{chat_fk}"
        else:
            chat_jid_expr = "NULL"
            chat_name_expr = "NULL"
        filters: list[str] = []
        params: list[Any] = []
        if chat_id is not None:
            if not chat_fk:
                raise core.WhatsAppError("ZWAMESSAGE has no recognized chat foreign key column")
            filters.append(f"m.{chat_fk} = ?")
            params.append(chat_id)
        if after_rowid is not None:
            filters.append("m.Z_PK > ?")
            params.append(after_rowid)
        if start is not None:
            if not date_col:
                raise core.WhatsAppError("ZWAMESSAGE has no recognized timestamp column")
            filters.append(f"m.{date_col} >= ?")
            params.append(core.datetime_to_whatsapp_timestamp(start))
        if end is not None:
            if not date_col:
                raise core.WhatsAppError("ZWAMESSAGE has no recognized timestamp column")
            filters.append(f"m.{date_col} < ?")
            params.append(core.datetime_to_whatsapp_timestamp(end))
        if deleted_col and not include_deleted:
            filters.append(f"COALESCE(m.{deleted_col}, 0) = 0")
        if participants:
            normalized = []
            for participant in participants:
                jid = Jid.parse(participant)
                normalized.extend(jid.equivalent_keys() if jid else [participant])
            placeholders = ", ".join("?" for _ in normalized)
            filters.append(f"({sender_expr} COLLATE NOCASE IN ({placeholders}) OR {chat_jid_expr} COLLATE NOCASE IN ({placeholders}))")
            params.extend(normalized)
            params.extend(normalized)
        where = "WHERE " + " AND ".join(f"({item})" for item in filters) if filters else ""
        order = "ASC" if ascending else "DESC"
        rows = conn.execute(
            f"""
            SELECT
                m.Z_PK AS rowid,
                {f"m.{chat_fk}" if chat_fk else "NULL"} AS chat_id,
                {stanza_expr} AS stanza_id,
                {sender_expr} AS sender_jid,
                {text_expr} AS text,
                {f"m.{date_col}" if date_col else "NULL"} AS message_date,
                {from_me_expr} AS is_from_me,
                {type_expr} AS raw_type,
                {starred_expr} AS is_starred,
                {f"m.{deleted_col}" if deleted_col else "0"} AS is_deleted,
                {media_expr} AS media_item_id,
                {chat_jid_expr} AS chat_jid,
                {chat_name_expr} AS chat_name
            FROM ZWAMESSAGE m
            {joins}
            {where}
            ORDER BY m.Z_PK {order}
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        return rows

    def _row_to_message(self, conn: sqlite3.Connection, row: sqlite3.Row, *, include_attachments: bool) -> Message:
        sender_jid = Jid.parse(row["sender_jid"])
        chat_jid = Jid.parse(row["chat_jid"])
        contact = self.resolve_contact(sender_jid) if self.enrich_contacts and sender_jid else None
        sender = "me" if core.coerce_bool(row["is_from_me"]) else (contact.display_name if contact else (sender_jid.raw if sender_jid else None))
        attachments = self._attachments_for_message(conn, int(row["rowid"]), row["media_item_id"]) if include_attachments else []
        return Message(
            id=int(row["rowid"]),
            chat_id=int(row["chat_id"] or 0),
            stanza_id=row["stanza_id"],
            sender=sender,
            text=str(row["text"] or ""),
            created_at=core.whatsapp_timestamp_to_datetime(row["message_date"]),
            is_from_me=core.coerce_bool(row["is_from_me"]),
            sender_jid=sender_jid,
            chat_jid=chat_jid,
            chat_name=row["chat_name"],
            raw_type=row["raw_type"],
            is_starred=core.coerce_bool(row["is_starred"]),
            is_deleted=core.coerce_bool(row["is_deleted"]),
            contact=contact,
            attachments=attachments,
            raw=core.row_to_dict(row),
        )

    def _attachments_for_message(self, conn: sqlite3.Connection, message_id: int, media_item_id: Any = None) -> list[Attachment]:
        if not self._table_exists(conn, "ZWAMEDIAITEM"):
            return []
        cols = self._columns(conn, "ZWAMEDIAITEM")
        message_fk = self._first_col(cols, ["ZMESSAGE", "ZMESSAGE1", "ZOWNER", "ZMESSAGEITEM"])
        path_expr = self._expr("mi", cols, ["ZMEDIAURL", "ZLOCALPATH", "ZPATH", "ZFILENAME", "ZFILEPATH"], "NULL")
        filename_expr = self._expr("mi", cols, ["ZFILENAME", "ZFILE", "ZVCARDNAME", "ZTITLE"], "NULL")
        mime_expr = self._expr("mi", cols, ["ZMIMETYPE", "ZCONTENTTYPE", "ZUTI"], "NULL")
        size_expr = self._expr("mi", cols, ["ZFILESIZE", "ZBYTESIZE", "ZSIZE"], "NULL")
        kind_expr = self._expr("mi", cols, ["ZMEDIATYPE", "ZTYPE"], "NULL")
        filters: list[str] = []
        params: list[Any] = []
        if message_fk:
            filters.append(f"mi.{message_fk} = ?")
            params.append(message_id)
        if media_item_id not in (None, ""):
            filters.append("mi.Z_PK = ?")
            params.append(media_item_id)
        if not filters:
            return []
        rows = conn.execute(
            f"""
            SELECT
                mi.Z_PK AS media_id,
                {path_expr} AS raw_path,
                {filename_expr} AS filename,
                {mime_expr} AS mime_type,
                {size_expr} AS byte_size,
                {kind_expr} AS media_kind
            FROM ZWAMEDIAITEM mi
            WHERE {" OR ".join(f"({item})" for item in filters)}
            ORDER BY mi.Z_PK
            """,
            params,
        ).fetchall()
        attachments: list[Attachment] = []
        for row in rows:
            safe_path = core.safe_resolve_media_path(row["raw_path"], self.media_root)
            attachments.append(
                Attachment(
                    id=int(row["media_id"]),
                    message_id=message_id,
                    filename=row["filename"],
                    path=safe_path,
                    mime_type=row["mime_type"],
                    byte_size=int(row["byte_size"]) if row["byte_size"] is not None else None,
                    media_kind=str(row["media_kind"]) if row["media_kind"] is not None else None,
                    missing=safe_path is None or not Path(safe_path).exists(),
                    raw=core.row_to_dict(row),
                )
            )
        return attachments

    def _load_contacts_v2(self) -> list[Contact]:
        if not self.contacts_db_path.exists():
            return []
        try:
            conn = self._connect_contacts()
        except core.WhatsAppError:
            return []
        with conn:
            if not self._table_exists(conn, "ZWAADDRESSBOOKCONTACT"):
                return []
            cols = self._columns(conn, "ZWAADDRESSBOOKCONTACT")
            jid_expr = self._expr("c", cols, ["ZWHATSAPPID", "ZJID", "ZCONTACTJID", "ZIDENTIFIER"], "NULL")
            phone_expr = self._expr("c", cols, ["ZPHONENUMBER", "ZPHONE", "ZFULLPHONE", "ZWAID"], "NULL")
            lid_expr = self._expr("c", cols, ["ZLID", "ZLIDJID"], "NULL")
            first_expr = self._expr("c", cols, ["ZFIRSTNAME", "ZGIVENNAME"], "NULL")
            last_expr = self._expr("c", cols, ["ZLASTNAME", "ZFAMILYNAME"], "NULL")
            full_expr = self._expr("c", cols, ["ZFULLNAME", "ZDISPLAYNAME", "ZNAME", "ZPUSHNAME"], "NULL")
            nickname_expr = self._expr("c", cols, ["ZNICKNAME"], "NULL")
            org_expr = self._expr("c", cols, ["ZORGANIZATION", "ZCOMPANY"], "NULL")
            rows = conn.execute(
                f"""
                SELECT
                    c.Z_PK AS contact_id,
                    {jid_expr} AS jid,
                    {phone_expr} AS phone,
                    {lid_expr} AS lid,
                    {first_expr} AS first_name,
                    {last_expr} AS last_name,
                    {full_expr} AS full_name,
                    {nickname_expr} AS nickname,
                    {org_expr} AS organization
                FROM ZWAADDRESSBOOKCONTACT c
                ORDER BY full_name, c.Z_PK
                """
            ).fetchall()
        contacts: list[Contact] = []
        for row in rows:
            phone = core.normalize_phone(row["phone"], self.region)
            jid = Jid.parse(row["jid"]) or (Jid.parse(phone) if phone else None)
            lid_jid = Jid.parse(row["lid"]) if row["lid"] else None
            display = str(row["full_name"] or " ".join(part for part in [row["first_name"], row["last_name"]] if part) or (f"+{phone}" if phone else jid.raw if jid else f"contact:{row['contact_id']}"))
            raw_jids = [candidate for candidate in [jid, lid_jid] if candidate]
            contacts.append(
                Contact(
                    id=str(row["contact_id"]),
                    display_name=display,
                    jid=jid,
                    phone=phone or (jid.phone if jid else None),
                    lid=lid_jid.lid if lid_jid else (jid.lid if jid else None),
                    first_name=row["first_name"],
                    last_name=row["last_name"],
                    nickname=row["nickname"],
                    organization=row["organization"],
                    raw_jids=raw_jids,
                    source_db_path=str(self.contacts_db_path),
                    raw=core.row_to_dict(row),
                )
            )
        return contacts

    def _contacts_from_chats(self) -> list[Contact]:
        contacts: list[Contact] = []
        try:
            chats = self.chats(limit=5000)
        except core.WhatsAppError:
            return []
        for chat in chats:
            if chat.kind != "direct" or not chat.jid:
                continue
            display = chat.display_name or (f"+{chat.jid.phone}" if chat.jid.phone else chat.jid.raw)
            contacts.append(
                Contact(
                    id=chat.jid.raw,
                    display_name=display,
                    jid=chat.jid,
                    phone=chat.jid.phone,
                    lid=chat.jid.lid,
                    raw_jids=[chat.jid],
                    source_db_path=str(self.chat_db_path),
                )
            )
        return contacts

    def _lid_pairs(self) -> dict[str, str]:
        if not self.lid_db_path.exists():
            return {}
        try:
            conn = self._connect_lid()
        except core.WhatsAppError:
            return {}
        with conn:
            if not self._table_exists(conn, "ZWAPHONENUMBERLIDPAIR"):
                return {}
            cols = self._columns(conn, "ZWAPHONENUMBERLIDPAIR")
            phone_col = self._first_col(cols, ["ZPHONENUMBER", "ZPHONE", "ZWAID"])
            lid_col = self._first_col(cols, ["ZLID", "ZLIDJID"])
            if not phone_col or not lid_col:
                return {}
            rows = conn.execute(f"SELECT {phone_col} AS phone, {lid_col} AS lid FROM ZWAPHONENUMBERLIDPAIR").fetchall()
        pairs: dict[str, str] = {}
        for row in rows:
            phone = core.normalize_phone(row["phone"], self.region)
            lid = Jid.parse(row["lid"])
            if phone and lid and lid.lid:
                pairs[phone] = lid.lid
        return pairs

    def _dedupe_contacts_with_lid_pairs(self, contacts: list[Contact]) -> list[Contact]:
        lid_pairs = self._lid_pairs()
        by_key: dict[str, Contact] = {}
        for contact in contacts:
            phone = contact.phone or (contact.jid.phone if contact.jid else None)
            lid = contact.lid or (lid_pairs.get(phone) if phone else None)
            key = phone or lid or (contact.jid.raw if contact.jid else contact.id)
            raw_jids = list(contact.raw_jids)
            if lid and not any(jid.lid == lid for jid in raw_jids):
                raw_jids.append(Jid(raw=f"{lid}@lid", kind="lid", lid=lid, user=lid, server="lid"))
            if key in by_key:
                existing = by_key[key]
                merged_jids = list({jid.raw: jid for jid in [*existing.raw_jids, *raw_jids]}.values())
                by_key[key] = Contact(
                    id=existing.id,
                    display_name=existing.display_name or contact.display_name,
                    jid=existing.jid or contact.jid,
                    phone=existing.phone or phone,
                    lid=existing.lid or lid,
                    first_name=existing.first_name or contact.first_name,
                    last_name=existing.last_name or contact.last_name,
                    nickname=existing.nickname or contact.nickname,
                    organization=existing.organization or contact.organization,
                    raw_jids=merged_jids,
                    source_db_path=existing.source_db_path or contact.source_db_path,
                    raw={**contact.raw, **existing.raw},
                )
            else:
                by_key[key] = Contact(
                    id=contact.id,
                    display_name=contact.display_name,
                    jid=contact.jid,
                    phone=phone,
                    lid=lid,
                    first_name=contact.first_name,
                    last_name=contact.last_name,
                    nickname=contact.nickname,
                    organization=contact.organization,
                    raw_jids=raw_jids,
                    source_db_path=contact.source_db_path,
                    raw=contact.raw,
                )
        return list(by_key.values())

    def _build_contact_index(self, contacts: list[Contact]) -> dict[str, Contact]:
        index: dict[str, Contact] = {}
        for contact in contacts:
            keys = [contact.id, contact.phone, contact.lid, contact.display_name]
            if contact.jid:
                keys.extend(contact.jid.equivalent_keys())
            for jid in contact.raw_jids:
                keys.extend(jid.equivalent_keys())
            for key in keys:
                if key:
                    index.setdefault(str(key).casefold(), contact)
        return index

    def _resolve_send_chat(self, *, to: str | None, chat_id: int | None, jid: str | Jid | None) -> Chat:
        if chat_id is not None:
            target = self.chat(chat_id=chat_id)
            if not target:
                raise core.WhatsAppError(f"chat_id not found: {chat_id}")
            return target
        target_jid = Jid.parse(jid) if jid is not None else None
        if not target_jid and to:
            phone = core.normalize_phone(to, self.region)
            target_jid = Jid.parse(phone) if phone else None
        if not target_jid and to:
            candidate = Jid.parse(to)
            target_jid = candidate if candidate and (candidate.phone or candidate.lid or candidate.kind == "group") else None
        if to:
            contact = self.resolve_contact(to)
            if contact:
                target_jid = contact.jid or (Jid.parse(contact.phone) if contact.phone else None)
        if not target_jid:
            phone = core.normalize_phone(to or str(jid or ""), self.region)
            target_jid = Jid.parse(phone) if phone else None
        if not target_jid:
            raise core.WhatsAppError("could not resolve send target")
        try:
            existing = self.chat(jid=target_jid)
        except core.WhatsAppError:
            existing = None
        if existing:
            return existing
        if target_jid.kind != "phone":
            raise core.WhatsAppError("new sends require an existing chat unless the target is a phone JID")
        return Chat(id=0, jid=target_jid, name=f"+{target_jid.phone}", display_name=f"+{target_jid.phone}", kind="direct")

    def _verify_send_result(self, result: SendResult, chat: Chat, text: str, after_rowid: int) -> SendResult:
        deadline = time.monotonic() + self.verification_timeout
        last_seen = after_rowid
        while time.monotonic() < deadline:
            try:
                batch = self.messages_after(last_seen, chat_id=chat.id if chat.id > 0 else None, limit=50)
            except core.WhatsAppError as exc:
                return SendResult(
                    recipient=result.recipient,
                    text=result.text,
                    file_paths=result.file_paths,
                    sent=True,
                    verified=False,
                    delivery_status="sent_unverified",
                    dry_run=False,
                    chat_id=result.chat_id,
                    error=f"verification unavailable: {exc}",
                    raw=result.raw,
                )
            for message in batch:
                last_seen = max(last_seen, message.id)
                if not message.is_from_me:
                    continue
                if chat.id > 0 and message.chat_id != chat.id:
                    continue
                if chat.id <= 0 and chat.jid and message.chat_jid and not set(chat.jid.equivalent_keys()).intersection(message.chat_jid.equivalent_keys()):
                    continue
                if text and text not in message.text:
                    continue
                return SendResult(
                    recipient=result.recipient,
                    text=result.text,
                    file_paths=result.file_paths,
                    sent=True,
                    verified=True,
                    delivery_status="sent",
                    dry_run=False,
                    chat_id=message.chat_id,
                    message_id=message.id,
                    stanza_id=message.stanza_id,
                    raw={**result.raw, "verified_message": message.to_dict()},
                )
            time.sleep(0.25)
        return SendResult(
            recipient=result.recipient,
            text=result.text,
            file_paths=result.file_paths,
            sent=True,
            verified=False,
            delivery_status="sent_unverified",
            dry_run=False,
            chat_id=result.chat_id,
            error="verification timed out",
            raw=result.raw,
        )

    def _max_message_rowid(self) -> int:
        try:
            with self._connect_chat() as conn:
                if not self._table_exists(conn, "ZWAMESSAGE"):
                    return 0
                row = conn.execute("SELECT COALESCE(MAX(Z_PK), 0) AS max_rowid FROM ZWAMESSAGE").fetchone()
                return int(row["max_rowid"] or 0)
        except core.WhatsAppError:
            return 0

    def _table_exists(self, conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1", (table,)).fetchone()
        return row is not None

    def _require_table(self, conn: sqlite3.Connection, table: str) -> None:
        if not self._table_exists(conn, table):
            raise core.WhatsAppError(f"required WhatsApp table not found: {table}")

    def _columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        if not self._table_exists(conn, table):
            return set()
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    @staticmethod
    def _first_col(columns: set[str], candidates: list[str]) -> str | None:
        for candidate in candidates:
            if candidate in columns:
                return candidate
        return None

    @classmethod
    def _expr(cls, alias: str, columns: set[str], candidates: list[str], default: str) -> str:
        col = cls._first_col(columns, candidates)
        return f"{alias}.{col}" if col else default
