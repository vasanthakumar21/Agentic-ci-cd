"""Kubernetes manifests the kubernetes plugin supplies as backing IaC.

 — the kubernetes ``IaC`` cell is **cluster refs, RBAC**: the plugin does
NOT provision a cluster (clusters are long-lived, client-owned infrastructure), it
supplies the *workload* manifests the ``deploy`` verb applies plus a Terraform view
using the kubernetes provider. This module renders the plain-YAML side: a
``Deployment`` and a ``Service``.

Why placeholders, not literals
------------------------------
The image (registry + repo + tag) and the namespace differ per client and per run.
Baking a real image reference or namespace in would make the manifests non-portable
and risk committing environment specifics. They are therefore emitted as ``${VAR}``
placeholders the deploy step substitutes (e.g. with ``envsubst``) from the
non-secret settings / references. No literal secret, token, or registry credential
appears — image pulls use the cluster's configured imagePullSecrets, referenced by
name only.

Determinism
------------------
YAML is assembled from fixed, ordered string templates — no timestamps, no random
suffixes, stable key order — so regenerating from the same IR is byte-identical.
"""

from __future__ import annotations

import re


def sanitize_name(raw: str) -> str:
    """Turn an IR project name into a safe, stable Kubernetes resource name.

 Kubernetes object names must be a DNS-1123 label: lower-case alphanumerics and
 ``-``, starting/ending alphanumeric. We lower-case, replace any other run of
 characters with a single ``-``, and trim stray dashes so the name is
 deterministic and valid.
 """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-").lower()
    return cleaned or "app"


def render_deployment_yaml(
    *, deployment_name: str, app_label: str, container_port: int = 8080
) -> str:
    """Render ``deployment.yaml`` — a ``kind: Deployment`` for the workload.

 The container image and namespace are ``${VAR}`` placeholders (never literals)
 so the manifest is portable and carries no environment specifics. The
 deploy verb substitutes them from the non-secret settings/references.
 """
    return f"""\
# Kubernetes Deployment for the kubernetes target (generated — design §8b).
#
# Image and namespace are ${{VAR}} placeholders substituted at deploy time from
# non-secret settings/references (R6) — no literal image, registry credential, or
# namespace is committed. Deterministic: stable key order, no timestamps (R5.3).
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {deployment_name}
  namespace: ${{NAMESPACE}}
  labels:
    app: {app_label}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {app_label}
  template:
    metadata:
      labels:
        app: {app_label}
    spec:
      containers:
        - name: {app_label}
          image: ${{IMAGE_REPO}}:${{IMAGE_TAG}}
          ports:
            - containerPort: {container_port}
          resources:
            requests:
              cpu: "100m"
              memory: "128Mi"
            limits:
              cpu: "500m"
              memory: "512Mi"
"""


def render_service_yaml(
    *, service_name: str, app_label: str, container_port: int = 8080
) -> str:
    """Render ``service.yaml`` — a ``kind: Service`` fronting the Deployment pods.

 The namespace is a ``${VAR}`` placeholder (never a literal) substituted at
 deploy time. Deterministic: stable key order, no timestamps.
 """
    return f"""\
# Kubernetes Service for the kubernetes target (generated — design §8b).
#
# Namespace is a ${{VAR}} placeholder substituted at deploy time (R6).
# Deterministic: stable key order, no timestamps (R5.3).
apiVersion: v1
kind: Service
metadata:
  name: {service_name}
  namespace: ${{NAMESPACE}}
  labels:
    app: {app_label}
spec:
  type: ClusterIP
  selector:
    app: {app_label}
  ports:
    - port: 80
      targetPort: {container_port}
      protocol: TCP
"""
