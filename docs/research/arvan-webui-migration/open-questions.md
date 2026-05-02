# Open Questions — Arvan WebUI Migration

> These questions must be answered by the project owner **before implementation begins**.
> Answers will update [`architecture.md`](architecture.md), [`webui-spec.md`](webui-spec.md), and [`migration-plan.md`](migration-plan.md).

---

## 1. Arvan S2 Configuration

### 1.1 S2 Region / Endpoint

**Question**: Which Arvan Cloud S2 region should be used?

Arvan offers multiple regions:
- `ir-thr-at1` (Tehran, Asiatech) — endpoint: `https://s3.ir-thr-at1.arvanstorage.ir`
- `ir-thr-ba1` (Tehran, Bamdad) — endpoint: `https://s3.ir-thr-ba1.arvanstorage.ir`
- `ir-thr-fr1` (Tehran, Fibre-1)
- `ir-tbz1` (Tabriz)

**Recommendation**: Choose the region geographically closest to the Iran VPS and with the lowest latency to the Kharej VPS.

**Impact**: The `ARVAN_S2_ENDPOINT` env var in both VPSes.

---

### 1.2 Bucket Naming

**Question**: What should the S2 bucket be named?

**Recommendation**: `rubetunes-media` (or `rubetunes-media-prod` and `rubetunes-media-staging` for environment separation).

**Constraint**: Bucket names must be globally unique in the Arvan namespace. Confirm the chosen name is available before coding.

---

### 1.3 S2 Cost Budget

**Question**: What is the monthly S2 storage and egress budget?

Arvan S2 pricing:
- Storage: ~0.025 USD / GB / month
- Egress (downloads): charges apply per GB transferred.

**Impact**: Determines the `MEDIA_TTL_DAYS` lifecycle rule (shorter TTL = lower storage cost). Also determines whether to use **presigned URLs** (direct S2 → user egress) or **proxy stream** (Iran VPS → user, no S2 egress but Iran VPS bandwidth cost).

---

## 2. File Delivery Strategy

### 2.1 Presigned URLs vs. Proxy Stream

**Question**: Should the Iran VPS hand out presigned URLs (direct S2 → user) or proxy the download through the Iran VPS?

| Factor | Presigned URL | Proxy Stream |
|--------|--------------|-------------|
| Iran VPS bandwidth | None (direct S2) | Full file traffic |
| Privacy | S2 URL visible to user | URL hidden |
| Revocation | Not possible after issue | Immediate |
| S2 egress cost | Yes (applies to S2 plan) | No (stays within Arvan) |
| Implementation complexity | Simple | Moderate |

**Recommendation**: Presigned URLs by default; proxy as opt-in fallback via `DOWNLOAD_MODE=proxy` env var.

**Owner confirmation needed**: Which is preferred?

---

### 2.2 Media Retention Policy

**Question**: How long should files be kept in S2 before automatic deletion?

Options: 1 day · 3 days · **7 days (recommended)** · 14 days · 30 days · forever.

**Impact**: `MEDIA_TTL_DAYS` setting and S2 lifecycle rules.

---

## 3. User Authentication

### 3.1 Registration Mode

**Question**: Which registration mode should be used?

| Mode | Description | Effort |
|------|-------------|--------|
| **Open registration + admin approval** | Anyone can register; admin approves each account | Low |
| **Invite code** | Admin generates codes; users register with a code | Medium |
| **Closed** | Admin creates accounts manually | Low |
| **Rubika OAuth** | Users authenticate via their Rubika account (if the API supports it) | High |

**Recommendation**: Start with "open registration + admin approval" (mirrors the existing whitelist behaviour).

---

### 3.2 Email Verification

**Question**: Is email verification required, or is admin approval sufficient?

**Impact**: If email verification is needed, an SMTP provider must be configured (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`).

---

## 4. Video Streaming

### 4.1 Video Streaming vs. Download-Only

**Question**: Should the Web UI support **streaming video** in the browser, or only download links?

Streaming video in the browser requires:
- Serving the `.mp4` file with `Accept-Ranges` support (S2 supports byte-range requests natively).
- An `<video>` element in the frontend.
- A somewhat larger Iran VPS bandwidth footprint if proxying.

**Impact**: The streaming player page in `webui-spec.md` section 3.8.

---

## 5. Multi-Language Priority

### 5.1 Persian vs. English

**Question**: Should Persian (Farsi) be the only language, or should English also be fully supported from day one?

**Recommendation**: Persian as the primary language (RTL layout); English as a secondary toggle (translated strings via `react-i18next`). English can be added progressively.

**Impact**: The `i18n` translation files `fa.json` and `en.json`.

---

## 6. Rubika Control Channel Account Setup

### 6.1 Dedicated Rubika Accounts

**Question**: Should the two VPSes use **dedicated new Rubika accounts** as the control channel (recommended), or the existing admin account?

**Recommendation**: Create two new Rubika accounts:
- `rubetunes_kharej_bot` (Kharej VPS side)
- `rubetunes_iran_ctrl` (Iran VPS side)

Each VPS stores its own account session. They send messages to each other's account GUIDs. This keeps the control channel separate from user interactions.

**Impact**: `RUBIKA_SESSION_KHAREJ`, `RUBIKA_SESSION_IRAN`, `IRAN_RUBIKA_ACCOUNT_GUID`, `KHAREJ_RUBIKA_ACCOUNT_GUID` env vars.

---

## 7. Existing Rubika State Migration

### 7.1 Migrating `state.json` to PostgreSQL

**Question**: Should the existing `state.json` (whitelist, banned list, usage logs) be migrated to the new PostgreSQL database, or should there be a clean start?

**Recommendation**: Run the one-time migration script (`scripts/migrate_state_json_to_db.py`) to import existing whitelisted/banned users with their status preserved.

**Impact**: A migration step in Phase 4 of the rollout plan.

---

## 8. Admin Panel Notification Channels

### 8.1 How Should the Admin Receive Registration Approval Notifications?

Options:
- Browser notification (when Admin Panel is open).
- Rubika chat message to the admin account (easiest, already available).
- Email.
- All of the above.

**Recommendation**: Rubika chat message to the admin account (no extra setup needed; uses the existing Rubika bot infrastructure) + in-browser notification badge on the Admin Panel.

---

## 9. Cookies File Transfer Security

### 9.1 Is Rubika Sufficient for Transmitting the `cookies.txt` File?

The `admin.cookies.update` message schema (section 11 of `message-schema.md`) uses S2 `tmp/` as the transfer medium for the cookies file (to avoid the 4 KB limit). The S2 object is then deleted immediately.

**Question**: Is this acceptable, or should a more secure channel (e.g., direct SSH SCP or an encrypted REST API between the two VPSes) be used?

**Impact**: The implementation of `admin.cookies.update` handling in Track A (A15).

---

## 10. Domain and TLS

### 10.1 Domain Name for Iran VPS Web UI

**Question**: What domain name will the Web UI be served from?

**Impact**: TLS certificate setup (Let's Encrypt via Certbot or Caddy), nginx `server_name`, `ALLOWED_ORIGINS` CORS setting.

---

## 11. Rubika Fallback for Control Bus

### 11.1 Fallback If Rubika Is Unavailable

**Question**: Should a direct HTTP/HTTPS fallback API between the two VPSes be implemented for the control channel (in case Rubika is unreachable)?

**Recommendation**: Yes — a simple `POST /internal/control` endpoint on the Kharej VPS (authenticated with a shared secret, only accessible from the Iran VPS IP). Activated automatically if no `health.pong` is received within 2 minutes.

**Impact**: An extra `KHAREJ_INTERNAL_API_URL` and `KHAREJ_INTERNAL_API_SECRET` env var; a minimal FastAPI endpoint on the Kharej side (Track A).
