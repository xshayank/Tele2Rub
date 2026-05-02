# Shared Contracts — v=1 (Frozen)

> **Status:** Frozen at `v=1`.  
> **Canonical code:** [`kharej/contracts.py`](../../../kharej/contracts.py)  
> **This document:** Human-readable reference for both Track A and Track B.

---

## Purpose

This document is the **single source of truth** for the shared contracts between the Iran VPS (Track B) and the Kharej VPS worker (Track A).  
It was frozen as part of **Step 2 of Track A** (see [`track-a-steps.md`](track-a-steps.md)).

Track B developers: mirror these contracts in the Iran-side codebase. When a change is required, bump `CONTRACT_VERSION` following the rules in the [How to evolve the contract](#how-to-evolve-the-contract) section.

---

## Canonical Sources of Truth

| Artifact | Purpose |
|----------|---------|
| [`kharej/contracts.py`](../../../kharej/contracts.py) | Pydantic v2 models, `encode`/`decode` helpers, S2 key helpers — **authoritative code**. |
| [`message-schema.md`](message-schema.md) | Human-readable schemas with example JSON payloads. |
| [`task-split.md`](task-split.md) §3 | Env vars, lifecycle state machine, S2 key conventions, DB schema. |
| [`architecture.md`](architecture.md) | System-level diagram and data-flow description. |

---

## Message Type Catalog

| Type | Direction | `job_id` | Purpose |
|------|-----------|:--------:|---------|
| `job.create` | Iran → Kharej | ✅ | Request a new download job (single track or batch playlist/album). |
| `job.accepted` | Kharej → Iran | ✅ | Acknowledge that the worker has received and will process the job. |
| `job.progress` | Kharej → Iran | ✅ | Report download/upload progress (throttled to ≤1 msg/3 s per job). |
| `job.completed` | Kharej → Iran | ✅ | All files uploaded to S2; includes S2 object keys, sizes, MIME types, SHA-256. |
| `job.failed` | Kharej → Iran | ✅ | The job could not be completed; includes machine-readable error code. |
| `job.cancel` | Iran → Kharej | ✅ | Cancel an in-progress job. |
| `user.whitelist.add` | Iran → Kharej | ✗ | Add a user to the Kharej access whitelist. |
| `user.whitelist.remove` | Iran → Kharej | ✗ | Remove a user from the Kharej access whitelist. |
| `user.block.add` | Iran → Kharej | ✗ | Block a user on the Kharej side so in-flight jobs can be rejected. |
| `user.block.remove` | Iran → Kharej | ✗ | Unblock a previously blocked user. |
| `admin.clearcache` | Iran → Kharej | ✗ | Flush metadata caches on the Kharej Worker. |
| `admin.settings.update` | Iran → Kharej | ✗ | Push updated runtime settings (avoids re-deploy for config changes). |
| `admin.cookies.update` | Iran → Kharej | ✗ | Replace the `cookies.txt` file on the Kharej Worker (via S2). |
| `admin.ack` | Kharej → Iran | ✗ | Generic acknowledgement for any admin control message. |
| `health.ping` | Iran → Kharej | ✗ | Request health status from the Kharej Worker. |
| `health.pong` | Kharej → Iran | ✗ | Detailed health status response (queue depth, provider states, disk). |

---

## Wire Format

Every message is sent over Rubika as a plain-text string with the format:

```
RTUNES::<json-body>
```

- `RTUNES::` is the routing prefix (constant `RTUNES_PREFIX` in `contracts.py`).
- The JSON body is a UTF-8 encoded object with at minimum `v`, `type`, `ts`, and `job_id`.
- Maximum wire size: **4 096 bytes** (constant `MAX_MESSAGE_BYTES`).

### Envelope fields (every message)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `v` | integer | ✅ | Schema version. Always `1` for this contract. |
| `type` | string | ✅ | Message type discriminator (e.g. `"job.create"`). |
| `ts` | string | ✅ | UTC timestamp (ISO-8601), e.g. `"2026-04-26T17:05:56Z"`. |
| `job_id` | string \| null | Context | UUID v4 of the job; `null` for admin and health messages. |

---

## Supporting Types

### `S2ObjectRef`

Used in the `parts` array of `job.completed`.

```python
class S2ObjectRef(BaseModel):
    key: str      # S2 object key (bucket-relative path)
    size: int     # Exact file size in bytes
    mime: str     # MIME type
    sha256: str   # Hex-encoded SHA-256 checksum
```

### S2 Key Conventions (from `task-split.md` §3.2)

```
media/{job_id}/{safe_filename}[.ext]        # Single media file
media/{job_id}/{safe_filename}-part{N}.zip  # Multipart ZIP
thumbs/{isrc_or_job_id}.jpg                 # Thumbnail
tmp/{job_id}/                               # Multipart upload staging
```

Helper functions in `contracts.py`: `make_media_key`, `make_part_key`, `make_thumb_key`, `make_tmp_prefix`.

### `JobStatus` enum

```
pending → accepted → running → completed
                   → failed
             → cancelled
```

Values: `pending | accepted | running | completed | failed | cancelled`

### `Platform` enum

`youtube | spotify | tidal | qobuz | amazon | soundcloud | bandcamp | musicdl`

### `AccessDecision` enum

`allow | block | not_whitelisted`

---

## Using the Code

### Encoding a message (sender side)

```python
from kharej.contracts import JobCreate, Platform, encode
from datetime import datetime, timezone

msg = JobCreate(
    ts=datetime.now(tz=timezone.utc),
    job_id="550e8400-e29b-41d4-a716-446655440000",
    user_id="f47ac10b-58cc-4372-a567-0e02b2c3d479",
    user_status="active",
    platform=Platform.spotify,
    url="https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
    quality="flac",
    job_type="single",
)
wire = encode(msg)           # "RTUNES::{...json...}"
await rubika_client.send(wire)
```

### Decoding a message (receiver side)

```python
from kharej.contracts import JobCreate, decode

raw = await rubika_client.recv()   # "RTUNES::{...}"
msg = decode(raw)                  # raises ValueError / ValidationError on bad input

if isinstance(msg, JobCreate):
    await dispatcher.handle_job_create(msg)
```

---

## How to Evolve the Contract

From [`message-schema.md`](message-schema.md) §12:

| Scenario | Action |
|----------|--------|
| Adding a new **optional** field | No version bump. Both sides must ignore unknown fields. |
| Renaming a **required** field | **Version bump**: bump `CONTRACT_VERSION` to `2`. Support both during a 2-week transition. |
| Removing a **required** field | **Version bump**: bump `CONTRACT_VERSION` to `2`. |
| Adding a new **message type** | No version bump. Receivers silently ignore unknown `type` values. |
| Changing the `RTUNES::` prefix | Major breaking change — coordinate both tracks carefully. |

**Implementation rule**: each receiver checks `if msg["v"] > SUPPORTED_VERSION: log_warning_and_skip()`.

A version bump requires:
1. A joint PR reviewed by both Track A and Track B.
2. Both tracks support `v=old` and `v=new` for a minimum 2-week transition window.
3. An update to `CONTRACT_VERSION` in `kharej/contracts.py` and the mirror in the Iran codebase.
4. An update to the "Status" banner in `message-schema.md` and `task-split.md`.

---

## Cross-Links

- [`track-a-steps.md`](track-a-steps.md) — Full Track A implementation roadmap.
- [`task-split.md`](task-split.md) §3 — Env vars, lifecycle, key conventions, DB schema.
- [`message-schema.md`](message-schema.md) — Human-readable schemas with example payloads.
- [`architecture.md`](architecture.md) — System architecture diagram.
- [`kharej/contracts.py`](../../../kharej/contracts.py) — Canonical Pydantic v2 implementation.
