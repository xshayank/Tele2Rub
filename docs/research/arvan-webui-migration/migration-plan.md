# Migration Plan — Arvan WebUI Migration

> See also: [architecture.md](architecture.md) · [task-split.md](task-split.md) · [open-questions.md](open-questions.md)

---

## Overview

The migration is structured as a **5-phase rollout** that maintains backward compatibility at every step.  The existing Rubika bot continues to work for users throughout the migration; the new Web UI is introduced in parallel.

---

## Phase 1: Introduce Arvan S2 Module (Parallel Run)

**Goal**: Get the S2 infrastructure in place and validated while the existing file-transfer-via-Rubika code path remains fully operational.

**Deliverables**:
- Arvan Cloud S2 bucket created with the two-credential split (write for Kharej, read for Iran).
- Lifecycle rules configured (`media/` → delete after 7 days, `tmp/` → delete after 24 h).
- `kharej/s2_client.py` implemented (Track A).
- Modified Kharej Worker uploads files to S2 **in addition to** sending them via Rubika (dual write).
- Iran VPS can verify that the S2 objects exist and checksums match.
- No user-visible change.

**Success criteria**:
- 100 files uploaded to S2 in staging environment with zero failures over a 48-hour period.
- Presigned URLs generated and tested for download.
- Lifecycle rules verified (old test objects deleted automatically).

**Risk**: Arvan S2 availability / latency. Mitigated by: dual-write keeps Rubika as fallback; test in staging first.

---

## Phase 2: Switch Downloads to S2 with Rubika Fallback

**Goal**: S2 becomes the primary file-transfer path; Rubika file upload is the fallback.

**Deliverables**:
- Kharej Worker: after S2 upload succeeds, sends `job.completed` with S2 key via Rubika control message instead of uploading the binary to Rubika.
- Iran VPS Rubika client: on receiving `job.completed`, generates presigned URL and delivers it back to the user via Rubika chat (`[Download your file](presigned_url)`).
- Rubika file-upload code path remains in place but is only triggered if S2 upload fails.
- Upload retry queue (`upload_retry.py`) is adapted to retry S2 uploads, not Rubika uploads.

**Backward compatibility**: Users still interact via Rubika; they receive a download link instead of a file attachment. The UX change is minimal and can be communicated to users.

**Success criteria**:
- 99% of downloads deliver via S2 presigned URL.
- Fallback to Rubika file upload triggers correctly on S2 failure.
- Existing Rubika users can download via the presigned link on mobile and desktop.

**Risk**: Users may be surprised by receiving a link instead of a file. Mitigate by: friendly message text ("Your file is ready — tap to download"); test with a small group first.

---

## Phase 3: Launch Web UI Beta (Whitelisted Users)

**Goal**: The Iran VPS Web UI is deployed and available to a selected group of users.

**Deliverables**:
- Iran VPS docker-compose deployed in production (Track B).
- Registration flow live: users can sign up and wait for admin approval.
- Admin Panel functional: admin can approve/reject registrations, view queue, view S2 storage.
- Search, download, quality picker, real-time progress, My Library pages live.
- Streaming audio player functional.
- Persian RTL UI with Vazirmatn font.
- HTTPS via Let's Encrypt (nginx/Caddy).

**Who can access**: Admin approves accounts manually during beta. Invite code mode optional.

**Backward compatibility**: Rubika bot still works for all existing users. Web UI is a new access point, not a replacement yet.

**Success criteria**:
- 10–20 beta users actively using the Web UI for at least 2 weeks.
- Zero data leaks (presigned URLs not accessible to non-authenticated users).
- All 14 feature categories from `current-features.md` available in the Web UI.

---

## Phase 4: Launch Admin Panel & Open Registration

**Goal**: Admin Panel is fully operational; registration opens to all users (or invite-code-gated).

**Deliverables**:
- Admin Panel: all pages from `webui-spec.md` section 4 live.
- Settings editor: admin can update rate limits, provider keys, cookies without redeploying.
- Audit log: all user and admin actions logged.
- Provider health dashboard: live.
- S2 storage usage dashboard: live.
- Optional: open registration (if owner decides — see open-questions.md).
- Sentry and Prometheus metrics for Iran VPS live.

**Backward compatibility**: Rubika bot continues to work. The `state.json` whitelist is migrated to the DB by a one-time script.

**Migration script**: `scripts/migrate_state_json_to_db.py` — reads `state.json`, inserts users into the `users` table with appropriate `status`.

**Success criteria**:
- Admin can approve/block users without touching the server.
- All settings can be updated without redeploying.
- Audit log captures all admin actions.

---

## Phase 5: Remove Rubika File-Transfer Code Path

**Goal**: Clean up the legacy Rubika file-upload code. Rubika is now used exclusively for control messages.

**Deliverables**:
- Remove `upload_retry.py` Rubika upload retry logic (replace with S2 retry logic that was already in place since Phase 2).
- Remove `app.send_document(...)` calls from `rub.py` (these were the Rubika file upload calls).
- Simplify the Kharej Worker: remove the dual-write logic introduced in Phase 1.
- Update `!download` / `!spotify` etc. responses in the Rubika bot to always return a presigned URL link.
- Archive the old `rub.py` `_do_download` (binary send) code path with a `DEPRECATED` comment.
- Update documentation.

**Backward compatibility**: Rubika users still receive download links (as established in Phase 2). No functional regression.

**Success criteria**:
- Zero `send_document` calls remain in the active code path.
- All downloads deliver via S2 presigned URLs.
- Rubika message sizes are consistently under 1 KB (only JSON control messages).

---

## Backward Compatibility Notes

| Phase | Rubika Bot | Web UI | Files via Rubika | Files via S2 |
|-------|-----------|--------|-----------------|-------------|
| Current | ✅ Full | ❌ | ✅ (binary) | ❌ |
| Phase 1 | ✅ Full | ❌ | ✅ (binary, primary) | ✅ (parallel write) |
| Phase 2 | ✅ Full (links) | ❌ | ✅ (fallback only) | ✅ (primary) |
| Phase 3 | ✅ Full (links) | ✅ Beta | ✅ (fallback only) | ✅ (primary) |
| Phase 4 | ✅ Full (links) | ✅ GA | ✅ (fallback only) | ✅ (primary) |
| Phase 5 | ✅ Links only | ✅ GA | ❌ (removed) | ✅ (only) |

---

## Risk Register

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|-----------|
| **Arvan S2 outage** | Low–Medium | High | Fallback to Rubika file transfer kept until Phase 5. S3-compatible — can switch to another provider (MinIO, Backblaze B2) by changing `endpoint_url`. |
| **Arvan S2 high egress cost** | Medium | Medium | Use presigned URLs (user downloads directly from S2, no Iran VPS egress). Set lifecycle rules to minimise stored data. Monitor usage via Admin Panel. Confirm cost with owner before launch. |
| **Rubika outage / API change** | Medium | High | Critical: Rubika is the control bus. Mitigation: implement a thin HTTP fallback API between the two VPSes (authenticated REST endpoint on Kharej that accepts job.create). Activate if Rubika is down >5 min. |
| **Rubika file transfer removal breaking existing users** | Low | Medium | Phase 2 introduces presigned URL links gracefully. Communicate change to users before Phase 5. |
| **Kharej VPS disk full during batch download** | Low | Medium | Disk guard (`disk_guard.py`) already in place. Monitor via Prometheus gauge. |
| **S2 credentials leaked** | Low | Critical | Rotate credentials immediately. Split write/read credentials already limits blast radius. |
| **Presigned URL shared without authorisation** | Medium | Low | Short TTL (1 hour). Optional single-use token wrapper. |
| **Iran VPS overloaded by proxy streams** | Medium | Medium | Use presigned URLs (direct S2 to user) by default; proxy only as fallback. Rate-limit proxy endpoints. |
| **PostgreSQL data loss** | Very Low | Critical | Daily automated backups. Use Arvan Cloud managed DB or self-managed with WAL archiving. |
| **React build / CDN delivery slow for Iranian users** | Medium | Medium | Serve static assets from the Iran VPS (nginx) directly, not from a CDN that may be filtered. |
| **musicdl license compliance** (PolyForm Noncommercial) | Low | High | Ensure RubeTunes is used non-commercially. Do not bundle musicdl in a paid SaaS. |
| **Spotify TOTP secret rotation** | Low | High | `SPOTIFY_TOTP_SECRET` env var override already supported. Monitor Spotify auth failures in Sentry. |

---

## Estimated Timeline

| Phase | Tracks active | Duration |
|-------|--------------|----------|
| Phase 1 | A | 1 week |
| Phase 2 | A | 1 week |
| Phase 3 | A + B | 3–4 weeks |
| Phase 4 | B | 2 weeks |
| Phase 5 | A + B | 1 week |
| **Total** | | **~8–9 weeks** |

Note: Track A (20 dev-days) and Track B (44 dev-days) run in parallel from Phase 3 onward.  Track B is the longer track; Track A should have Phase 1 and 2 work done before Track B starts Phase 3.
