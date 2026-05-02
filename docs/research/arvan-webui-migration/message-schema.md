# Rubika Control Bus — Message Schema

> **Status:** Frozen at `v=1`. Canonical implementation: [`kharej/contracts.py`](../../../kharej/contracts.py). See [`CONTRACTS.md`](CONTRACTS.md).

> **Version**: 1  
> **Status**: Proposed — must be approved by both Track A and Track B developers before coding begins.  
> See also: [architecture.md](architecture.md) · [task-split.md](task-split.md)

---

## Principles

1. **No binary payloads** — files NEVER travel over Rubika. Only small JSON control messages.
2. **Size limit** — each message must fit within Rubika's text message limit. Keep every message under **4 KB** (UTF-8 encoded). Typical messages are 200–800 bytes.
3. **Versioned** — every message carries `"v": 1`. Breaking changes require a bump to `"v": 2` and a migration period.
4. **Routing prefix** — every message is sent as a Rubika text message prefixed with `RTUNES::` followed by the JSON body. Recipients strip the prefix before JSON-parsing.
5. **Idempotency** — messages include a `job_id` (UUID v4) so duplicate deliveries can be detected and ignored.
6. **Timestamps** — all `ts` fields are ISO-8601 UTC (e.g., `"2026-04-26T17:05:56Z"`).

---

## Message Envelope

Every message shares the following top-level fields:

```json
{
  "v": 1,
  "type": "<message-type>",
  "ts": "<ISO-8601 UTC>",
  "job_id": "<uuid-v4 or null>"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `v` | integer | ✅ | Schema version. Currently `1`. |
| `type` | string | ✅ | Message type (see below). |
| `ts` | string | ✅ | UTC timestamp of when the message was created. |
| `job_id` | string \| null | Context-dependent | UUID v4 of the job. Required for all job-related messages. |

---

## 1. `job.create`

**Direction**: Iran VPS → Kharej VPS  
**Purpose**: Request a new download job.

```json
{
  "v": 1,
  "type": "job.create",
  "ts": "2026-04-26T17:05:56Z",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "user_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "user_status": "active",
  "platform": "spotify",
  "url": "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC",
  "quality": "flac",
  "job_type": "single",
  "format_hint": null
}
```

**Batch variant** (playlist or album — `job_type: "batch"`):

```json
{
  "v": 1,
  "type": "job.create",
  "ts": "2026-04-26T17:10:00Z",
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "user_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "user_status": "active",
  "platform": "spotify",
  "url": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
  "quality": "mp3",
  "job_type": "batch",
  "format_hint": "mp3",
  "collection_name": "Today's Top Hits",
  "track_ids": [
    "4uLU6hMCjMI75M1A2tKUQC",
    "7qiZfU4dY1lWllzX7mPBI3"
  ],
  "total_tracks": 50
}
```

> ⚠️ **Size warning**: If `track_ids` would push the message over 4 KB (roughly 200+ track IDs), split into multiple `job.create` messages with the same `job_id` and a `batch_seq` / `batch_total` field, OR omit `track_ids` and let the Kharej Worker fetch the playlist independently using the `url`.

**Fields**:

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `user_id` | string | ✅ | UUID of the requesting user |
| `user_status` | string | ✅ | `"active"` \| `"admin"` |
| `platform` | string | ✅ | `"youtube"` \| `"spotify"` \| `"tidal"` \| `"qobuz"` \| `"amazon"` \| `"soundcloud"` \| `"bandcamp"` \| `"musicdl"` |
| `url` | string | ✅ | Platform URL (validated by Iran VPS before sending) |
| `quality` | string | ✅ | `"mp3"` \| `"flac"` \| `"hires"` \| `"1080p"` \| `"720p"` \| … |
| `job_type` | string | ✅ | `"single"` \| `"batch"` |
| `format_hint` | string \| null | ✗ | User-specified format override (`"mp3"`, `"flac"`, `"m4a"`) |
| `collection_name` | string | Batch only | Human-readable playlist/album name |
| `track_ids` | array[string] | Batch only | Spotify track IDs (omit if >200 tracks) |
| `total_tracks` | integer | Batch only | Total track count |

---

## 2. `job.accepted`

**Direction**: Kharej VPS → Iran VPS  
**Purpose**: Acknowledge that the worker has received and will process the job.

```json
{
  "v": 1,
  "type": "job.accepted",
  "ts": "2026-04-26T17:05:57Z",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "worker_version": "2.0.0",
  "queue_position": 1
}
```

| Field | Notes |
|-------|-------|
| `worker_version` | Semver of the Kharej Worker (for debugging) |
| `queue_position` | Position in the Kharej internal queue (1 = being processed now) |

---

## 3. `job.progress`

**Direction**: Kharej VPS → Iran VPS  
**Purpose**: Report download / upload progress. Published at most once every 3 seconds per job.

**Single-file variant**:

```json
{
  "v": 1,
  "type": "job.progress",
  "ts": "2026-04-26T17:06:00Z",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "phase": "downloading",
  "percent": 42,
  "speed": "3.2 MB/s",
  "eta_sec": 18
}
```

**Batch variant**:

```json
{
  "v": 1,
  "type": "job.progress",
  "ts": "2026-04-26T17:11:00Z",
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "phase": "downloading",
  "done_tracks": 12,
  "total_tracks": 50,
  "failed_tracks": 1,
  "current_track": "Shape of You — Ed Sheeran"
}
```

**Upload phase**:

```json
{
  "v": 1,
  "type": "job.progress",
  "ts": "2026-04-26T17:06:30Z",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "phase": "uploading",
  "percent": 75,
  "speed": "8.1 MB/s",
  "part": 1,
  "total_parts": 1
}
```

| Field | Type | Notes |
|-------|------|-------|
| `phase` | string | `"downloading"` \| `"processing"` \| `"uploading"` \| `"zipping"` |
| `percent` | integer | 0–100. Omitted for batch (use `done_tracks`/`total_tracks`). |
| `speed` | string | Human-readable speed string (e.g., `"3.2 MB/s"`). |
| `eta_sec` | integer | Seconds remaining (best-effort estimate). |
| `done_tracks` | integer | Batch only. |
| `total_tracks` | integer | Batch only. |
| `failed_tracks` | integer | Batch only. Tracks that could not be downloaded. |
| `current_track` | string | Batch only. Title of the track currently being processed. |
| `part` | integer | Multipart ZIP upload: current part number. |
| `total_parts` | integer | Multipart ZIP upload: total number of parts. |

---

## 4. `job.completed`

**Direction**: Kharej VPS → Iran VPS  
**Purpose**: All files have been uploaded to S2. Includes the S2 object keys, sizes, MIME types, and checksums.

**Single-file variant**:

```json
{
  "v": 1,
  "type": "job.completed",
  "ts": "2026-04-26T17:07:15Z",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "parts": [
    {
      "s2_key": "media/550e8400-e29b-41d4-a716-446655440000/Shape_of_You.flac",
      "filename": "Shape_of_You.flac",
      "size_bytes": 34205696,
      "mime_type": "audio/flac",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    }
  ],
  "metadata": {
    "title": "Shape of You",
    "artists": ["Ed Sheeran"],
    "album": "÷ (Divide)",
    "duration_sec": 234,
    "isrc": "GBAHS1600463",
    "source": "qobuz",
    "quality": "flac"
  }
}
```

**Multi-part ZIP variant** (batch download):

```json
{
  "v": 1,
  "type": "job.completed",
  "ts": "2026-04-26T17:25:00Z",
  "job_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "parts": [
    {
      "s2_key": "media/a1b2c3d4-e5f6-7890-abcd-ef1234567890/TodaysTopHits-part1.zip",
      "filename": "TodaysTopHits-part1.zip",
      "size_bytes": 2094006272,
      "mime_type": "application/zip",
      "sha256": "abc123..."
    },
    {
      "s2_key": "media/a1b2c3d4-e5f6-7890-abcd-ef1234567890/TodaysTopHits-part2.zip",
      "filename": "TodaysTopHits-part2.zip",
      "size_bytes": 512000000,
      "mime_type": "application/zip",
      "sha256": "def456..."
    }
  ],
  "metadata": {
    "collection_name": "Today's Top Hits",
    "total_tracks": 50,
    "downloaded_tracks": 49,
    "failed_tracks": 1,
    "quality": "mp3"
  }
}
```

| Field | Notes |
|-------|-------|
| `parts` | Array of uploaded S2 objects. Always an array (even for single files). |
| `parts[].s2_key` | Full S2 object key (bucket-relative path). |
| `parts[].filename` | Display filename for the download button. |
| `parts[].size_bytes` | Exact file size in bytes. |
| `parts[].mime_type` | MIME type. |
| `parts[].sha256` | Hex-encoded SHA-256 of the uploaded file (for integrity check). |
| `metadata` | Human-readable metadata for display in the Web UI. |

---

## 5. `job.failed`

**Direction**: Kharej VPS → Iran VPS  
**Purpose**: The job could not be completed.

```json
{
  "v": 1,
  "type": "job.failed",
  "ts": "2026-04-26T17:06:45Z",
  "job_id": "550e8400-e29b-41d4-a716-446655440000",
  "error_code": "no_source_available",
  "message": "All download sources exhausted for this track.",
  "retryable": false
}
```

**Error codes**:

| Code | Description | Retryable |
|------|-------------|-----------|
| `no_source_available` | All providers failed or returned no result | false |
| `s2_upload_failed` | S2 upload failed after 5 retries | true |
| `download_timeout` | yt-dlp or provider timed out | true |
| `rate_limited` | Provider rate-limited the Kharej VPS | true |
| `invalid_url` | URL could not be parsed | false |
| `access_denied` | User is not whitelisted or is banned | false |
| `disk_space_error` | Insufficient disk space on Kharej VPS | false |
| `internal_error` | Unexpected exception in the worker | true |

---

## 6. `user.whitelist.add` / `user.whitelist.remove`

**Direction**: Iran VPS → Kharej VPS  
**Purpose**: Sync user whitelist state to Kharej Worker.

```json
{
  "v": 1,
  "type": "user.whitelist.add",
  "ts": "2026-04-26T18:00:00Z",
  "job_id": null,
  "user_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "display_name": "Ali Rezaei"
}
```

```json
{
  "v": 1,
  "type": "user.whitelist.remove",
  "ts": "2026-04-26T18:01:00Z",
  "job_id": null,
  "user_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479"
}
```

---

## 7. `user.block.add` / `user.block.remove`

**Direction**: Iran VPS → Kharej VPS  
**Purpose**: Block or unblock a user on the Kharej side (so in-flight jobs can be rejected).

```json
{
  "v": 1,
  "type": "user.block.add",
  "ts": "2026-04-26T18:05:00Z",
  "job_id": null,
  "user_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "reason": "Spam detected"
}
```

```json
{
  "v": 1,
  "type": "user.block.remove",
  "ts": "2026-04-26T18:06:00Z",
  "job_id": null,
  "user_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479"
}
```

---

## 8. `health.ping` / `health.pong`

**Direction**: Ping — Iran VPS → Kharej VPS. Pong — Kharej VPS → Iran VPS.  
**Purpose**: Request and receive health status of the Kharej Worker and its provider connections.

```json
{
  "v": 1,
  "type": "health.ping",
  "ts": "2026-04-26T18:10:00Z",
  "job_id": null,
  "request_id": "ping-abc123"
}
```

```json
{
  "v": 1,
  "type": "health.pong",
  "ts": "2026-04-26T18:10:01Z",
  "job_id": null,
  "request_id": "ping-abc123",
  "worker_version": "2.0.0",
  "queue_depth": 3,
  "circuit_breakers": [
    { "key": "qobuz", "state": "closed", "consecutive_failures": 0 },
    { "key": "deezer", "state": "open", "seconds_until_close": 312, "consecutive_failures": 4 },
    { "key": "tidal_alt", "state": "closed", "consecutive_failures": 0 }
  ],
  "providers": [
    { "name": "Qobuz", "status": "up", "response_ms": 145 },
    { "name": "Deezer", "status": "down", "response_ms": null },
    { "name": "YouTube Music", "status": "up", "response_ms": 201 }
  ],
  "disk_free_gb": 42.3,
  "uptime_sec": 86400
}
```

---

## 9. `admin.clearcache`

**Direction**: Iran VPS → Kharej VPS  
**Purpose**: Flush metadata caches on Kharej Worker.

```json
{
  "v": 1,
  "type": "admin.clearcache",
  "ts": "2026-04-26T18:15:00Z",
  "job_id": null,
  "target": "all"
}
```

`target` values: `"lru"` | `"isrc"` | `"all"`.

---

## 10. `admin.settings.update`

**Direction**: Iran VPS → Kharej VPS  
**Purpose**: Push updated runtime settings to the Kharej Worker (avoids re-deploy for config changes).

```json
{
  "v": 1,
  "type": "admin.settings.update",
  "ts": "2026-04-26T18:20:00Z",
  "job_id": null,
  "settings": {
    "BATCH_CONCURRENCY": 4,
    "USER_TRACKS_PER_HOUR": 50,
    "CIRCUIT_FAIL_THRESHOLD": 5,
    "DEEZER_ARL": "new_arl_value_here"
  }
}
```

⚠️ Sensitive values (ARL, tokens) are transmitted over Rubika — this is acceptable since Rubika messages are encrypted in transit by the platform. However, consider whether to use the S2 `tmp/` path for secrets transfer instead (see [open-questions.md](open-questions.md)).

---

## 11. `admin.cookies.update`

**Direction**: Iran VPS → Kharej VPS  
**Purpose**: Replace the `cookies.txt` file on the Kharej Worker.

Since cookies files can exceed the 4 KB message limit, they are uploaded to S2 first:

```json
{
  "v": 1,
  "type": "admin.cookies.update",
  "ts": "2026-04-26T18:25:00Z",
  "job_id": null,
  "s2_key": "tmp/cookies-update-2026-04-26.txt",
  "sha256": "abc123def456..."
}
```

The Kharej Worker:
1. Downloads the file from S2 using its write credentials (which include `GetObject` on `tmp/`).
2. Validates the SHA-256 checksum.
3. Replaces `cookies.txt` in-place.
4. Deletes the S2 object.

---

## 12. Versioning Strategy

| Scenario | Action |
|----------|--------|
| Adding a new optional field | No version bump. Both sides must ignore unknown fields. |
| Renaming a required field | Version bump: `"v": 2`. Support both during a 2-week transition period. |
| Removing a required field | Version bump: `"v": 2`. |
| Adding a new message type | No version bump. Receivers silently ignore unknown `type` values. |
| Changing the routing prefix `RTUNES::` | Major breaking change — coordinate carefully. |

**Implementation**: Each receiver checks `if msg["v"] > SUPPORTED_VERSION: log_warning_and_skip()`.

---

## Summary Table

| Type | Direction | Carries job_id | Max size |
|------|-----------|:--------------:|---------|
| `job.create` | Iran → Kharej | ✅ | ~1–3 KB (track_ids optional) |
| `job.accepted` | Kharej → Iran | ✅ | ~200 B |
| `job.progress` | Kharej → Iran | ✅ | ~300 B |
| `job.completed` | Kharej → Iran | ✅ | ~1–2 KB |
| `job.failed` | Kharej → Iran | ✅ | ~300 B |
| `user.whitelist.add` | Iran → Kharej | ✗ | ~200 B |
| `user.whitelist.remove` | Iran → Kharej | ✗ | ~150 B |
| `user.block.add` | Iran → Kharej | ✗ | ~200 B |
| `user.block.remove` | Iran → Kharej | ✗ | ~150 B |
| `health.ping` | Iran → Kharej | ✗ | ~150 B |
| `health.pong` | Kharej → Iran | ✗ | ~1–2 KB |
| `admin.clearcache` | Iran → Kharej | ✗ | ~150 B |
| `admin.settings.update` | Iran → Kharej | ✗ | ~500 B |
| `admin.cookies.update` | Iran → Kharej | ✗ | ~250 B |
