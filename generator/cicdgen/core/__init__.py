"""Generator core — the "render → write + manifest" entry point.

Ties the pieces together into one deterministic, re-runnable entry point:

 1. validate the IR (``cicdgen.ir.load_and_validate_ir``)
 2. dispatch to the two plugins (``cicdgen.plugins.dispatch`` — select + render)
 3. write the files under the target repo root
 4. record them in ``.cicd/generated.manifest.json`` for clean regeneration /
 axis-swap removal

Public surface:

 * ``generate(ir, repo_root,...)`` — validate → render → write + manifest
 * ``plan(ir, repo_root,...)`` — dry-run: what WOULD be written
 * ``GenerationResult`` / ``GenerationPlan`` — structured outcomes
 * ``Manifest`` / ``ManifestEntry`` — the generated-file manifest model
 * ``MANIFEST_RELPATH`` / ``load_manifest`` — manifest location + reader
 * ``AxisChange`` / ``detect_axis_change`` — axis-swap migration detection:
 classify a regenerate as a ci_vendor / target_platform swap vs. the prior
 manifest; surfaced on the plan/result as ``axis_change``
 * ``GenerationError`` — safety failures (path escape, leak)

Both ``plugins_root`` and ``registry`` are injectable so the core is testable
with in-memory plugins.
"""

from __future__ import annotations

from cicdgen.core.capabilities import (
    CapabilityError,
    required_capabilities,
    validate_capabilities,
)
from cicdgen.core.doctor import (
    DoctorError,
    DriftCategory,
    DriftEntry,
    DriftReport,
    LiveFinding,
    LiveReport,
    LiveStatus,
    check_drift,
    check_live_wiring,
    render_drift_report,
    render_live_report,
)
from cicdgen.core.generate import (
    GenerationError,
    GenerationPlan,
    GenerationResult,
    generate,
    load_manifest,
    plan,
)
from cicdgen.core.manifest import (
    MANIFEST_RELPATH,
    MANIFEST_VERSION,
    Manifest,
    ManifestEntry,
    content_hash,
)
from cicdgen.core.migration import (
    AxisChange,
    detect_axis_change,
    detect_axis_change_for_repo,
)
from cicdgen.core.policy import (
    POLICY_RELPATH,
    RULES,
    PolicyDocumentError,
    PolicyError,
    Violation,
    block_prod_deploy_friday,
    enforce_policies,
    evaluate_policies,
    load_policy_doc,
    minimum_coverage,
    require_security_scan,
)
from cicdgen.core.runbook import (
    RUNBOOK_RELPATH,
    render_migration_runbook,
    write_migration_runbook,
)
from cicdgen.core.secrets_guard import (
    SecretLeak,
    enforce_secrets_by_reference,
    references_are_plausible,
)
from cicdgen.core.stage_graph import (
    CyclicDependencyError,
    StageGraphError,
    UndeclaredDependencyError,
    build_ordered_stage_graph,
    detect_cycles,
    ordered_stages,
    topological_order,
    validate_dependencies,
)

__all__ = [
    # orchestration
    "generate",
    "plan",
    "GenerationResult",
    "GenerationPlan",
    "GenerationError",
    "load_manifest",
    # drift detection — cicd doctor
    "DoctorError",
    "DriftCategory",
    "DriftEntry",
    "DriftReport",
    "check_drift",
    "render_drift_report",
    "LiveStatus",
    "LiveFinding",
    "LiveReport",
    "check_live_wiring",
    "render_live_report",
    # capability gate
    "CapabilityError",
    "required_capabilities",
    "validate_capabilities",
    # canonical stage graph
    "StageGraphError",
    "UndeclaredDependencyError",
    "CyclicDependencyError",
    "validate_dependencies",
    "detect_cycles",
    "topological_order",
    "ordered_stages",
    "build_ordered_stage_graph",
    # policy engine
    "PolicyError",
    "PolicyDocumentError",
    "Violation",
    "RULES",
    "POLICY_RELPATH",
    "require_security_scan",
    "minimum_coverage",
    "block_prod_deploy_friday",
    "evaluate_policies",
    "enforce_policies",
    "load_policy_doc",
    # manifest
    "Manifest",
    "ManifestEntry",
    "content_hash",
    "MANIFEST_RELPATH",
    "MANIFEST_VERSION",
    # axis-swap migration detection
    "AxisChange",
    "detect_axis_change",
    "detect_axis_change_for_repo",
    # migration runbook emitter
    "render_migration_runbook",
    "write_migration_runbook",
    "RUNBOOK_RELPATH",
    # secrets-by-reference guard
    "SecretLeak",
    "enforce_secrets_by_reference",
    "references_are_plausible",
]
