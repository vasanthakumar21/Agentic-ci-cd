"""Idempotent ``--apply`` execution half of the bootstrapper.

The planner (:mod:`cicdgen.bootstrap.planner`) produces a value-free, dry-run
**plan** of what a bootstrap run *would* do. This module performs it — but only
when the caller explicitly opts in (``confirm=True``), and only through an
injectable :class:`Executor` interface so the real vendor/cloud API calls stay
behind a seam that tests can fake WITHOUT touching a real account.

Three hard contracts:

* **Dry-run by default.** ``apply_bootstrap(..., confirm=False)`` (the default)
 performs ZERO mutating calls — it only re-plans. Live / destructive actions are
 gated behind the explicit ``confirm=True`` flag (the ``--apply`` + confirm gate).
* **Idempotency.** For each planned action the executor FIRST detects whether the
 resource already exists. Absent → create. Present-and-equal → no-op
 (``UNCHANGED``). Present-but-different → update. Re-running against unchanged
 state therefore yields all ``UNCHANGED`` outcomes and creates no duplicates.
* **Value-free reporting.** A secret VALUE is read via
:meth:`LocalConfig.get_secret` only at the moment it is handed to
:meth:`Executor.push_secret`, and is NEVER logged, repr'd, or placed into an
:class:`ApplyResult` / rendered report. The executor receives the value; this
 layer never echoes it.

Fail-fast on a missing required local-config key happens in the loader
(:func:`cicdgen.localconfig.load_local_config`), which names the KEY only;
``apply_bootstrap`` lets that :class:`LocalConfigError` propagate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from cicdgen.bootstrap.planner import (
    BootstrapPlan,
    Concern,
    plan_bootstrap,
)
from cicdgen.localconfig import LocalConfig, RunnerMode, load_local_config


class Outcome(StrEnum):
    """Per-action result of an apply run.

 * ``CREATED`` — the resource was absent and was created.
 * ``UPDATED`` — the resource existed but differed and was brought into line.
 * ``UNCHANGED`` — the resource existed and already matched (idempotent no-op).
 * ``SKIPPED`` — nothing to do for this action (e.g. shared runners, no OIDC),
 or a dry-run (``confirm=False``) where no mutation was performed.
 """

    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class BranchProtectionRule:
    """Value-free, comparable snapshot of a branch-protection rule.

 The executor returns the CURRENT rule for a branch (or ``None`` if the branch
 has no protection) so the apply layer can decide create vs update vs unchanged
 purely by equality — no vendor-specific diffing leaks into this module.
 """

    required_approvals: int
    required_checks: tuple[str, ...]
    enforce_admins: bool
    dismiss_stale_reviews: bool


@runtime_checkable
class Executor(Protocol):
    """The vendor/cloud seam every concern's live operations go through.

 A real implementation wraps the GitHub/Azure DevOps/AWS/Azure APIs; tests
 inject a fake that simulates existing/absent resources and records calls, so
 the idempotency logic is verifiable without a live account.

 Every method is a plain detect-or-mutate operation. Detection methods are pure
 reads; mutation methods are only ever called by :func:`apply_bootstrap` after a
 detection says they are needed AND the caller passed ``confirm=True``.

 Hard rule::meth:`push_secret` receives the raw secret VALUE — it is the ONLY
 method that does — and must not log it. No other method ever sees a value.
 """

    # --- repo ---------------------------------------------------------------
    def repo_exists(self) -> bool:
        """True if the target repository already exists."""
        ...

    def create_repo(self) -> None:
        """Create the target repository (called only when absent + create_if_missing)."""
        ...

    # --- secrets ------------------------------------------------------------
    def secret_exists(self, name: str) -> bool:
        """True if a secret with ``name`` is already present in the vendor store."""
        ...

    def push_secret(self, name: str, value: str) -> None:
        """Create/overwrite secret ``name`` with ``value``.

 ``value`` is the raw secret — the implementation MUST NOT log or echo it.
 """
        ...

    # --- oidc ---------------------------------------------------------------
    def oidc_trust_exists(self, provider: str, role_name: str) -> bool:
        """True if the OIDC trust / deploy identity for ``role_name`` already exists."""
        ...

    def create_oidc_trust(self, provider: str, role_name: str) -> None:
        """Create the cloud OIDC provider + deploy role trust scoped to this repo."""
        ...

    # --- runners ------------------------------------------------------------
    def runner_registered(self, labels: tuple[str, ...]) -> bool:
        """True if a self-hosted runner matching ``labels`` is already registered."""
        ...

    def register_runner(self, labels: tuple[str, ...], token: str) -> None:
        """Register a self-hosted runner using the registration ``token``.

 ``token`` is sensitive — the implementation MUST NOT log or echo it.
 """
        ...

    # --- branch protection --------------------------------------------------
    def branch_protection_current(self, branch: str) -> BranchProtectionRule | None:
        """Return the current protection rule for ``branch``, or ``None`` if unset."""
        ...

    def set_branch_protection(self, branch: str, rule: BranchProtectionRule) -> None:
        """Create/replace the protection rule for ``branch``."""
        ...


@dataclass(frozen=True)
class ActionResult:
    """Value-free record of what happened to ONE planned action.

 ``detail`` references NAMES / non-secret wiring only (same rule as the plan's
 action descriptions) and is safe to log.
 """

    concern: Concern
    target: str
    outcome: Outcome
    detail: str = ""


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of an :func:`apply_bootstrap` run — value-free and loggable.

 ``dry_run`` is True when the run was a plan-only pass (``confirm=False``): in
 that case every :class:`ActionResult` is ``SKIPPED`` and NO mutating executor
 method was called.
 """

    repo: str
    dry_run: bool
    results: tuple[ActionResult, ...] = field(default_factory=tuple)

    def by_outcome(self, outcome: Outcome) -> list[ActionResult]:
        return [r for r in self.results if r.outcome is outcome]

    @property
    def created(self) -> list[ActionResult]:
        return self.by_outcome(Outcome.CREATED)

    @property
    def updated(self) -> list[ActionResult]:
        return self.by_outcome(Outcome.UPDATED)

    @property
    def unchanged(self) -> list[ActionResult]:
        return self.by_outcome(Outcome.UNCHANGED)

    @property
    def skipped(self) -> list[ActionResult]:
        return self.by_outcome(Outcome.SKIPPED)

    @property
    def changed(self) -> bool:
        """True if any resource was created or updated (nothing changed ⇒ idempotent)."""
        return bool(self.created or self.updated)


# ---------------------------------------------------------------------------
# per-concern executors — each detects first, then mutates only if needed and
# only when ``confirm`` is True. None of these log a secret / token value.
# ---------------------------------------------------------------------------
def _apply_repo(
    config: LocalConfig, executor: Executor, *, confirm: bool
) -> list[ActionResult]:
    repo = config.bootstrap.repo
    target = f"{repo.owner}/{repo.name}"

    if not repo.create_if_missing:
        return [
            ActionResult(
                concern=Concern.REPO,
                target=target,
                outcome=Outcome.SKIPPED,
                detail="create_if_missing is false — repo assumed to exist.",
            )
        ]

    if executor.repo_exists():
        return [
            ActionResult(
                concern=Concern.REPO,
                target=target,
                outcome=Outcome.UNCHANGED,
                detail="repository already exists.",
            )
        ]

    if confirm:
        executor.create_repo()
    return [
        ActionResult(
            concern=Concern.REPO,
            target=target,
            outcome=Outcome.CREATED,
            detail="repository created.",
        )
    ]


def _apply_secrets(
    config: LocalConfig, executor: Executor, *, confirm: bool
) -> list[ActionResult]:
    results: list[ActionResult] = []
    for name in config.bootstrap.secrets:
        if executor.secret_exists(name):
            # Idempotent: a present secret is left as-is (overwriting a live secret
            # every run would be churny + risk clobbering a rotated value). It is a
            # no-op, recorded UNCHANGED. No value is read in this branch.
            results.append(
                ActionResult(
                    concern=Concern.SECRETS,
                    target=name,
                    outcome=Outcome.UNCHANGED,
                    detail=f"secret '{name}' already present.",
                )
            )
            continue

        if confirm:
            # Read the raw value ONLY here, at the moment of push, and hand it
            # straight to the executor. It never enters a log, repr, or result.
            value = config.get_secret(name)
            executor.push_secret(name, value)
        results.append(
            ActionResult(
                concern=Concern.SECRETS,
                target=name,
                outcome=Outcome.CREATED,
                detail=f"secret '{name}' pushed (value by name; never displayed).",
            )
        )
    return results


def _oidc_provider_label(aws_provider_arn: str | None) -> str:
    return aws_provider_arn or "github-oidc"


def _apply_oidc(
    config: LocalConfig, executor: Executor, *, confirm: bool
) -> list[ActionResult]:
    oidc = config.bootstrap.oidc
    results: list[ActionResult] = []

    if oidc is None or (oidc.aws is None and oidc.azure is None):
        return [
            ActionResult(
                concern=Concern.OIDC,
                target="(none)",
                outcome=Outcome.SKIPPED,
                detail="no OIDC wiring configured.",
            )
        ]

    specs: list[tuple[str, str]] = []
    if oidc.aws is not None:
        specs.append((_oidc_provider_label(oidc.aws.provider_arn), oidc.aws.role_name))
    if oidc.azure is not None:
        specs.append(("azure-wif", oidc.azure.client_id))

    for provider, role_name in specs:
        if executor.oidc_trust_exists(provider, role_name):
            results.append(
                ActionResult(
                    concern=Concern.OIDC,
                    target=role_name,
                    outcome=Outcome.UNCHANGED,
                    detail=f"OIDC trust for '{role_name}' already exists.",
                )
            )
            continue
        if confirm:
            executor.create_oidc_trust(provider, role_name)
        results.append(
            ActionResult(
                concern=Concern.OIDC,
                target=role_name,
                outcome=Outcome.CREATED,
                detail=f"OIDC trust for '{role_name}' created.",
            )
        )
    return results


def _apply_runners(
    config: LocalConfig, executor: Executor, *, confirm: bool
) -> list[ActionResult]:
    runners = config.bootstrap.runners
    if runners.mode is not RunnerMode.SELF_HOSTED:
        return [
            ActionResult(
                concern=Concern.RUNNERS,
                target="shared",
                outcome=Outcome.SKIPPED,
                detail="runners mode is 'shared' — nothing to register.",
            )
        ]

    labels = tuple(runners.labels or ())
    label_target = ",".join(labels) if labels else "(default)"

    if executor.runner_registered(labels):
        return [
            ActionResult(
                concern=Concern.RUNNERS,
                target=label_target,
                outcome=Outcome.UNCHANGED,
                detail="a matching self-hosted runner is already registered.",
            )
        ]

    if confirm:
        # The registration token is resolved by its ENV-VAR NAME and handed to the
        # executor; its value is never logged.
        token = config.get_env_token(runners.registration_token_env)  # type: ignore[arg-type]
        executor.register_runner(labels, token)
    return [
        ActionResult(
            concern=Concern.RUNNERS,
            target=label_target,
            outcome=Outcome.CREATED,
            detail="self-hosted runner registered (token by env-var name).",
        )
    ]


def _apply_branch_protection(
    config: LocalConfig, executor: Executor, *, confirm: bool
) -> list[ActionResult]:
    results: list[ActionResult] = []
    for branch in sorted(config.bootstrap.branch_protection):
        cfg = config.bootstrap.branch_protection[branch]
        desired = BranchProtectionRule(
            required_approvals=cfg.required_approvals,
            required_checks=tuple(cfg.required_checks),
            enforce_admins=cfg.enforce_admins,
            dismiss_stale_reviews=cfg.dismiss_stale_reviews,
        )
        current = executor.branch_protection_current(branch)

        if current is None:
            if confirm:
                executor.set_branch_protection(branch, desired)
            results.append(
                ActionResult(
                    concern=Concern.BRANCH_PROTECTION,
                    target=branch,
                    outcome=Outcome.CREATED,
                    detail=f"branch protection set on '{branch}'.",
                )
            )
        elif current == desired:
            results.append(
                ActionResult(
                    concern=Concern.BRANCH_PROTECTION,
                    target=branch,
                    outcome=Outcome.UNCHANGED,
                    detail=f"branch protection on '{branch}' already matches.",
                )
            )
        else:
            if confirm:
                executor.set_branch_protection(branch, desired)
            results.append(
                ActionResult(
                    concern=Concern.BRANCH_PROTECTION,
                    target=branch,
                    outcome=Outcome.UPDATED,
                    detail=f"branch protection on '{branch}' updated to match config.",
                )
            )
    return results


# Ordered by concern (repo first, branch protection last) to mirror the plan.
_APPLIERS = (
    _apply_repo,
    _apply_secrets,
    _apply_oidc,
    _apply_runners,
    _apply_branch_protection,
)


def _dry_run_result(plan: BootstrapPlan) -> ApplyResult:
    """Turn a plan into an all-``SKIPPED``:class:`ApplyResult` (no mutations)."""
    results = tuple(
        ActionResult(
            concern=action.concern,
            target=plan.repo,
            outcome=Outcome.SKIPPED,
            detail=action.description,
        )
        for action in plan.actions
    )
    return ApplyResult(repo=plan.repo, dry_run=True, results=results)


def apply_bootstrap(
    cicd_dir: Path | str,
    *,
    executor: Executor,
    environ: dict[str, str] | None = None,
    confirm: bool = False,
) -> ApplyResult:
    """Apply the bootstrap plan idempotently through ``executor``.

 **Dry-run by default.** With ``confirm=False`` (the default) this performs NO
 mutating executor call: it loads + validates the config, builds the plan, and
 returns an all-:data:`Outcome.SKIPPED`:class:`ApplyResult` so a caller can see
 what ``--apply`` would do. Only ``confirm=True`` executes.

 **Idempotent.** With ``confirm=True`` each concern detects existing state first,
 then creates/updates only what is absent or divergent; re-running against
 unchanged state yields all :data:`Outcome.UNCHANGED` and creates no duplicates.

 **Value-free.** Secret / token values are read only at the moment they are
 handed to the executor and never enter the returned result or any log.

 Args:
 cicd_dir: the ``.cicd`` directory with ``bootstrap.local.yaml`` /
 ``secrets.local.yaml``.
 executor: the injected vendor/cloud seam (a fake in tests). Keyword-only.
 environ: inject an environment mapping for ``*_env`` token resolution;
 defaults to the real process environment.
 confirm: must be ``True`` to perform live actions. ``False`` (default) is a
 dry-run that mutates nothing.

 Returns:
 A value-free :class:`ApplyResult`.

 Raises:
 cicdgen.localconfig.LocalConfigError: if a required key is missing/empty or a
 required token env var is unset — message names the KEY only.
 """
    # Always load + validate first (fail-fast, naming the missing KEY). When token
    # resolution is only needed for actual registration, we still resolve so a
    # dry-run surfaces a missing token the same way --apply would.
    config = load_local_config(cicd_dir, resolve_env_tokens=True, environ=environ)

    if not confirm:
        plan = plan_bootstrap(cicd_dir, environ=environ)
        return _dry_run_result(plan)

    results: list[ActionResult] = []
    for applier in _APPLIERS:
        results.extend(applier(config, executor, confirm=True))

    repo = f"{config.bootstrap.repo.owner}/{config.bootstrap.repo.name}"
    return ApplyResult(repo=repo, dry_run=False, results=tuple(results))


# ---------------------------------------------------------------------------
# rendering — the value-free apply report
# ---------------------------------------------------------------------------
_OUTCOME_TAGS: dict[Outcome, str] = {
    Outcome.CREATED: "created",
    Outcome.UPDATED: "updated",
    Outcome.UNCHANGED: "unchanged",
    Outcome.SKIPPED: "skipped",
}


def render_apply_result(result: ApplyResult) -> str:
    """Format an :class:`ApplyResult` as a value-free, loggable report."""
    lines: list[str] = []
    mode = "DRY RUN — no changes made" if result.dry_run else "APPLIED"
    header = f"Bootstrap apply for {result.repo}  [{mode}]"
    lines.append(header)
    lines.append("=" * len(header))
    lines.append("")
    lines.append(
        f"{len(result.results)} action(s): "
        f"{len(result.created)} created, {len(result.updated)} updated, "
        f"{len(result.unchanged)} unchanged, {len(result.skipped)} skipped."
    )
    lines.append("")
    for r in result.results:
        lines.append(f"  - [{_OUTCOME_TAGS[r.outcome]}] ({r.concern}) {r.target}: {r.detail}")
    return "\n".join(lines)


def print_apply_result(result: ApplyResult) -> None:
    """Print the value-free apply report to stdout."""
    print(render_apply_result(result))
