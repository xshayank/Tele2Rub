"""Server-rendered UI routes for the Iran VPS service (Track B, Step 8).

Serves Jinja2 HTML pages for the end-user web interface.  All pages are
thin shells: they load the base layout, then use JavaScript to talk to the
existing JSON API endpoints (auth, jobs, SSE stream, download).

Routes
------
GET  /              Home / job-submit page
GET  /login         Login form
GET  /register      Registration form
GET  /pending       Post-registration "pending approval" notice
GET  /ui/jobs/{id}  Job progress + download page
GET  /library       Paginated job history page
GET  /settings      Account settings page
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import pathlib

_TEMPLATES_DIR = pathlib.Path(__file__).parent.parent / "templates"

templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

router = APIRouter(tags=["ui"])


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def home(request: Request) -> HTMLResponse:
    """Home page — URL submission form."""
    return templates.TemplateResponse(request, "index.html")


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request) -> HTMLResponse:
    """Login form page."""
    return templates.TemplateResponse(request, "login.html")


@router.get("/register", response_class=HTMLResponse, include_in_schema=False)
async def register_page(request: Request) -> HTMLResponse:
    """Registration form page."""
    return templates.TemplateResponse(request, "register.html")


@router.get("/pending", response_class=HTMLResponse, include_in_schema=False)
async def pending_page(request: Request) -> HTMLResponse:
    """Post-registration 'pending approval' notice."""
    return templates.TemplateResponse(request, "pending.html")


@router.get("/ui/jobs/{job_id}", response_class=HTMLResponse, include_in_schema=False)
async def job_page(request: Request, job_id: str) -> HTMLResponse:
    """Job progress + download page."""
    return templates.TemplateResponse(request, "job.html", {"job_id": job_id})


@router.get("/library", response_class=HTMLResponse, include_in_schema=False)
async def library_page(request: Request) -> HTMLResponse:
    """Paginated library of the user's own jobs."""
    return templates.TemplateResponse(request, "library.html")


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(request: Request) -> HTMLResponse:
    """Account settings page."""
    return templates.TemplateResponse(request, "settings.html")
