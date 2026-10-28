# syntax=docker/dockerfile:1
#
# Resilient multi-stage build for the aws-sagemaker target (generated — R7.1).
#
# Design notes:
#   * Base image is PINNED (python:3.12-slim) for reproducible, cache-friendly builds.
#   * There is deliberately NO distro package-manager step (no fetch from a live
#     Debian/Ubuntu mirror): the build must not depend on a package mirror that
#     can be slow or down. Dependencies are installed with pip from a pinned
#     requirements file (PyPI only). A client that truly needs system libraries can
#     add them, but the generated default stays resilient.
#   * Deterministic: stable layer order, no timestamps, no "latest" tags.

# --- builder stage: install dependencies into an isolated prefix --------------
FROM python:3.12-slim AS builder

# Keep pip quiet, reproducible, and free of a local wheel cache layer.
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first so this layer caches independently of app changes.
# Requirements are PINNED by the client; pip pulls from PyPI (no apt mirror).
COPY requirements.txt ./
RUN python -m pip install --prefix=/install -r requirements.txt

# --- runtime stage: a SageMaker bring-your-own-container inference image -------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Bring in the installed dependencies from the builder stage, then the app code.
COPY --from=builder /install /usr/local
COPY . .

# SageMaker invokes the image as `docker run <image> serve`; the model server must
# expose /ping and /invocations on port 8080. A client implements the server module.
EXPOSE 8080
ENTRYPOINT ["python", "-m", "model_server"]
