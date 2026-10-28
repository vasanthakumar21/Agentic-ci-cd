"""The resilient Dockerfile the gcp-cloud-run ``build`` verb emits.

``build`` on gcp-cloud-run is ``docker build`` — Cloud Run runs a
container image exactly like aws-ecs / aws-eks — so the plugin must supply a
Dockerfile for the client to build. demands builds be *resilient*: they must
not fall over because a distro package mirror is slow, rate-limited, or briefly
down. The classic failure is a ``RUN apt-get update && apt-get install …`` layer
that reaches a live Debian/Ubuntu mirror on every build: when the mirror hiccups the
whole pipeline goes red for reasons unrelated to the change under test.

This file is a self-contained copy of the resilient multi-stage pattern used by
``plugins/target/aws/ecs/dockerfile.py`` so the gcp-cloud-run plugin carries its own
build definition and does NOT import across plugin boundaries.

How this Dockerfile stays resilient
------------------------------------
* **Pinned base image** — ``python:3.12-slim`` (a specific minor, not ``latest``),
 so the base layer is reproducible and cached, and the build does not silently
 drift when a new Python point release lands.
* **No live apt mirror dependency** — there is deliberately NO ``apt-get update`` /
 ``apt-get install`` step. The slim image already carries a CPython runtime and
 the TLS/CA roots pip needs; application dependencies come from ``pip install``
 against a pinned ``requirements.txt``, which pulls from PyPI (the same registry
 the app already depends on) rather than adding a *second* fragile network source.
* **Multi-stage** — a ``builder`` stage installs wheels into a throwaway prefix;
 the final ``runtime`` stage copies only the installed site-packages and the app,
 so the shipped image is small and carries no build-time cruft.
* **Cloud Run shaped** — Cloud Run injects the listening port via ``$PORT`` (default
 8080) and expects the container to bind ``0.0.0.0:$PORT``. The default ``CMD`` is a
 conventional module entrypoint a client overrides; the ``ENV PORT=8080`` default
 keeps the image runnable locally without relying on Cloud Run to inject it.
* **Deterministic** — no timestamps, no ``ARG`` defaulting to "now", stable layer
 order. The same inputs produce the same image definition every time.

The content is intentionally a sensible, correct *default*. A client with native
extensions that genuinely need system libraries can extend it, but the generated
starting point never depends on a live apt mirror.
"""

from __future__ import annotations

#: The pinned base image for BOTH stages. A specific minor version (not ``latest``,
#: not a bare ``3``) keeps builds reproducible and cache-friendly.
BASE_IMAGE = "python:3.12-slim"

#: The resilient, multi-stage Dockerfile emitted as a support file by ``build``.
#: NOTE: no ``apt-get update``/``apt-get install`` anywhere — see module docstring.
DOCKERFILE_CONTENT = f"""\
# syntax=docker/dockerfile:1
#
# Resilient multi-stage build for the gcp-cloud-run target (generated — R7.1).
#
# Design notes:
#   * Base image is PINNED ({BASE_IMAGE}) for reproducible, cache-friendly builds.
#   * There is deliberately NO distro package-manager step (no fetch from a live
#     Debian/Ubuntu mirror): the build must not depend on a package mirror that
#     can be slow or down. Application dependencies are installed with pip from a
#     pinned requirements file (PyPI only). A client that truly needs system
#     libraries can add them, but the generated default stays resilient.
#   * Cloud Run shaped: the container binds 0.0.0.0:$PORT ($PORT is injected by
#     Cloud Run; the ENV default keeps the image runnable locally).
#   * Deterministic: stable layer order, no timestamps, no "latest" tags.

# --- builder stage: install dependencies into an isolated prefix --------------
FROM {BASE_IMAGE} AS builder

# Keep pip quiet, reproducible, and free of a local wheel cache layer.
ENV PIP_NO_CACHE_DIR=1 \\
    PIP_DISABLE_PIP_VERSION_CHECK=1 \\
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so this layer caches independently of app changes.
# Requirements are PINNED by the client; pip pulls from PyPI (no apt mirror).
COPY requirements.txt ./
RUN python -m pip install --prefix=/install -r requirements.txt

# --- runtime stage: copy only what we need to run -----------------------------
FROM {BASE_IMAGE} AS runtime

# Cloud Run injects $PORT; default it so the image runs locally too.
ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PORT=8080

WORKDIR /app

# Run as a non-root user — created without touching the package manager.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# Bring in the installed dependencies from the builder stage, then the app code.
COPY --from=builder /install /usr/local
COPY . .

# Cloud Run sends requests to the port named by $PORT. The container entrypoint
# must bind 0.0.0.0:$PORT. A client overrides CMD for their own process; the
# default is a conventional module entrypoint and is easy to change.
EXPOSE 8080
CMD ["python", "-m", "app"]
"""
