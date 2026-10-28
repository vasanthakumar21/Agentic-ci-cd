"""The "automated vs manual" bootstrap report.

Emits a summary that separates what the bootstrapper *automates for you* from the
*irreducible manual* prerequisites a human must still perform — and points each
manual step at the runbook, so the limitations are **documented, never hidden**.

Two columns:

* **AUTOMATED** — the plan's :attr:`BootstrapPlan.automatable_actions`: the repo,
 secrets, OIDC trust, runner registration, and branch protection a ``--apply`` run
 performs behind the :class:`~cicdgen.bootstrap.apply.Executor` seam.
* **MANUAL** — the steps the bootstrapper *cannot* do for you, derived from the
 loaded config:
 - **always**: exporting the vendor admin token env var — providing the first
 admin credential is inherently a human action;
 - when ``oidc.aws`` is present but has no ``provider_arn``: creating/confirming
 the account/org-level AWS IAM OIDC identity provider (often account-managed);
 - when ``oidc.azure`` is present: registering/confirming the Azure app
 registration + federated-credential subject at tenant level (often org-managed);
 - when runners are **self-hosted**: obtaining the runner registration token into
 its env var (human-provided);
 - when a Databricks target is configured: a note that workspace/metastore
 creation is account-level (the Databricks IaC README flags this).

Hard rule: this report is **value-free**. It references secret / token ENV-VAR
NAMES only — never a resolved value. A missing required key fails fast in the
loader, naming the KEY;:func:`emit_automation_report` lets that
:class:`~cicdgen.localconfig.LocalConfigError` propagate.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cicdgen.bootstrap.planner import BootstrapAction, BootstrapPlan, plan_from_config
from cicdgen.localconfig import LocalConfig, RunnerMode, load_local_config

# The documents a reader is pointed at for the manual steps.
RUNBOOK = "MIGRATION_RUNBOOK.md"
IAC_READMES = "the per-target IaC READMEs"


@dataclass(frozen=True)
class ManualStep:
    """One irreducible manual prerequisite — value-free.

 Attributes:
 concern: the area of work this prerequisite belongs to (e.g. ``"secrets"``,
 ``"oidc"``, ``"runners"``, ``"target"``) — a plain label, not tied to
 the planner's :class:`~cicdgen.bootstrap.planner.Concern` so new manual
 categories (like a target note) can be expressed freely.
 description: a VALUE-FREE sentence of what the human must do. May reference
 an env-var NAME, never a value.
 why: a short reason this step cannot be automated.
 pointer: where the operator reads more (the runbook / IaC READMEs).
 """

    concern: str
    description: str
    why: str
    pointer: str = RUNBOOK


@dataclass(frozen=True)
class AutomationReport:
    """Structured "automated vs manual" split of a bootstrap run (value-free).

 ``automated`` holds the plan's automatable actions (what bootstrap does for you);
 ``manual`` holds the irreducible human prerequisites. Counts are derived so a
 one-line summary ("Automated N of M steps; K manual (see runbook)") is trivial.
 """

    repo: str
    automated: tuple[BootstrapAction, ...]
    manual: tuple[ManualStep, ...]
    runbook: str = RUNBOOK

    @property
    def automated_count(self) -> int:
        return len(self.automated)

    @property
    def manual_count(self) -> int:
        return len(self.manual)

    @property
    def total_count(self) -> int:
        """Every step a full bootstrap touches — automated plus manual."""
        return self.automated_count + self.manual_count

    @property
    def summary(self) -> str:
        """One-line honest summary: automated N of M, K manual."""
        return (
            f"Automated {self.automated_count} of {self.total_count} steps; "
            f"{self.manual_count} manual (see {self.runbook})."
        )


# ---------------------------------------------------------------------------
# deriving the irreducible manual prerequisites from the loaded config
# ---------------------------------------------------------------------------
def _manual_steps(config: LocalConfig) -> list[ManualStep]:
    """Derive the value-free manual prerequisites from the validated config.

 Purely data-driven: each step is emitted only when the config makes it
 applicable, and target platforms are never hard-coded beyond what the config
 reveals. References env-var NAMES only.
 """
    bootstrap = config.bootstrap
    steps: list[ManualStep] = []

    # ALWAYS: the first admin token is a human-provided credential. Reference the
    # env-var NAME only (never resolve or echo its value).
    admin_env = bootstrap.vendor_admin.token_env
    steps.append(
        ManualStep(
            concern="secrets",
            description=(
                f"Export the vendor admin token into env var '{admin_env}' before "
                f"running bootstrap."
            ),
            why=(
                "Providing the first admin credential is inherently a human action — "
                "the bootstrapper needs it to authenticate but cannot mint it for you."
            ),
        )
    )

    # OIDC — account/org-level identity-provider creation can be account-managed.
    oidc = bootstrap.oidc
    if oidc is not None and oidc.aws is not None and not oidc.aws.provider_arn:
        steps.append(
            ManualStep(
                concern="oidc",
                description=(
                    "Create or confirm the account/org-level AWS IAM OIDC identity "
                    "provider for GitHub Actions, then record its ARN as "
                    "'oidc.aws.provider_arn'."
                ),
                why=(
                    "Org-level IdP creation is often account-managed (one provider "
                    "shared across repos), so it sits outside a per-repo bootstrap."
                ),
                pointer=IAC_READMES,
            )
        )
    if oidc is not None and oidc.azure is not None:
        steps.append(
            ManualStep(
                concern="oidc",
                description=(
                    "Register or confirm the Azure app registration and its "
                    "federated-credential subject at tenant level for this repo."
                ),
                why=(
                    "Tenant-level app registration is usually org-managed and may "
                    "require directory privileges a per-repo bootstrap does not hold."
                ),
                pointer=IAC_READMES,
            )
        )

    # Self-hosted runners — the registration token is human-provided (env-var NAME).
    runners = bootstrap.runners
    if runners.mode is RunnerMode.SELF_HOSTED and runners.registration_token_env:
        steps.append(
            ManualStep(
                concern="runners",
                description=(
                    "Obtain a self-hosted runner registration token and export it "
                    f"into env var '{runners.registration_token_env}'."
                ),
                why=(
                    "Runner registration tokens are short-lived and human-minted from "
                    "the vendor UI/API; bootstrap consumes one but cannot issue it."
                ),
            )
        )

    # Databricks target — workspace/metastore creation is account-level (README flags
    # this). Only emitted when a databricks target is actually configured.
    target = bootstrap.target
    if target is not None and target.databricks is not None:
        steps.append(
            ManualStep(
                concern="target",
                description=(
                    "Create or confirm the Databricks workspace and Unity Catalog "
                    "metastore before bootstrap wires the deploy job."
                ),
                why=(
                    "Workspace and metastore provisioning is an account-level action "
                    "(flagged in the Databricks IaC README); it precedes per-repo wiring."
                ),
                pointer=IAC_READMES,
            )
        )

    return steps


# ---------------------------------------------------------------------------
# building the report
# ---------------------------------------------------------------------------
def emit_automation_report_from_plan(
    plan: BootstrapPlan, config: LocalConfig
) -> AutomationReport:
    """Build an :class:`AutomationReport` from an already-built plan + config.

 Pure and value-free: the automated column is the plan's automatable actions;
 the manual column is derived from the config's irreducible prerequisites.
 """
    return AutomationReport(
        repo=plan.repo,
        automated=tuple(plan.automatable_actions),
        manual=tuple(_manual_steps(config)),
    )


def _build_report(config: LocalConfig) -> AutomationReport:
    plan = plan_from_config(config)
    return emit_automation_report_from_plan(plan, config)


# ---------------------------------------------------------------------------
# rendering — the value-free "automated vs manual" report
# ---------------------------------------------------------------------------
def render_automation_report(report: AutomationReport) -> str:
    """Format an :class:`AutomationReport` as a value-free, loggable report.

 Value-free by construction: it renders only action descriptions and manual-step
 text, which are themselves value-free. Safe to print or log.
 """
    lines: list[str] = []
    header = f"Bootstrap automation report for {report.repo}"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append("")
    lines.append(report.summary)
    lines.append("")

    lines.append(f"AUTOMATED ({report.automated_count}) — bootstrap does this for you:")
    if report.automated:
        for action in report.automated:
            lines.append(f"  - ({action.concern}) {action.description}")
    else:
        lines.append("  (nothing to automate)")
    lines.append("")

    lines.append(f"MANUAL ({report.manual_count}) — irreducible, you must do this:")
    if report.manual:
        for step in report.manual:
            lines.append(f"  - ({step.concern}) {step.description}")
            lines.append(f"      why: {step.why}")
            lines.append(f"      see: {step.pointer}")
    else:
        lines.append("  (no manual steps required)")
    lines.append("")

    lines.append(
        f"Manual steps are documented, never hidden — see {report.runbook}."
    )
    return "\n".join(lines)


def emit_automation_report(
    cicd_dir: Path | str,
    *,
    environ: dict[str, str] | None = None,
) -> str:
    """Load the local config and emit the "automated vs manual" report.

 This is the public entry point. It loads + validates the config (fail-fast,
 naming a missing KEY), builds the plan, derives the irreducible manual
 prerequisites, and returns a rendered, VALUE-FREE report string.

 Args:
 cicd_dir: the ``.cicd`` directory with ``bootstrap.local.yaml`` /
 ``secrets.local.yaml``.
 environ: inject an environment mapping for ``*_env`` token resolution;
 defaults to the real process environment.

 Returns:
 A rendered, value-free report separating AUTOMATED from MANUAL work, with a
 one-line summary and runbook pointers.

 Raises:
 cicdgen.localconfig.LocalConfigError: if a required key is missing/empty or a
 required token env var is unset — message names the KEY only.
 """
    config = load_local_config(cicd_dir, resolve_env_tokens=True, environ=environ)
    report = _build_report(config)
    return render_automation_report(report)


def build_automation_report(
    cicd_dir: Path | str,
    *,
    environ: dict[str, str] | None = None,
) -> AutomationReport:
    """Load the config and return the structured :class:`AutomationReport`.

 Same loading + validation contract as :func:`emit_automation_report`, but returns
 the structured model instead of the rendered string (for programmatic callers).
 """
    config = load_local_config(cicd_dir, resolve_env_tokens=True, environ=environ)
    return _build_report(config)
