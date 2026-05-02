# Web UI Specification — Arvan WebUI Migration

> See also: [architecture.md](architecture.md) · [task-split.md](task-split.md)

---

## 1. Tech-Stack Recommendation

### Primary Recommendation: **FastAPI + React (Vite) + TypeScript**

| Layer | Technology | Reasoning |
|-------|-----------|-----------|
| **Backend API** | FastAPI (Python 3.11+) | Native async, built-in OpenAPI docs, compatible with existing `rubetunes/` Python package; easy to call `rubetunes` functions directly without subprocess overhead. |
| **Frontend** | React 18 + Vite + TypeScript | Best-in-class RTL support via CSS `direction: rtl`; rich ecosystem of Persian-ready UI component libraries (Mantine, Ant Design, shadcn/ui); TypeScript ensures contract safety with API layer. |
| **UI Component Library** | **shadcn/ui + Tailwind CSS** | Accessible, unstyled-by-default, easy dark-mode (`dark:` variant), RTL-ready, no bundle bloat. Alternatively Mantine v7 (has RTL out of the box). |
| **State / data fetching** | React Query (TanStack Query) | Handles loading/error/stale state automatically; pairs perfectly with FastAPI REST endpoints. |
| **Real-time** | `EventSource` (SSE) from FastAPI | Simpler than WebSocket for one-directional server → browser push; FastAPI `StreamingResponse` with `text/event-stream`. WebSocket available as optional upgrade. |
| **Forms** | React Hook Form + Zod | Type-safe validation matching Pydantic schemas on the backend. |
| **Routing** | React Router v6 | SPA routing, lazy loading per page. |
| **Database ORM** | SQLAlchemy 2 (async) + Alembic | Mature, async-native in v2, strong migration tooling. |
| **Auth** | FastAPI-Users library | Handles JWT + httpOnly cookie, registration, password hashing, admin role. |

**Why not Django + HTMX?**
HTMX + Django would be simpler to set up, but it is harder to implement rich real-time progress updates (SSE progress bars, live queue), and the RTL/dark-mode support is more CSS work without a modern component library.

**Why not Next.js?**
Next.js adds SSR complexity that is not needed here. The site is user-authenticated (no SEO benefit from SSR). Vite + React gives faster dev builds and simpler deployment (serve static bundle via nginx).

---

## 2. Auth Model

### 2.1 Registration Flow

```
User fills in Register form
  → POST /auth/register { email, password, display_name }
  → DB: users.status = 'pending_approval'
  → Admin receives notification in Admin Panel
  → Admin clicks "Approve" (or "Reject")
  → DB: users.status = 'active' (or 'rejected')
  → (Optional) Email sent to user on approval
```

### 2.2 Token Model

- **Access token**: JWT signed with `HS256`, TTL 15 min. Payload: `{ sub: user_id, role, exp }`.
- **Refresh token**: Opaque UUID stored in DB table `refresh_tokens`; TTL 7 days. Sent as `httpOnly` cookie.
- **Token refresh**: `POST /auth/refresh` — validates the refresh cookie, returns a new access token.
- **Logout**: `POST /auth/logout` — deletes the refresh token from DB.
- All API routes (except `/auth/register`, `/auth/login`, `/health`) require a valid access token in `Authorization: Bearer <token>` or a session cookie.
- Admin Panel routes also require `role = 'admin'` in the JWT claim.

### 2.3 Invite Code Mode (optional — see open-questions.md)

If the owner prefers invite codes over email:
- Admin generates invite codes from the Admin Panel.
- Users enter an invite code on the registration form; the code is validated server-side.
- No admin approval step needed when a valid code is used.

---

## 3. Public Web UI Pages

### 3.1 Landing / Login Page — `/`

- Shown to unauthenticated users.
- Dark, minimal design with the RubeTunes logo.
- "Sign in" and "Create an account" calls to action.
- Persian as primary language (RTL layout); English toggle optional.

### 3.2 Register Page — `/register`

**Fields**: Display name · Email · Password · Confirm password · (optional) Invite code.

**UX**: Inline validation (Zod); on submit shows "Your registration is pending admin approval" state.

### 3.3 Login Page — `/login`

**Fields**: Email · Password · Remember me (extends refresh token TTL).

**UX**: "Forgot password?" link (email reset flow, see open-questions.md for email provider choice).

---

### 3.4 Search Page — `/search`

**Layout**: Full-width search bar at top (prominent, focused on load); results grid below.

**Search flow**:
1. User types a query → debounced call to `GET /search?q=...&platform=spotify` (or multi-platform).
2. Results shown as cards: cover art thumbnail, title, artist, album, duration, platform badge.
3. "Download" button on each result opens the **Quality Picker** drawer/modal.

**Filters**: Platform selector (Spotify, Tidal, Qobuz, musicdl, YouTube Music); quality badge filter (MP3, FLAC, Hi-Res).

**Direct URL input**: Users can paste a URL directly into the search bar; the page detects the platform and routes accordingly.

---

### 3.5 Result Detail Page — `/result/{job_id_or_url}`

For URLs (YouTube, Spotify track, etc.) that have a rich info page:

**YouTube video**:
- Thumbnail, title, channel, duration, upload date.
- Quality cards: 4K · 1080p · 720p · 480p · 360p · 240p · Audio-only MP3 · Subtitles.
- Each card shows estimated file size.
- "Download" button on selected quality → creates job.

**Spotify / music track**:
- Album art, title, artists, album, duration, ISRC.
- Quality picker: MP3 · FLAC CD · FLAC Hi-Res (availability shown with ✓/✗).
- Platform waterfall preview (which sources will be tried).
- "Download" button → creates job.

**Spotify playlist / album**:
- Collection cover art, name, owner/artist, track count.
- Track list (paginated).
- Quality picker for the whole batch.
- "Download All" button → creates batch job.

---

### 3.6 Download Progress Page — `/jobs/{job_id}`

Real-time progress view (SSE):

- **Status badges**: `pending` → `accepted` → `running` → `completed` / `failed`.
- **Progress bar** with percentage and download speed (from `job.progress` events).
- For batch jobs: per-track status grid.
- On completion: "Download File" button (fetches presigned URL) or, for multi-part ZIPs, a list of "Download Part N" buttons.
- Error state: human-readable error message + "Try again" button.

---

### 3.7 My Library / Downloads Page — `/library`

- Paginated list of the user's completed downloads.
- Columns: thumbnail, title, platform, quality, file size, downloaded at, status.
- "Download again" (generates a fresh presigned URL if the S2 object still exists; shows "Expired" if the lifecycle rule deleted it).
- Search/filter by platform, quality, date.
- "Active jobs" tab showing any in-progress downloads.

---

### 3.8 Streaming Player — `/player/{job_id}`

For audio files at minimum; video optional (see open-questions.md).

- Embedded HTML5 `<audio>` (or `<video>`) element pointed at the presigned S2 URL.
- Controls: play/pause, seek, volume, download button.
- Shows track metadata (title, artist, album art).
- Playlist mode for batch jobs (next/previous track).

---

### 3.9 Account Settings — `/settings`

- Change display name.
- Change email (requires password confirmation + re-verification).
- Change password.
- Delete account (soft-delete; admin sees as `status=deleted`).
- Download rate limit display (current usage vs. limit).
- Notification preferences (browser push / none).

---

## 4. Admin Panel Pages

All admin pages are under `/admin/*`. Access requires `role = 'admin'` JWT claim.

### 4.1 Admin Dashboard — `/admin`

- Summary cards: Total users · Active users · Pending registrations · Jobs today · S2 storage used (GB).
- Recent activity feed.
- Quick links to all sub-sections.

---

### 4.2 Users List — `/admin/users`

**Columns**: Avatar · Display name · Email · Status (active / pending / blocked / deleted) · Registered at · Last seen · Downloads (count) · Actions.

**Actions per row**:
- **Approve** (pending users) → sets `status = 'active'`, sends `user.whitelist.add` control message to Kharej.
- **Block** → sets `status = 'blocked'`, sends `user.block.add` to Kharej.
- **Unblock** → sets `status = 'active'`, sends `user.block.remove` to Kharej.
- **View audit log** → filters audit log by this user.
- **Delete** → soft-delete.

**Search**: By name, email, status.

**Bulk actions**: Approve all pending · Block selected.

---

### 4.3 Pending Registrations Queue — `/admin/users/pending`

- Dedicated view for `status = 'pending_approval'` users.
- Approval or rejection with optional message.
- Shows registration timestamp (oldest first by default).
- Badge count in the sidebar navigation.

---

### 4.4 Active Jobs / Queue Monitor — `/admin/jobs`

**Tabs**: Active · Completed · Failed.

**Active jobs columns**: Job ID · User · Platform · Title/URL · Status · Progress · Started at · Actions (Cancel).

**Completed jobs columns**: Job ID · User · Platform · Title · Quality · File size · S2 key · Completed at · TTL remaining · Download.

**Failed jobs columns**: Job ID · User · Error code · Error message · Failed at · Retry button.

**Queue depth chart**: Live line chart of `queue_depth` gauge (Prometheus or in-memory counter).

---

### 4.5 S2 Storage Usage Dashboard — `/admin/storage`

- Total used storage (GB) vs. plan limit.
- Object count by prefix (`media/`, `thumbs/`, `tmp/`).
- Top users by storage used.
- Breakdown by platform and quality.
- "Purge expired" button (triggers nightly cleanup job manually).
- S2 lifecycle rule display.

Data source: Arvan S2 `ListObjectsV2` with aggregation, cached in DB every 6 hours.

---

### 4.6 Settings Editor — `/admin/settings`

Lets the admin update runtime settings without redeploying.

**Categories**:

| Category | Settings |
|----------|----------|
| **Rate Limiting** | `USER_TRACKS_PER_HOUR` |
| **Batch Downloads** | `BATCH_CONCURRENCY`, `ZIP_PART_SIZE_MB` |
| **S2** | `PRESIGNED_URL_TTL_SEC`, `MEDIA_TTL_DAYS` |
| **Providers** | `DEEZER_ARL`, `TIDAL_TOKEN`, `SPOTIFY_CLIENT_ID/SECRET` |
| **Cookies** | Upload new `cookies.txt` (sent securely to Kharej via control message `admin.cookies.update` — the file itself is split into base64 chunks if >4 KB, or transmitted via S2 temporary object) |
| **Circuit Breaker** | `CIRCUIT_FAIL_THRESHOLD`, `CIRCUIT_FAIL_WINDOW_SEC`, `CIRCUIT_OPEN_DURATION_SEC` |
| **Shutdown** | `SHUTDOWN_TIMEOUT_SEC` |
| **Notifications** | Sentry DSN, Log format |
| **Registration** | Open registration / invite code / closed |

Settings are stored in the DB `settings` table (key-value) and pushed to the Kharej Worker via `admin.settings.update` control message.

**Warning**: API keys and secrets are stored encrypted at rest in the DB (AES-256-GCM with a key derived from `SECRET_KEY` env var).

---

### 4.7 Audit Log — `/admin/audit`

- Every user action, admin action, job event, and system event is logged.
- Columns: Timestamp · User · IP address · Action type · Detail · Job ID (if applicable).
- Sortable, filterable (by user, action type, date range).
- Export to CSV.

---

### 4.8 Provider Health — `/admin/health`

- Grid of provider cards: Qobuz · Deezer · Tidal · lrclib · MusicBrainz · Odesli · YouTube Music · Arvan S2.
- Status: 🟢 Up · 🟡 Slow · 🔴 Down.
- Last check timestamp + response time.
- Circuit breaker state (open / half-open / closed) per provider.
- "Re-check all" button (sends `health.ping` to Kharej; displays `health.pong` response).

---

## 5. UI / UX Requirements

### 5.1 Design Principles

- **Modern and polished**: inspired by Spotify Web, YouTube Music, Plex. Not "yet another form app."
- **Dark mode by default**: `prefers-color-scheme: dark` respected; explicit toggle in header.
- **RTL-first**: Persian (Farsi) as the primary language. Layout engine: `direction: rtl; unicode-bidi: embed`. Numbers displayed in Western Arabic numerals (not Indic) unless user preference set. Font: **Vazirmatn** (open-source, supports Persian script beautifully).
- **Responsive**: Mobile-first. Breakpoints: xs (< 640 px), sm (640 px), md (768 px), lg (1024 px), xl (1280 px). Tested on iPhone 14 screen size.
- **Accessibility basics**: `aria-label` on all icon-only buttons; keyboard navigation for all interactive elements; `prefers-reduced-motion` respected; colour contrast ≥ 4.5:1 (WCAG AA).

### 5.2 Download Progress UX

- Animated progress bar with percentage and speed.
- Live updating without page reload (SSE).
- "Cancel" button available during download phase.
- Toast notifications on completion or error.

### 5.3 Error Handling

- User-friendly Persian-language error messages (no raw exception strings exposed).
- Retry buttons on transient errors.
- "Report an issue" link sends the job ID to admin audit log.

### 5.4 Internationalisation (i18n)

- All UI strings in a JSON translation file: `fa.json` (Persian), `en.json` (English).
- `react-i18next` for runtime language switching.
- Language preference stored in `localStorage` and user profile.
- Default language: **Persian**.

---

## 6. API Route Summary

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/auth/register` | None | Register new user |
| `POST` | `/auth/login` | None | Login, set refresh cookie |
| `POST` | `/auth/refresh` | Cookie | Refresh access token |
| `POST` | `/auth/logout` | JWT | Revoke refresh token |
| `GET` | `/search` | JWT | Search (Spotify, musicdl, etc.) |
| `GET` | `/video/info` | JWT | Fetch yt-dlp metadata for a URL |
| `POST` | `/jobs` | JWT | Create a new download job |
| `GET` | `/jobs/{job_id}` | JWT | Get job status and details |
| `GET` | `/jobs/{job_id}/events` | JWT | SSE stream for job progress |
| `GET` | `/jobs/{job_id}/download` | JWT | Get presigned URL (or proxy stream) |
| `GET` | `/library` | JWT | List user's completed downloads |
| `GET` | `/settings/me` | JWT | Get account settings |
| `PATCH` | `/settings/me` | JWT | Update account settings |
| `GET` | `/admin/users` | Admin JWT | List all users |
| `PATCH` | `/admin/users/{id}` | Admin JWT | Update user status (approve/block) |
| `GET` | `/admin/jobs` | Admin JWT | List all jobs |
| `DELETE`| `/admin/jobs/{id}` | Admin JWT | Cancel a job |
| `GET` | `/admin/storage` | Admin JWT | S2 usage stats |
| `GET` | `/admin/audit` | Admin JWT | Audit log |
| `GET` | `/admin/settings` | Admin JWT | Get all settings |
| `PATCH` | `/admin/settings` | Admin JWT | Update settings |
| `GET` | `/admin/health` | Admin JWT | Provider health |
| `POST` | `/admin/health/ping` | Admin JWT | Trigger health.ping to Kharej |
