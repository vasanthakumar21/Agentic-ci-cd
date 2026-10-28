"""The resilient Dockerfile the aws-batch ``build`` verb emits.

``build`` on aws-batch is ``docker build``, so the plugin must supply a Dockerfile for the client to build. demands
builds be *resilient* — they must not fall over because a distro package mirror is
slow, rate-limited, or briefly down. The classic failure is a ``RUN apt-get update
&& apt-get install …`` layer that reaches a live Debian/Ubuntu mirror on every
build: when the mirror hiccups the whole pipeline goes red for reasons unrelated to
the change under test.

How this Dockerfile stays resilient
------------------------------------
* **Pinned base image** — ``python:3.12-slim`` (a specific minor, not ``latest``).
* **No live apt mirror dependency** — there is deliberately NO ``apt-get update`` /
 ``apt-get install`` step. Application dependencies come from ``pip install``
 against a pinned ``requirements.txt`` (PyPI only).
* **Multi-stage** — a ``builder`` stage installs wheels into an isolated prefix; the
 final ``runtime`` stage copies only the installed site-packages and the app.
* **Deterministic** — no timestamps, stable layer order.

A Batch job's container is a batch worker (not a long-running service), so the
default ``CMD`` runs the job module once rather than serving traffic.
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
# Resilient multi-stage build for the aws-batch target (generated — R7.1).
#
# Design notes:
#   * Base image is PINNED ({BASE_IMAGE}) for reproducible, cache-friendly builds.
#   * There is deliberately NO distro package-manager step (no fetch from a live
#     Debian/Ubuntu mirror): the build must not depend on a package mirror that
#     can be slow or down. Application dependencies are installed with pip from a
#     pinned requirements file (PyPI only). A client that truly needs system
#     libraries can add them, but the generated default stays resilient.
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

# --- runtime stage: copy only what we need to run the batch job ----------------
FROM {BASE_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Run as a non-root user — created without touching the package manager.
RUN useradd --create-home --uid 10001 batchuser
USER batchuser

# Bring in the installed dependencies from the builder stage, then the app code.
COPY --from=builder /install /usr/local
COPY . .

# A Batch job runs the job module once and exits (it is a worker, not a service).
# A client overrides CMD for their own batch entrypoint.
CMD ["python", "-m", "job"]
"""
