"""The resilient Dockerfile the aws-sagemaker ``build`` verb emits.

``build`` on aws-sagemaker (the bring-your-own-container path) is ``docker build``, so the plugin must supply a Dockerfile for the
client to build. demands builds be *resilient* — they must not fall over
because a distro package mirror is slow, rate-limited, or briefly down.

How this Dockerfile stays resilient
------------------------------------
* **Pinned base image** — ``python:3.12-slim`` (a specific minor, not ``latest``).
* **No live apt mirror dependency** — there is deliberately NO ``apt-get update`` /
 ``apt-get install`` step. Dependencies come from ``pip install`` against a pinned
 ``requirements.txt`` (PyPI only).
* **Multi-stage** — a ``builder`` stage installs wheels into an isolated prefix; the
 final ``runtime`` stage copies only the installed site-packages and the app.
* **Deterministic** — no timestamps, stable layer order.

A SageMaker bring-your-own-container inference image must respond to ``docker run
<image> serve`` and expose a ``/ping`` + ``/invocations`` HTTP contract on port
8080; the default ``ENTRYPOINT`` runs the model server module which a client
implements.
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
# Resilient multi-stage build for the aws-sagemaker target (generated — R7.1).
#
# Design notes:
#   * Base image is PINNED ({BASE_IMAGE}) for reproducible, cache-friendly builds.
#   * There is deliberately NO distro package-manager step (no fetch from a live
#     Debian/Ubuntu mirror): the build must not depend on a package mirror that
#     can be slow or down. Dependencies are installed with pip from a pinned
#     requirements file (PyPI only). A client that truly needs system libraries can
#     add them, but the generated default stays resilient.
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

# --- runtime stage: a SageMaker bring-your-own-container inference image -------
FROM {BASE_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \\
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Bring in the installed dependencies from the builder stage, then the app code.
COPY --from=builder /install /usr/local
COPY . .

# SageMaker invokes the image as `docker run <image> serve`; the model server must
# expose /ping and /invocations on port 8080. A client implements the server module.
EXPOSE 8080
ENTRYPOINT ["python", "-m", "model_server"]
"""
