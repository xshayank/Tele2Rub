"""Server-rendered UI routes for the Iran VPS service (Track B, Step 8 + 9).

Serves Jinja2 HTML pages for the end-user web interface and admin panel.
All pages are thin shells: they load the base layout, then use JavaScript
to talk to the existing JSON API endpoints.

Routes (user)
------
GET  /              Home / job-submit page
GET  /login         Login form
GET  /register      Registration form
GET  /pending       Post-registration "pending approval" notice
GET  /ui/jobs/{id}  Job progress + download page
GET  /library       Paginated job history page
GET  /settings      Account settings page

Routes (admin)
------
GET  /admin               Admin dashboard
GET  /admin/users         User management page
GET  /admin/registrations Pending registrations page
GET  /admin/jobs          Job monitor page
GET  /admin/storage       Storage management page
GET  /admin/settings      Settings editor page
GET  /admin/health        Provider health page
GET  /admin/audit         Audit log page
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
    import uuid as _uuid
    try:
        _uuid.UUID(job_id)
    except ValueError:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Job not found.")
    return templates.TemplateResponse(request, "job.html", {"job_id": job_id})


@router.get("/library", response_class=HTMLResponse, include_in_schema=False)
async def library_page(request: Request) -> HTMLResponse:
    """Paginated library of the user's own jobs."""
    return templates.TemplateResponse(request, "library.html")


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(request: Request) -> HTMLResponse:
    """Account settings page."""
    return templates.TemplateResponse(request, "settings.html")


# ---------------------------------------------------------------------------
# Admin UI pages (Step 9)
# ---------------------------------------------------------------------------


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard(request: Request) -> HTMLResponse:
    """Admin dashboard page."""
    return templates.TemplateResponse(request, "admin_dashboard.html")


@router.get("/admin/ui/users", response_class=HTMLResponse, include_in_schema=False)
async def admin_users_page(request: Request) -> HTMLResponse:
    """Admin user-management page."""
    return templates.TemplateResponse(request, "admin_users.html")


@router.get("/admin/ui/registrations", response_class=HTMLResponse, include_in_schema=False)
async def admin_registrations_page(request: Request) -> HTMLResponse:
    """Admin pending-registrations page."""
    return templates.TemplateResponse(request, "admin_registrations.html")


@router.get("/admin/ui/jobs", response_class=HTMLResponse, include_in_schema=False)
async def admin_jobs_page(request: Request) -> HTMLResponse:
    """Admin job-monitor page."""
    return templates.TemplateResponse(request, "admin_jobs.html")


@router.get("/admin/ui/storage", response_class=HTMLResponse, include_in_schema=False)
async def admin_storage_page(request: Request) -> HTMLResponse:
    """Admin storage-management page."""
    return templates.TemplateResponse(request, "admin_storage.html")


@router.get("/admin/ui/settings", response_class=HTMLResponse, include_in_schema=False)
async def admin_settings_page(request: Request) -> HTMLResponse:
    """Admin settings-editor page."""
    return templates.TemplateResponse(request, "admin_settings.html")


@router.get("/admin/ui/health", response_class=HTMLResponse, include_in_schema=False)
async def admin_health_page(request: Request) -> HTMLResponse:
    """Admin provider-health page."""
    return templates.TemplateResponse(request, "admin_health.html")


@router.get("/admin/ui/audit", response_class=HTMLResponse, include_in_schema=False)
async def admin_audit_page(request: Request) -> HTMLResponse:
    """Admin audit-log page."""
    return templates.TemplateResponse(request, "admin_audit.html")
