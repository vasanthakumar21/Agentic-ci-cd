"""Dry-run-by-default bootstrap planner — the PLANNING half.

Reads the local, git-ignored config via :mod:`cicdgen.localconfig` and produces an
ordered **plan** of :class:`BootstrapAction` objects describing what a bootstrap run
*would* do — WITHOUT executing anything. Execution (``--apply``) is separate; this
module never touches a vendor/cloud API, never shells out, and never writes outside
its own in-memory plan.

Planning flow:

 1. load ``.cicd/*.local.yaml`` and validate all required keys (fail-fast,
 naming the missing KEY; the loader raises
:class:`~cicdgen.localconfig.LocalConfigError`);
 2. dry-run by default: describe the exact actions (push N secrets, create the
 OIDC role, set branch protection, register runners, create the repo) grouped
 by concern, in a stable order;
 3. (elsewhere) on ``--apply`` perform them idempotently.

Hard rule: the planner describes secrets by NAME only. A secret VALUE (or a
resolved ``*_env`` token value) is NEVER read, logged, repr'd, or placed into an
action description or the rendered report. The planner reads only the catalog of
names and the non-secret wiring.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from cicdgen.localconfig import BootstrapConfig, LocalConfig, load_local_config


class Concern(StrEnum):
    """The grouped areas of work a bootstrap run covers."""

    REPO = "repo"
    SECRETS = "secrets"
    OIDC = "oidc"
    RUNNERS = "runners"
    BRANCH_PROTECTION = "branch_protection"


# Stable ordering of concerns in the plan: repo must exist before anything is pushed
# into it; secrets/OIDC/runners configure it; branch protection is applied last.
_CONCERN_ORDER: dict[Concern, int] = {
    Concern.REPO: 0,
    Concern.SECRETS: 1,
    Concern.OIDC: 2,
    Concern.RUNNERS: 3,
    Concern.BRANCH_PROTECTION: 4,
}


@dataclass(frozen=True)
class BootstrapAction:
    """One planned unit of bootstrap work — a value-free description.

 Attributes:
 concern: which grouped area this action belongs to.
 description: a human-readable, VALUE-FREE sentence of what WOULD happen.
 It may reference secret/env-var NAMES and non-secret wiring, but never
 a secret value.
 automatable: True if a ``--apply`` run could perform this via API/IaC;
 False if it is an irreducible manual step the operator must do and that
 belongs in the "manual" column of the report.
 executed: always ``False`` in a dry-run plan. The planner never executes;
 this placeholder exists so the same shape can carry apply-time state.
 """

    concern: Concern
    description: str
    automatable: bool = True
    executed: bool = False


@dataclass(frozen=True)
class BootstrapPlan:
    """The ordered result of planning a bootstrap run (dry-run by default).

 ``actions`` is ordered by concern (see :data:`_CONCERN_ORDER`) then by the order
 discovered within a concern. ``repo`` is a value-free ``owner/name`` label for
 reports. Nothing here holds a secret value.
 """

    repo: str
    actions: tuple[BootstrapAction, ...]
    dry_run: bool = True

    def by_concern(self, concern: Concern) -> list[BootstrapAction]:
        """Return the actions for a single concern, preserving plan order."""
        return [a for a in self.actions if a.concern is concern]

    @property
    def automatable_actions(self) -> list[BootstrapAction]:
        return [a for a in self.actions if a.automatable]

    @property
    def manual_actions(self) -> list[BootstrapAction]:
        return [a for a in self.actions if not a.automatable]


# ---------------------------------------------------------------------------
# per-concern planners — each takes the validated BootstrapConfig and returns a
# list of value-free actions. None of these read a secret value.
# ---------------------------------------------------------------------------
def _plan_repo(bootstrap: BootstrapConfig) -> list[BootstrapAction]:
    repo = bootstrap.repo
    host = repo.host or "the configured vendor host"
    if repo.create_if_missing:
        return [
            BootstrapAction(
                concern=Concern.REPO,
                description=(
                    f"Create repository '{repo.owner}/{repo.name}' on {host} "
                    f"(default branch '{repo.default_branch}') if it does not already exist."
                ),
                automatable=True,
            )
        ]
    return [
        BootstrapAction(
            concern=Concern.REPO,
            description=(
                f"Assume repository '{repo.owner}/{repo.name}' already exists on {host} "
                f"(create_if_missing is false — no repo will be created)."
            ),
            automatable=True,
        )
    ]


def _plan_secrets(bootstrap: BootstrapConfig) -> list[BootstrapAction]:
    # One action per catalogued secret NAME. The value is never read.
    actions: list[BootstrapAction] = []
    for name in bootstrap.secrets:
        actions.append(
            BootstrapAction(
                concern=Concern.SECRETS,
                description=(
                    f"Push secret '{name}' to the vendor secret store "
                    f"(value supplied locally by name; never displayed)."
                ),
                automatable=True,
            )
        )
    return actions


def _plan_oidc(bootstrap: BootstrapConfig) -> list[BootstrapAction]:
    actions: list[BootstrapAction] = []
    oidc = bootstrap.oidc
    if oidc is None or (oidc.aws is None and oidc.azure is None):
        actions.append(
            BootstrapAction(
                concern=Concern.OIDC,
                description=(
                    "No OIDC wiring configured — skipping cloud identity-provider setup "
                    "(key-based or no-cloud target)."
                ),
                automatable=True,
            )
        )
        return actions

    if oidc.aws is not None:
        aws = oidc.aws
        # account_id / role_name / region are non-secret wiring (not tokens).
        provider = (
            f"reuse existing provider '{aws.provider_arn}'"
            if aws.provider_arn
            else "create the GitHub-OIDC identity provider"
        )
        actions.append(
            BootstrapAction(
                concern=Concern.OIDC,
                description=(
                    f"Configure AWS OIDC trust in account '{aws.account_id}' "
                    f"(region '{aws.region}'): {provider} and create/update deploy role "
                    f"'{aws.role_name}' with a trust policy scoped to this repo."
                ),
                automatable=True,
            )
        )
    if oidc.azure is not None:
        az = oidc.azure
        rg = f" resource group '{az.resource_group}'" if az.resource_group else ""
        actions.append(
            BootstrapAction(
                concern=Concern.OIDC,
                description=(
                    f"Configure Azure workload-identity federation on client "
                    f"'{az.client_id}' (tenant '{az.tenant_id}', subscription "
                    f"'{az.subscription_id}'{rg}): add a federated credential scoped "
                    f"to this repo."
                ),
                automatable=True,
            )
        )
    return actions


def _plan_runners(bootstrap: BootstrapConfig) -> list[BootstrapAction]:
    runners = bootstrap.runners
    if runners.mode.value == "self-hosted":
        labels = f" with labels {runners.labels}" if runners.labels else ""
        # Describe the registration token by its ENV-VAR NAME only.
        return [
            BootstrapAction(
                concern=Concern.RUNNERS,
                description=(
                    f"Register a self-hosted runner{labels} using the registration "
                    f"token from env var '{runners.registration_token_env}' "
                    f"(token value read from the environment; never displayed)."
                ),
                automatable=True,
            )
        ]
    return [
        BootstrapAction(
            concern=Concern.RUNNERS,
            description=(
                "Runners mode is 'shared' — no runner registration needed "
                "(vendor-hosted runners will be used)."
            ),
            automatable=True,
        )
    ]


def _plan_branch_protection(bootstrap: BootstrapConfig) -> list[BootstrapAction]:
    actions: list[BootstrapAction] = []
    # Deterministic order: by branch name.
    for branch in sorted(bootstrap.branch_protection):
        rule = bootstrap.branch_protection[branch]
        checks = ", ".join(rule.required_checks) if rule.required_checks else "none"
        actions.append(
            BootstrapAction(
                concern=Concern.BRANCH_PROTECTION,
                description=(
                    f"Set branch protection on '{branch}': require "
                    f"{rule.required_approvals} approval(s), required checks: [{checks}], "
                    f"enforce_admins={rule.enforce_admins}, "
                    f"dismiss_stale_reviews={rule.dismiss_stale_reviews}."
                ),
                automatable=True,
            )
        )
    return actions


# Ordered list of per-concern planners (defines within-plan discovery order).
_PLANNERS = (
    _plan_repo,
    _plan_secrets,
    _plan_oidc,
    _plan_runners,
    _plan_branch_protection,
)


def plan_from_config(config: LocalConfig) -> BootstrapPlan:
    """Build a :class:`BootstrapPlan` from an already-loaded :class:`LocalConfig`.

 Pure and value-free: reads only the catalog of names and non-secret wiring.
 Never reveals or logs a secret / token value.
 """
    bootstrap = config.bootstrap
    actions: list[BootstrapAction] = []
    for planner in _PLANNERS:
        actions.extend(planner(bootstrap))

    # Stable sort by concern order; within a concern, discovery order is preserved
    # because Python's sort is stable.
    actions.sort(key=lambda a: _CONCERN_ORDER[a.concern])

    repo = f"{bootstrap.repo.owner}/{bootstrap.repo.name}"
    return BootstrapPlan(repo=repo, actions=tuple(actions), dry_run=True)


def plan_bootstrap(
    cicd_dir: Path | str,
    *,
    environ: dict[str, str] | None = None,
) -> BootstrapPlan:
    """Load the local config and produce a dry-run :class:`BootstrapPlan`.

 This is the public entry point. It is **dry-run by default** and performs NO
 live action: it only loads + validates the config and describes
 what an ``--apply`` run would do.

 Args:
 cicd_dir: the ``.cicd`` directory holding ``bootstrap.local.yaml`` and
 ``secrets.local.yaml``.
 environ: inject an environment mapping (for the ``*_env`` token resolution)
 — defaults to the real process environment.

 Returns:
 A :class:`BootstrapPlan` whose ``actions`` describe what WOULD happen.

 Raises:
 cicdgen.localconfig.LocalConfigError: if a required key is missing / empty
 or a required token env var is unset — the message names the KEY only,
 never a value.
 """
    config = load_local_config(cicd_dir, resolve_env_tokens=True, environ=environ)
    return plan_from_config(config)


# ---------------------------------------------------------------------------
# rendering — the "what would happen" dry-run report (value-free)
# ---------------------------------------------------------------------------
_CONCERN_TITLES: dict[Concern, str] = {
    Concern.REPO: "Repository",
    Concern.SECRETS: "Secrets",
    Concern.OIDC: "Cloud OIDC trust",
    Concern.RUNNERS: "Runners",
    Concern.BRANCH_PROTECTION: "Branch protection",
}


def render_plan(plan: BootstrapPlan) -> str:
    """Format a :class:`BootstrapPlan` as a human-readable dry-run report.

 The output is value-free by construction (it only renders action descriptions,
 which are themselves value-free) and is safe to print or log.
 """
    lines: list[str] = []
    mode = "DRY RUN — no changes will be made" if plan.dry_run else "APPLY"
    lines.append(f"Bootstrap plan for {plan.repo}  [{mode}]")
    lines.append("=" * len(lines[0]))
    lines.append("")

    total = len(plan.actions)
    automatable = len(plan.automatable_actions)
    manual = len(plan.manual_actions)
    lines.append(
        f"{total} planned action(s): {automatable} automatable, {manual} manual."
    )
    lines.append("")

    # Group in the stable concern order.
    for concern in sorted(_CONCERN_ORDER, key=lambda c: _CONCERN_ORDER[c]):
        concern_actions = plan.by_concern(concern)
        if not concern_actions:
            continue
        lines.append(f"[{_CONCERN_TITLES[concern]}]")
        for action in concern_actions:
            tag = "auto" if action.automatable else "MANUAL"
            lines.append(f"  - ({tag}) {action.description}")
        lines.append("")

    lines.append(
        "Run with --apply to perform these actions (idempotent). "
        "Dry run performs no changes."
    )
    return "\n".join(lines)


def print_plan(plan: BootstrapPlan) -> None:
    """Print the rendered dry-run report to stdout (value-free)."""
    print(render_plan(plan))
