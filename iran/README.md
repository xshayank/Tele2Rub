# Iran Service (Track B)

Iran-side Python web service and admin panel for the RubeTunes music-download
platform. Receives download requests from end-users over HTTP, forwards them to
the Kharej VPS worker over a Rubika control channel, and streams progress events
back to the browser via Server-Sent Events (SSE).

See the full implementation roadmap at
[`docs/research/arvan-webui-migration/track-b-steps.md`](../docs/research/arvan-webui-migration/track-b-steps.md).

## Requirements

- Python 3.11+
- Dependencies listed in `iran/requirements.txt`

## Running locally

```bash
# 1. Install dependencies (ideally in a virtual environment)
pip install fastapi "uvicorn[standard]" pydantic-settings

# 2. (Optional) create a .env file
cp .env.example .env   # edit as needed

# 3. Start the development server
python -m iran

# 4. Open the interactive API docs
open http://localhost:8000/docs
```

## CLI flags

```
python -m iran --help          # show all options
python -m iran --version       # print service version
python -m iran --check-config  # validate env-var configuration
python -m iran --port 9000     # override listen port
```

## Health check

```bash
curl http://localhost:8000/health
# {"status":"ok","service":"iran","version":"0.1.0","contract_version":1}
```

## Package structure (Step 1 skeleton)

```
iran/
├── __init__.py          # package + __version__
├── __main__.py          # python -m iran entrypoint
├── main.py              # FastAPI app factory + lifespan
├── config.py            # IranSettings (pydantic-settings, IRAN_ prefix)
├── logging_setup.py     # JSON / text logging configuration
├── rubika_client.py     # Rubika transport stub (wired in Step 5)
├── s2_client.py         # S2 read-only client stub (wired in Step 6)
├── event_bus.py         # SSE/WS event bus stub (wired in Step 7)
├── api/
│   ├── __init__.py
│   ├── health.py        # GET /health
│   ├── auth.py          # (Step 4)
│   ├── jobs.py          # (Step 7)
│   ├── downloads.py     # (Step 7)
│   ├── search.py        # (Step 7)
│   └── admin.py         # (Step 9)
└── requirements.txt     # pinned runtime + test deps
```
