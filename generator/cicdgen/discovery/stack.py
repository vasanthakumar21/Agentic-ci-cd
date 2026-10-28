"""Stack discovery — inspect a client repo and propose a draft IR.

``discover_stack(repo_root)`` scans the repo for deterministic filesystem
signals (no network) and infers, with evidence + confidence:

 * ci_vendor — from existing CI files
 * target_platform — from deploy signals (Dockerfile/ECS, notebooks,
 host.json, k8s manifests, …)
 * cloud.provider — from aws/azure/gcp hints
 * project.name — the repo directory name
 * project.language — from manifest files (pyproject.toml, package.json, …)
 * stages / environments — sensible defaults, refined by signals

The result is a :class:`StackProposal`: a *draft* IR (dict + a validated
:class:`~cicdgen.ir.models.Pipeline` where every required field resolves),
per-field evidence, per-axis confidence, and a list of assumptions/ambiguities
the developer must confirm.

This module PROPOSES only. It performs **no writes**: it does not create,
modify, or delete any file in the repo or anywhere else. It never silently
assumes a platform — every inference carries its confidence and, when weak, an
explicit "please confirm" assumption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from cicdgen.ir.models import Pipeline


# ---------------------------------------------------------------------------
# Confidence + evidence
# ---------------------------------------------------------------------------
class Confidence(StrEnum):
    """How strongly the signals support an inference."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class Evidence:
    """Why a single axis was inferred the way it was.

 Attributes:
 axis: the field inferred (e.g. ``ci_vendor``, ``target_platform``).
 value: the inferred value (or ``None`` when nothing could be inferred).
 confidence: how strongly the signals support it.
 signals: the triggering file(s)/path(s), repo-relative, that drove it.
 note: a human-readable explanation / assumption to confirm.
 """

    axis: str
    value: str | None
    confidence: Confidence
    signals: list[str] = field(default_factory=list)
    note: str = ""


@dataclass(frozen=True)
class StackProposal:
    """A proposed draft IR for the developer to confirm.

 Attributes:
 repo_root: the inspected repo root (absolute).
 ir: the proposed draft IR as a plain dict (what becomes pipeline.yaml).
 pipeline: a validated :class:`Pipeline` when the draft is complete
 enough to validate; ``None`` when it could only be marked draft.
 evidence: per-axis :class:`Evidence`, keyed by axis name.
 assumptions: ambiguities/assumptions the developer must confirm before
 generation — never resolved silently.

 This object is pure data. It is returned to the developer for confirmation;
 nothing here writes to disk.
 """

    repo_root: Path
    ir: dict[str, Any]
    pipeline: Pipeline | None
    evidence: dict[str, Evidence]
    assumptions: list[str] = field(default_factory=list)

    @property
    def is_draft(self) -> bool:
        """True when the proposal could not fully validate as a Pipeline.

 A draft that cannot validate is still useful — it is handed back for the
 developer to complete — but it is explicitly marked rather than passed
 off as generation-ready.
 """
        return self.pipeline is None

    def lowest_confidence(self) -> Confidence:
        """The weakest confidence across the two primary axes."""
        order = {Confidence.LOW: 0, Confidence.MEDIUM: 1, Confidence.HIGH: 2}
        axes = [self.evidence[a] for a in ("ci_vendor", "target_platform") if a in self.evidence]
        if not axes:
            return Confidence.LOW
        return min((e.confidence for e in axes), key=lambda c: order[c])


# ---------------------------------------------------------------------------
# Signal scanning helpers (filesystem only — deterministic, no network)
# ---------------------------------------------------------------------------
def _rel(path: Path, root: Path) -> str:
    """Repo-relative POSIX path for stable, platform-independent evidence."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _exists(root: Path, *parts: str) -> Path | None:
    p = root.joinpath(*parts)
    return p if p.exists() else None


def _glob_first(root: Path, pattern: str, limit: int = 5) -> list[Path]:
    """Deterministic (sorted) glob, capped, skipping common vendored dirs."""
    skip = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}
    out: list[Path] = []
    for p in sorted(root.glob(pattern)):
        if any(part in skip for part in p.parts):
            continue
        out.append(p)
        if len(out) >= limit:
            break
    return out


def _read_text_safe(path: Path, max_bytes: int = 65536) -> str:
    """Read a small text file for signal sniffing; empty string on any error."""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:max_bytes]
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# ci_vendor inference
# ---------------------------------------------------------------------------
# (relative signal path, how to detect, vendor) — order = priority.
def _infer_ci_vendor(root: Path) -> Evidence:
    #.github/workflows/ with at least one workflow file.
    workflows = root / ".github" / "workflows"
    if workflows.is_dir():
        wf = _glob_first(root, ".github/workflows/*.y*ml")
        signals = [_rel(w, root) for w in wf] or [_rel(workflows, root)]
        return Evidence(
            axis="ci_vendor",
            value="github",
            confidence=Confidence.HIGH,
            signals=signals,
            note="Found a .github/workflows/ directory with workflow file(s).",
        )

    file_map = [
        ("azure-pipelines.yml", "azure-devops", "Found azure-pipelines.yml."),
        ("azure-pipelines.yaml", "azure-devops", "Found azure-pipelines.yaml."),
        (".gitlab-ci.yml", "gitlab", "Found .gitlab-ci.yml."),
        ("bitbucket-pipelines.yml", "bitbucket", "Found bitbucket-pipelines.yml."),
        ("Jenkinsfile", "jenkins", "Found a Jenkinsfile."),
    ]
    for name, vendor, note in file_map:
        if _exists(root, name):
            return Evidence(
                axis="ci_vendor",
                value=vendor,
                confidence=Confidence.HIGH,
                signals=[name],
                note=note,
            )

    # No CI signal at all → propose a sensible default at LOW confidence.
    return Evidence(
        axis="ci_vendor",
        value="github",
        confidence=Confidence.LOW,
        signals=[],
        note="No CI configuration found. Defaulting to github — PLEASE CONFIRM the CI vendor.",
    )


# ---------------------------------------------------------------------------
# target_platform inference
# ---------------------------------------------------------------------------
def _has_k8s_manifest(root: Path) -> list[str]:
    hits: list[str] = []
    # Helm / kustomize marker files.
    for name in ("Chart.yaml", "kustomization.yaml", "kustomization.yml"):
        for p in _glob_first(root, f"**/{name}", limit=3):
            hits.append(_rel(p, root))
    # Any YAML declaring a k8s "kind: Deployment" (common strong signal).
    if not hits:
        for p in _glob_first(root, "**/*.y*ml", limit=40):
            text = _read_text_safe(p, max_bytes=8192)
            if "kind: Deployment" in text or "kind:Deployment" in text:
                hits.append(_rel(p, root))
                if len(hits) >= 3:
                    break
    return hits


def _looks_databricks(root: Path) -> list[str]:
    hits: list[str] = []
    for name in ("databricks.yml", "databricks.yaml"):
        if _exists(root, name):
            hits.append(name)
    for p in _glob_first(root, "**/*.ipynb", limit=3):
        hits.append(_rel(p, root))
    nb = root / "notebooks"
    if nb.is_dir():
        hits.append(_rel(nb, root))
    # pyproject / requirements referencing databricks.
    for name in ("pyproject.toml", "requirements.txt"):
        p = _exists(root, name)
        if p and "databricks" in _read_text_safe(p).lower():
            hits.append(name)
    # de-dup while preserving order
    seen: set[str] = set()
    uniq = [h for h in hits if not (h in seen or seen.add(h))]
    return uniq


def _looks_azure_functions(root: Path) -> list[str]:
    hits: list[str] = []
    if _exists(root, "host.json"):
        hits.append("host.json")
    for p in _glob_first(root, "**/function.json", limit=3):
        hits.append(_rel(p, root))
    return hits


def _dockerfile_ecs_strength(root: Path) -> tuple[Path | None, bool]:
    """Return (Dockerfile path or None, has_strong_ecs_hint)."""
    dockerfile = _exists(root, "Dockerfile")
    if dockerfile is None:
        # some repos nest it
        found = _glob_first(root, "**/Dockerfile", limit=1)
        dockerfile = found[0] if found else None
    if dockerfile is None:
        return None, False

    strong = False
    # task-definition / ecs hints in common deploy files or TF.
    for p in _glob_first(root, "**/*task-def*", limit=3) + _glob_first(
        root, "**/task-definition*", limit=3
    ):
        _ = p
        strong = True
    if not strong:
        for p in _glob_first(root, "**/*.tf", limit=20) + _glob_first(root, "**/*.yml", limit=20):
            text = _read_text_safe(p, max_bytes=8192).lower()
            if "aws_ecs" in text or "ecs_service" in text or "ecs service" in text:
                strong = True
                break
    return dockerfile, strong


def _infer_target_platform(root: Path) -> Evidence:
    # Databricks — a distinctive, no-image shape.
    db = _looks_databricks(root)
    if db:
        return Evidence(
            axis="target_platform",
            value="databricks",
            confidence=Confidence.HIGH if len(db) >= 2 else Confidence.MEDIUM,
            signals=db,
            note="Notebook/Databricks signals found (no container image expected).",
        )

    # Azure Functions.
    af = _looks_azure_functions(root)
    if af:
        return Evidence(
            axis="target_platform",
            value="azure-functions",
            confidence=Confidence.HIGH if "host.json" in af else Confidence.MEDIUM,
            signals=af,
            note="Azure Functions signals (host.json / function.json) found.",
        )

    # Kubernetes.
    k8s = _has_k8s_manifest(root)
    if k8s:
        return Evidence(
            axis="target_platform",
            value="kubernetes",
            confidence=Confidence.HIGH,
            signals=k8s,
            note="Kubernetes manifests / Helm / kustomize signals found.",
        )

    # Dockerfile → aws-ecs (container). Strong hint = medium/high, plain = low.
    dockerfile, strong = _dockerfile_ecs_strength(root)
    if dockerfile is not None:
        if strong:
            return Evidence(
                axis="target_platform",
                value="aws-ecs",
                confidence=Confidence.HIGH,
                signals=[_rel(dockerfile, root)],
                note="Dockerfile plus ECS/task-definition hints found.",
            )
        return Evidence(
            axis="target_platform",
            value="aws-ecs",
            confidence=Confidence.LOW,
            signals=[_rel(dockerfile, root)],
            note=(
                "A Dockerfile was found with no stronger deploy signal. Defaulting to "
                "aws-ecs (container) — PLEASE CONFIRM the target platform."
            ),
        )

    # Nothing → low-confidence default.
    return Evidence(
        axis="target_platform",
        value="aws-ecs",
        confidence=Confidence.LOW,
        signals=[],
        note=(
            "No deploy signals found. Defaulting to aws-ecs — PLEASE CONFIRM the target "
            "platform (could be databricks, azure-functions, kubernetes, …)."
        ),
    )


# ---------------------------------------------------------------------------
# cloud.provider inference
# ---------------------------------------------------------------------------
def _infer_cloud_provider(root: Path, target: Evidence) -> Evidence:
    # Target platform strongly implies a provider.
    target_default = {
        "aws-ecs": "aws",
        "databricks": "azure",
        "azure-functions": "azure",
        "kubernetes": None,
    }

    # Explicit terraform provider hints.
    aws_hits: list[str] = []
    azure_hits: list[str] = []
    gcp_hits: list[str] = []
    for p in _glob_first(root, "**/*.tf", limit=20):
        text = _read_text_safe(p, max_bytes=8192).lower()
        rel = _rel(p, root)
        if 'provider "aws"' in text or "aws_" in text:
            aws_hits.append(rel)
        if 'provider "azurerm"' in text or 'provider "azure"' in text or "azurerm_" in text:
            azure_hits.append(rel)
        if 'provider "google"' in text or "google_" in text:
            gcp_hits.append(rel)

    # aws-actions in github workflows → aws.
    for p in _glob_first(root, ".github/workflows/*.y*ml", limit=5):
        if "aws-actions" in _read_text_safe(p, max_bytes=8192):
            aws_hits.append(_rel(p, root))

    if aws_hits and not azure_hits and not gcp_hits:
        return Evidence("cloud.provider", "aws", Confidence.HIGH, aws_hits, "AWS IaC/action hints.")
    if azure_hits and not aws_hits and not gcp_hits:
        return Evidence(
            "cloud.provider", "azure", Confidence.HIGH, azure_hits, "Azure IaC/provider hints."
        )
    if gcp_hits and not aws_hits and not azure_hits:
        return Evidence(
            "cloud.provider", "gcp", Confidence.HIGH, gcp_hits, "GCP IaC/provider hints."
        )

    mixed = [h for group in (aws_hits, azure_hits, gcp_hits) for h in group]
    default = target_default.get(target.value or "", None)
    if default is not None:
        return Evidence(
            "cloud.provider",
            default,
            Confidence.MEDIUM if not mixed else Confidence.LOW,
            mixed,
            f"Inferred '{default}' from target_platform '{target.value}'"
            + (" (conflicting IaC hints present — PLEASE CONFIRM)." if mixed else "."),
        )

    return Evidence(
        "cloud.provider",
        None,
        Confidence.LOW,
        mixed,
        "Could not infer a cloud provider — PLEASE CONFIRM (aws | azure | gcp).",
    )


# ---------------------------------------------------------------------------
# project.name / project.language
# ---------------------------------------------------------------------------
_LANGUAGE_MARKERS = [
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("requirements.txt", "python"),
    ("package.json", "node"),
    ("go.mod", "go"),
    ("pom.xml", "java"),
    ("build.gradle", "java"),
    ("Cargo.toml", "rust"),
    ("Gemfile", "ruby"),
    ("composer.json", "php"),
]


def _infer_language(root: Path) -> Evidence:
    for marker, lang in _LANGUAGE_MARKERS:
        if _exists(root, marker):
            return Evidence(
                "project.language",
                lang,
                Confidence.HIGH,
                [marker],
                f"Language '{lang}' inferred from {marker}.",
            )
    # Fall back to notebooks → python.
    if _glob_first(root, "**/*.ipynb", limit=1):
        return Evidence(
            "project.language",
            "python",
            Confidence.MEDIUM,
            ["*.ipynb"],
            "Language 'python' inferred from notebooks.",
        )
    return Evidence(
        "project.language",
        None,
        Confidence.LOW,
        [],
        "Could not infer the project language — PLEASE CONFIRM.",
    )


# ---------------------------------------------------------------------------
# stages / environments defaults (refined by signals)
# ---------------------------------------------------------------------------
def _default_stages(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """A sensible default stage set, refined by signals. Returns (stages, notes)."""
    notes: list[str] = []
    stages: list[dict[str, Any]] = [
        {"id": "test", "runs": "make test"},
    ]

    # Security stage if a scanner config is present.
    security_markers = [
        ".github/workflows/codeql.yml",
        ".snyk",
        "bandit.yaml",
        ".bandit",
        ".semgrep.yml",
        "trivy.yaml",
    ]
    if any(_exists(root, m) for m in security_markers):
        stages.append({"id": "security", "scans": ["sast", "secrets", "dependency"]})
        notes.append("Added a 'security' stage because a scanner config was detected.")

    stages.append({"id": "build", "runs": "make build"})
    stages.append({"id": "deploy-dev", "environment": "dev", "runs": "make deploy ENV=dev"})
    stages.append(
        {
            "id": "deploy-prod",
            "environment": "prod",
            "when": "manual",
            "runs": "make deploy ENV=prod",
        }
    )
    return stages, notes


def _default_environments() -> list[dict[str, Any]]:
    return [
        {"name": "dev", "gate": "auto", "protection": "none"},
        {"name": "prod", "gate": "manual", "protection": "required-approvals"},
    ]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def discover_stack(repo_root: Path | str) -> StackProposal:
    """Inspect ``repo_root`` and propose a draft IR for developer confirmation.

 Deterministic and filesystem-only (no network). Performs NO writes — it only
 reads the repo and returns a :class:`StackProposal`.

 Args:
 repo_root: path to the client repository to inspect.

 Returns:
 A :class:`StackProposal` carrying the draft IR (dict), a validated
:class:`Pipeline` when complete, per-axis evidence, and the list of
 assumptions/ambiguities the developer must confirm.

 Raises:
 NotADirectoryError: if ``repo_root`` is not an existing directory.
 """
    root = Path(repo_root).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"repo_root is not a directory: {root}")

    ci_ev = _infer_ci_vendor(root)
    target_ev = _infer_target_platform(root)
    provider_ev = _infer_cloud_provider(root, target_ev)
    language_ev = _infer_language(root)

    evidence: dict[str, Evidence] = {
        "ci_vendor": ci_ev,
        "target_platform": target_ev,
        "cloud.provider": provider_ev,
        "project.language": language_ev,
    }

    stages, stage_notes = _default_stages(root)
    environments = _default_environments()

    ir: dict[str, Any] = {
        "version": 1,
        "ci_vendor": ci_ev.value,
        "target_platform": target_ev.value,
        "project": {"name": root.name},
        "environments": environments,
        "stages": stages,
    }
    if language_ev.value is not None:
        ir["project"]["language"] = language_ev.value
    if provider_ev.value is not None:
        ir["cloud"] = {"provider": provider_ev.value, "auth": "oidc"}

    # Collect assumptions the developer must confirm (anything LOW, plus notes).
    assumptions: list[str] = []
    for axis in ("ci_vendor", "target_platform", "cloud.provider", "project.language"):
        ev = evidence[axis]
        if ev.confidence is Confidence.LOW:
            assumptions.append(f"[{axis}] {ev.note}")
    assumptions.extend(stage_notes)
    assumptions.append(
        "Stages and environments are a default scaffold (test → build → deploy-dev → "
        "deploy-prod) — review and adjust to the project's real workflow."
    )

    # Try to validate as a complete Pipeline (plugin-existence check is a later
    # gate — discovery only checks IR *shape*).
    pipeline: Pipeline | None
    try:
        pipeline = Pipeline.model_validate(ir)
    except Exception:  # noqa: BLE001 — any validation failure just means "draft only"
        pipeline = None
        assumptions.append(
            "The draft IR could not be fully validated — it is marked DRAFT; complete the "
            "missing/required fields before generating."
        )

    return StackProposal(
        repo_root=root,
        ir=ir,
        pipeline=pipeline,
        evidence=evidence,
        assumptions=assumptions,
    )
