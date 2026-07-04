# whatsapp-wrapper

`whatsapp-wrapper` is a macOS-first Python API for local WhatsApp Desktop data. It mirrors the shape of `imessage-wrapper`: read local chat/contact SQLite databases in read-only mode, enrich identities, search/list chats and messages, watch for new rows, and send through WhatsApp.app UI automation.

The package never writes to WhatsApp databases. `ChatStorage.sqlite`, `ContactsV2.sqlite`, and `LID.sqlite` are treated as local sync/cache data. Outbound sends go through WhatsApp Desktop and can then be verified by polling the local database.

## Install

```bash
uv sync --dev
```

## Quick Start

```python
from whatsapp_wrapper import WhatsAppClient

client = WhatsAppClient()

for chat in client.chats(limit=10):
    print(chat.id, chat.display_name, chat.jid.raw if chat.jid else None)

messages = client.messages(chat_id=1, limit=25, attachments=True)
hits = client.search_messages("coffee", limit=10)
contacts = client.search_contacts("alex")
```

Preview a send without touching WhatsApp:

```python
result = client.send(to="+15550100001", text="Running five minutes late", dry_run=True)
print(result.to_dict())
```

Actually sending requires WhatsApp.app on macOS, Accessibility/Automation permission, and a confirmed target:

```python
client.send(to="+15550100001", text="Running five minutes late", verify=True)
```

WhatsApp Desktop does not need to already be open for direct sends. The wrapper launches it with a `whatsapp://send?...` URL, waits for the app, AX-confirms the focused chat when WhatsApp exposes enough accessibility text, clears transient composer reply state, then reopens the direct URL with the outbound text prefilled and presses Return. Explicit phone-number sends can be attempted even when the local SQLite cache is unavailable; name/contact lookup and verification require WhatsApp Desktop to have created and synced its local databases.

## Data Locations

The default discovery path is:

```text
~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared
```

with legacy container fallbacks. The wrapper looks for:

- `ChatStorage.sqlite`
- `ContactsV2.sqlite`
- `LID.sqlite`
- media under `Message/`

SQLite connections are opened with `file:<path>?mode=ro`, a short busy timeout, and short-lived connections. `immutable=1` is intentionally not used because WhatsApp may have active WAL/SHM files.

## Public API

`WhatsAppClient` exposes:

- `chats()`, `iter_chats()`, `chat()`, `search_chats()`
- `messages()`, `messages_after()`, `iter_messages()`, `watch()`, `search_messages()`
- `contacts()`, `search_contacts()`, `resolve_contact()`
- `send()`
- `doctor()`

Exported models:

- `Jid`
- `Chat`
- `Message`
- `Attachment`
- `Contact`
- `SendResult`

`Message.text` preserves the raw message body from SQLite. `Message.display_text` provides a best-effort human-readable label for textless rows with known WhatsApp message types, such as media, URL, system-information, video-call, and disappearing-message rows. `Message.type_name` exposes the decoded type name while `Message.raw_type` keeps the original database value. Media captions are exposed as `Attachment.caption` because WhatsApp Desktop stores image captions on media metadata rows rather than `ZWAMESSAGE.ZTEXT`. The low-number type names come from public WhatsApp forensics references; newer Desktop/system labels are intentionally conservative and should be expanded only with UI or fixture evidence.

## Sending Policy

The sender does not insert rows into `ChatStorage.sqlite`.

- Direct text sends open `whatsapp://send?phone=<digits>` without text, wait for WhatsApp.app, AX-confirm the focused chat when possible, clear transient composer reply state, reopen `whatsapp://send?phone=<digits>&text=<encoded>`, wait for WhatsApp to populate the draft, then press Return.
- Direct file sends open the direct chat, AX-confirm it when possible, clear transient composer reply state, reopen the direct URL, focus the composer area, place file URLs on the pasteboard, wait for the media preview, optionally paste/type caption text, wait for the caption field, then press Return.
- Group sends are experimental, require `allow_experimental_group=True`, and route by existing `chat_id` only.
- Verification polls `ChatStorage.sqlite` for a new outgoing row in the target chat. It compares a conservative normalized form of the outbound text so common WhatsApp rewrites, such as `:)` becoming `🙂`, still verify. For file sends, verification also loads media rows and checks attachment captions. If UI automation appears to complete but the database does not update before timeout, the result is `sent_unverified`.

## Tests

The test suite uses only synthetic SQLite fixtures with fake names, `+155501...` phone numbers, `@s.whatsapp.net`, `@lid`, and `@g.us` identifiers. No real WhatsApp databases or private identifiers belong in tests or package fixtures.

```bash
uv run pytest
```
