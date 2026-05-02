# RubeTunes Kharej Worker

The Kharej VPS worker is responsible for:

- Consuming Rubika control messages (`job.create`) sent by the Iran VPS.
- Downloading requested media (YouTube, Spotify, Tidal, Qobuz, Amazon Music,
  SoundCloud, Bandcamp, musicdl) using the downloader adapters in
  `kharej/downloaders/`.
- Pushing completed files to **Arvan S2 Object Storage** so the Iran Web UI
  can serve them to end-users without any binary payload travelling over
  Rubika.
- Publishing lifecycle events (`job.progress`, `job.done`, `job.error`) back
  to the Rubika channel so the Iran-side UI can update download status in
  real time.

---

## Research docs

- [`../docs/research/arvan-webui-migration/track-a-steps.md`](../docs/research/arvan-webui-migration/track-a-steps.md) — full 12-step Track A plan
- [`../docs/research/arvan-webui-migration/task-split.md`](../docs/research/arvan-webui-migration/task-split.md) — Track A scope
- [`../docs/research/arvan-webui-migration/architecture.md`](../docs/research/arvan-webui-migration/architecture.md) — overall architecture

---

## Status

**Step 1 of 12 — package skeleton only. No functionality yet.**

Steps 2–12 will progressively fill in the stubs with real implementations.

---

## Quickstart

```bash
python -m kharej --help
python -m kharej.worker --healthcheck
pytest kharej/tests
```
