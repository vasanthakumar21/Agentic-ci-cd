"""Bootstrapper — maps local config → vendor/cloud wiring, dry-run by default.

This package holds the one-time, idempotent bootstrap logic. The PLANNING half
reads ``.cicd/*.local.yaml`` and produces an ordered, value-free **plan** of what
a bootstrap run would do (push N secrets, create the OIDC role, register runners,
set branch protection, create the repo) WITHOUT executing anything. The EXECUTION
half (:mod:`cicdgen.bootstrap.apply`) is an idempotent ``--apply`` that detects
existing resources and creates no duplicates, behind an injectable
:class:`Executor` seam and gated by an explicit ``confirm`` flag (dry-run by
default).

Hard rule: the planner describes secrets by NAME only and never reads, logs, or
renders a secret / token value. The apply layer reads a value only at the moment
it hands it to the executor, and never logs it.

Public surface:

 *:func:`plan_bootstrap` — load + validate ``.cicd`` → dry-run :class:`BootstrapPlan`.
 *:func:`plan_from_config` — build a plan from an already-loaded ``LocalConfig``.
 *:func:`render_plan` /:func:`print_plan` — format the dry-run report (value-free).
 *:class:`BootstrapPlan` /:class:`BootstrapAction` /:class:`Concern` — the model.
 *:func:`apply_bootstrap` — idempotent execution (dry-run unless ``confirm=True``).
 *:class:`Executor` — the injectable vendor/cloud seam (faked in tests).
 *:class:`ApplyResult` /:class:`ActionResult` /:class:`Outcome` — the apply model.
 *:class:`BranchProtectionRule` — comparable rule snapshot used for idempotency.
 *:func:`render_apply_result` /:func:`print_apply_result` — value-free apply report.
 *:func:`emit_automation_report` — the "automated X vs manual Y (see runbook)"
 report: separates what bootstrap automates from the irreducible manual
 prerequisites. Value-free.
 *:class:`AutomationReport` /:class:`ManualStep` — the report model.
"""

from __future__ import annotations

from cicdgen.bootstrap.apply import (
    ActionResult,
    ApplyResult,
    BranchProtectionRule,
    Executor,
    Outcome,
    apply_bootstrap,
    print_apply_result,
    render_apply_result,
)
from cicdgen.bootstrap.planner import (
    BootstrapAction,
    BootstrapPlan,
    Concern,
    plan_bootstrap,
    plan_from_config,
    print_plan,
    render_plan,
)
from cicdgen.bootstrap.report import (
    AutomationReport,
    ManualStep,
    build_automation_report,
    emit_automation_report,
    emit_automation_report_from_plan,
    render_automation_report,
)

__all__ = [
    "ActionResult",
    "ApplyResult",
    "AutomationReport",
    "BootstrapAction",
    "BootstrapPlan",
    "BranchProtectionRule",
    "Concern",
    "Executor",
    "ManualStep",
    "Outcome",
    "apply_bootstrap",
    "build_automation_report",
    "emit_automation_report",
    "emit_automation_report_from_plan",
    "plan_bootstrap",
    "plan_from_config",
    "print_apply_result",
    "print_plan",
    "render_apply_result",
    "render_automation_report",
    "render_plan",
]
