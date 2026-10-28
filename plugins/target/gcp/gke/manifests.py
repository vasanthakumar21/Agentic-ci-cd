"""Kubernetes manifests the gcp-gke plugin supplies as backing IaC.

 — the gcp-gke ``IaC`` cell is **GKE refs + Workload Identity**: the
plugin does NOT provision a GKE cluster (clusters are long-lived, client-owned
infrastructure), it supplies the *workload* manifests the ``deploy`` verb applies
with ``kubectl`` / ``helm`` plus the Terraform that references the existing cluster
and wires Artifact Registry, a deploy service account, GKE Workload Identity, and a
Workload Identity Federation pool/provider for keyless CI (see :mod:`.terraform`).
This module renders the plain-YAML side: a ``Deployment``, a ``Service``, and a
``ServiceAccount`` annotated for GKE Workload Identity.

GKE Workload Identity vs EKS IRSA
---------------------------------
The GKE-native keyless story binds a Kubernetes ServiceAccount (KSA) to a Google
service account (GSA) via the ``iam.gke.io/gcp-service-account`` annotation. That
annotation is the GKE analogue of EKS's ``eks.amazonaws.com/role-arn`` — it is what
makes this plugin GKE-specific rather than a generic kubernetes plugin. The GSA
email is a ``${VAR}`` placeholder (never a literal) so no project specific is
committed.

Why placeholders, not literals
------------------------------
The image (Artifact Registry host + repo + tag/digest) and the namespace differ per
client and per run. Baking a real image reference or namespace in would make the
manifests non-portable and risk committing environment specifics. They are therefore
emitted as ``${VAR}`` placeholders the deploy step substitutes (e.g. with
``envsubst``) from the non-secret settings / references. No literal secret, token, or
service-account key appears — image pulls use the node's/GKE Workload Identity, and
the GSA binding is referenced by name only.

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


def render_serviceaccount_yaml(*, service_account_name: str, app_label: str) -> str:
    """Render ``serviceaccount.yaml`` — a ``ServiceAccount`` bound to a GSA (GKE WI).

 The Google service account email is an ``${GSA_EMAIL}`` placeholder (never a
 literal) so no project specific is committed; the deploy step substitutes it from
 the non-secret reference. This is the GKE-specific binding (GKE Workload
 Identity) that lets the workload impersonate a Google service account — the
 GCP analogue of EKS IRSA.
 """
    return f"""\
# Kubernetes ServiceAccount for the gcp-gke target (generated — design §11.9).
#
# The iam.gke.io/gcp-service-account annotation binds this Kubernetes ServiceAccount
# to a Google service account via GKE Workload Identity (the GCP analogue of EKS
# IRSA). The GSA email is an ${{VAR}} placeholder substituted at deploy time (R6) —
# no project id or key is committed. Deterministic: stable key order (R5.3).
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {service_account_name}
  namespace: ${{NAMESPACE}}
  labels:
    app: {app_label}
  annotations:
    iam.gke.io/gcp-service-account: ${{GSA_EMAIL}}
"""


def render_deployment_yaml(
    *,
    deployment_name: str,
    app_label: str,
    service_account_name: str,
    container_port: int = 8080,
) -> str:
    """Render ``deployment.yaml`` — a ``kind: Deployment`` for the workload.

 The container image and namespace are ``${VAR}`` placeholders (never literals)
 so the manifest is portable and carries no environment specifics. The
 deploy verb substitutes them from the non-secret settings/references. The pod
 runs under the Workload-Identity-annotated ServiceAccount so it impersonates its
 bound Google service account.
 """
    return f"""\
# Kubernetes Deployment for the gcp-gke target (generated — design §11.9).
#
# Image and namespace are ${{VAR}} placeholders substituted at deploy time from
# non-secret settings/references (R6) — no literal image, registry credential, or
# namespace is committed. The pod runs under the GKE Workload Identity
# ServiceAccount so it impersonates its bound Google service account.
# Deterministic: stable key order, no timestamps (R5.3).
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
      serviceAccountName: {service_account_name}
      containers:
        - name: {app_label}
          image: ${{IMAGE_REF}}
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
# Kubernetes Service for the gcp-gke target (generated — design §11.9).
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
