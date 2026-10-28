"""Kubernetes manifests the azure-aks plugin supplies as backing IaC.

 — the azure-aks ``IaC`` cell is **AKS refs + workload identity**: the
plugin does NOT provision an AKS cluster (clusters are long-lived, client-owned
infrastructure), it supplies the *workload* manifests the ``deploy`` verb applies
with ``kubectl`` / ``helm`` plus the Terraform that references the existing cluster,
provisions ACR, and wires an Azure user-assigned managed identity + AKS workload
identity + a federated credential for keyless CI (see :mod:`.terraform`). This
module renders the plain-YAML side: a ``Deployment``, a ``Service``, and a
``ServiceAccount`` annotated for **Azure Workload Identity**.

Why placeholders, not literals
------------------------------
The image (ACR login server + repo + tag/digest) and the namespace differ per client
and per run. Baking a real image reference or namespace in would make the manifests
non-portable and risk committing environment specifics. They are therefore emitted
as ``${VAR}`` placeholders the deploy step substitutes (e.g. with ``envsubst``) from
the non-secret settings / references. The ServiceAccount's Azure Workload Identity
client id is likewise a ``${VAR}`` placeholder — no tenant/subscription/client id is
committed. No literal secret, token, or registry credential appears; image pulls rely
on AKS's ACR attachment / workload identity.

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
    """Render ``serviceaccount.yaml`` — a ``ServiceAccount`` for Azure Workload Identity.

 The workload-identity client id is an ``${AZURE_CLIENT_ID}`` placeholder (never a
 literal) so no tenant/subscription/client id is committed; the deploy step
 substitutes it from the non-secret reference. The
 ``azure.workload.identity/use`` label on the pod template + this annotated
 ServiceAccount are the Azure-specific binding that lets the workload federate to
 an Azure AD identity via the cluster's OIDC issuer (AKS Workload Identity).
 """
    return f"""\
# Kubernetes ServiceAccount for the azure-aks target (generated — design §11.9).
#
# The azure.workload.identity/client-id annotation binds this ServiceAccount to an
# Azure AD (user-assigned managed) identity via AKS Workload Identity. The client id
# is an ${{VAR}} placeholder substituted at deploy time (R6) — no tenant,
# subscription, or client id is committed.
# Deterministic: stable key order, no timestamps (R5.3).
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {service_account_name}
  namespace: ${{NAMESPACE}}
  labels:
    app: {app_label}
    azure.workload.identity/use: "true"
  annotations:
    azure.workload.identity/client-id: ${{AZURE_CLIENT_ID}}
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
 is labelled ``azure.workload.identity/use: "true"`` and runs under the annotated
 ServiceAccount so it federates to its Azure AD identity (AKS Workload Identity).
 """
    return f"""\
# Kubernetes Deployment for the azure-aks target (generated — design §11.9).
#
# Image and namespace are ${{VAR}} placeholders substituted at deploy time from
# non-secret settings/references (R6) — no literal image, registry credential, or
# namespace is committed. The pod is labelled for Azure Workload Identity and runs
# under the annotated ServiceAccount so it federates to its Azure AD identity.
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
        azure.workload.identity/use: "true"
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
# Kubernetes Service for the azure-aks target (generated — design §11.9).
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
