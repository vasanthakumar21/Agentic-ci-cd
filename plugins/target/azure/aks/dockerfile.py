"""The resilient Dockerfile the azure-aks ``build`` verb emits.

``build`` on azure-aks is ``docker build``, so the plugin must supply a Dockerfile for the client to build.
demands builds be *resilient* — they must not fall over because a distro package
mirror is slow, rate-limited, or briefly down. The classic failure is a ``RUN
apt-get update && apt-get install …`` layer that reaches a live Debian/Ubuntu mirror
on every build: when the mirror hiccups the whole pipeline goes red for reasons
unrelated to the change under test.

This file is a self-contained copy of the resilient multi-stage pattern used by the
other container targets (aws/eks, gcp/cloud-run) so the azure-aks plugin carries its
own build definition and does NOT import across plugin boundaries.

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
* **Deterministic** — no timestamps, no ``ARG`` defaulting to "now", stable layer
 order. The same inputs produce the same image definition every time.
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
# Resilient multi-stage build for the azure-aks target (generated — R7.1).
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

# --- runtime stage: copy only what we need to run -----------------------------
FROM {BASE_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Run as a non-root user — created without touching the package manager.
RUN useradd --create-home --uid 10001 appuser
USER appuser

# Bring in the installed dependencies from the builder stage, then the app code.
COPY --from=builder /install /usr/local
COPY . .

# The container entrypoint. A client overrides CMD for their own process; the
# default is a conventional module entrypoint and is easy to change.
CMD ["python", "-m", "app"]
"""
