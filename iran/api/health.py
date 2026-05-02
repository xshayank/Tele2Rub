"""Health check router for the Iran VPS service.

Provides a single, unauthenticated ``GET /health`` endpoint that returns
the service status and contract version.  Used by load-balancers,
monitoring agents, and the Kharej worker's ``HealthPing`` flow.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

import iran
from kharej.contracts import CONTRACT_VERSION

logger = logging.getLogger("iran.api.health")

router = APIRouter(tags=["health"])


@router.get(
    "/health",
    summary="Service health check",
    response_description="Service status and version information",
)
async def health() -> dict[str, Any]:
    """Return ``200 OK`` when the service is reachable.

    Response body::

        {
            "status": "ok",
            "service": "iran",
            "version": "0.1.0",
            "contract_version": 1
        }
    """
    return {
        "status": "ok",
        "service": "iran",
        "version": iran.__version__,
        "contract_version": CONTRACT_VERSION,
    }


@router.get(
    "/healthz",
    summary="Liveness probe",
    response_description="Service liveness status",
)
async def healthz() -> dict[str, Any]:
    """Kubernetes / Docker liveness probe endpoint.

    Returns ``200 {"status": "ok"}`` when the service process is alive.
    Used by the Docker Compose ``healthcheck`` and by load-balancer
    readiness checks.
    """
    return {
        "status": "ok",
        "service": "iran",
        "version": iran.__version__,
        "contract_version": CONTRACT_VERSION,
    }
