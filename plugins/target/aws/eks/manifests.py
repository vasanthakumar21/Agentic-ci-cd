"""Kubernetes manifests the aws-eks plugin supplies as backing IaC.

 — the aws-eks ``IaC`` cell is **EKS refs + IRSA/OIDC**: the plugin does
NOT provision an EKS cluster (clusters are long-lived, client-owned infrastructure),
it supplies the *workload* manifests the ``deploy`` verb applies with ``kubectl`` /
``helm`` plus the Terraform that wires ECR, IRSA (IAM Roles for Service Accounts),
and the GitHub-OIDC deploy role (see :mod:`.terraform`). This module renders the
plain-YAML side: a ``Deployment``, a ``Service``, and a ``ServiceAccount`` annotated
for IRSA.

Why placeholders, not literals
------------------------------
The image (ECR registry + repo + tag/digest) and the namespace differ per client and
per run. Baking a real image reference or namespace in would make the manifests
non-portable and risk committing environment specifics. They are therefore emitted
as ``${VAR}`` placeholders the deploy step substitutes (e.g. with ``envsubst``) from
the non-secret settings / references. The ServiceAccount's IRSA role ARN is likewise
a ``${VAR}`` placeholder — no account id is committed. No literal secret, token, or
registry credential appears; image pulls rely on the node role / IRSA.

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
    """Render ``serviceaccount.yaml`` — a ``ServiceAccount`` annotated for IRSA.

 The IRSA role ARN is an ``${IRSA_ROLE_ARN}`` placeholder (never a literal) so no
 account id is committed; the deploy step substitutes it from the non-secret
 reference. This is the EKS-specific binding that lets the workload assume an
 IAM role via the cluster's OIDC provider (IAM Roles for Service Accounts).
 """
    return f"""\
# Kubernetes ServiceAccount for the aws-eks target (generated — design §11.9).
#
# The eks.amazonaws.com/role-arn annotation binds this ServiceAccount to an IAM
# role via IRSA (IAM Roles for Service Accounts). The role ARN is an ${{VAR}}
# placeholder substituted at deploy time (R6) — no account id is committed.
# Deterministic: stable key order, no timestamps (R5.3).
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {service_account_name}
  namespace: ${{NAMESPACE}}
  labels:
    app: {app_label}
  annotations:
    eks.amazonaws.com/role-arn: ${{IRSA_ROLE_ARN}}
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
 runs under the IRSA-annotated ServiceAccount so it assumes its scoped IAM role.
 """
    return f"""\
# Kubernetes Deployment for the aws-eks target (generated — design §11.9).
#
# Image and namespace are ${{VAR}} placeholders substituted at deploy time from
# non-secret settings/references (R6) — no literal image, registry credential, or
# namespace is committed. The pod runs under the IRSA ServiceAccount so it assumes
# its scoped IAM role. Deterministic: stable key order, no timestamps (R5.3).
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
# Kubernetes Service for the aws-eks target (generated — design §11.9).
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
