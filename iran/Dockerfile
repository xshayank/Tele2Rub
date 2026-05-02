# iran/Dockerfile — Multi-stage build for the Iran VPS service (Track B, Step 11).
#
# Stage 1 builds the React SPA; Stage 2 packages the Python API with the
# bundled static files so a single image can serve both.

# ── Stage 1: React SPA build ──────────────────────────────────────────────
FROM node:20-slim AS frontend
WORKDIR /app/web
COPY iran/web/package*.json ./
RUN npm ci
COPY iran/web/ ./
RUN npm run build             # outputs to iran/web/dist/

# ── Stage 2: Python API + bundled static files ────────────────────────────
FROM python:3.11-slim
WORKDIR /app

# Install curl for the Docker healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY iran/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the Iran service source
COPY iran/ ./iran/

# Copy the compiled React SPA into the static files directory
COPY --from=frontend /app/web/dist ./iran/static/

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/healthz || exit 1

ENTRYPOINT ["uvicorn", "iran.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
