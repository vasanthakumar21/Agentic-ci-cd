"""Org-level policy engine.

An org-level policy layer that enforces declarative rules on the vendor-neutral
IR BEFORE generation renders anything (fail-fast), producing precise, value-free
violation messages. Policies hold **no vendor/platform knowledge**: every
predicate reads only the vendor-neutral :class:`Pipeline` IR and references no
``CiVendor`` / ``TargetPlatform`` literal, so the SAME policy set applies
identically to ``github × aws-ecs`` and ``azure-devops × databricks``.

Where policies come from
------------------------
A committed, git-tracked ``.cicd/policies.yaml`` (org-level, NOT secret — it IS
committed, unlike the ``*.local.yaml`` files). Its shape:

.. code-block:: yaml

 version: 1
 policies:
 - rule: require_security_scan
 params: { scans: [sast, secrets] }
 - rule: minimum_coverage
 params: { percent: 80 }
 - rule: block_prod_deploy_friday
 params: { environments: [prod] }

Engine
------
Each rule is a small **pure predicate** with the signature
``evaluate(pipeline, params, *, clock_day=None) -> list[Violation]``. The registry
:data:`RULES` maps a rule name to its predicate — a **data-only** ``dict`` so
adding an org rule never adds a vendor/platform branch anywhere in the core.
:func:`evaluate_policies` runs every configured rule and **aggregates ALL**
violations (not just the first) so a developer fixes the IR in one pass.

Each :class:`Violation` names its ``rule`` and the offending ``ir_path`` and
carries a value-free ``message``.

Determinism
-----------
``block_prod_deploy_friday`` is **temporal**: the day-of-week is INJECTED via the
``clock_day`` parameter and is **never** read from the wall clock inside
generation, so generation stays byte-reproducible. A rule that needs the day but
is given none simply does not fire (it cannot assert a temporal violation without
an injected day), which keeps generation deterministic by construction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from cicdgen.ir.models import Pipeline, ScanType

#: Repo-relative location of the committed, org-level policy document. NOT a
#: ``*.local.*`` file: it is git-tracked (see ``.gitignore``) so policy changes
#: are reviewed in the diff. Absent file => is a no-op.
POLICY_RELPATH = ".cicd/policies.yaml"


class PolicyError(RuntimeError):
    """Raised when the IR breaks one or more org policies.

 Fail-fast and value-free: the message lists EVERY violation in one pass
 — each naming its rule + offending IR path — so a developer fixes the IR in a
 single edit. Carries the structured :class:`Violation` list for programmatic
 use (``exc.violations``).
 """

    def __init__(self, violations: Sequence[Violation]) -> None:
        self.violations: tuple[Violation, ...] = tuple(violations)
        super().__init__(self._format(self.violations))

    @staticmethod
    def _format(violations: Sequence[Violation]) -> str:
        header = (
            f"policy gate failed: {len(violations)} violation(s) "
            f"(the IR must satisfy every org policy in .cicd/policies.yaml):"
        )
        lines = [header]
        for v in violations:
            lines.append(f"  - [{v.rule}] {v.message} (ir_path: {v.ir_path})")
        return "\n".join(lines)


@dataclass(frozen=True)
class Violation:
    """A single policy breach, named by rule + offending IR location (value-free).

 ``rule`` is the registry rule name; ``message`` explains the breach WITHOUT
 echoing any secret value; ``ir_path`` points at the offending IR location
 (e.g. ``"thresholds.coverage_min_percent"``) so the author knows exactly what
 to change.
 """

    rule: str
    message: str
    ir_path: str


#: A policy predicate: a pure function of the vendor-neutral IR + its params that
#: returns the (possibly empty) list of violations it finds. ``clock_day`` is the
#: INJECTED day-of-week (lower-case full name, e.g. ``"friday"``) for temporal
#: rules; non-temporal rules ignore it. Platform-blind.
Predicate = Callable[[Pipeline, Mapping[str, Any]], "list[Violation]"]


# ---------------------------------------------------------------------------
# Rule predicates — each pure, platform-blind, value-free
# ---------------------------------------------------------------------------
def require_security_scan(
    pipeline: Pipeline,
    params: Mapping[str, Any],
    *,
    clock_day: str | None = None,
) -> list[Violation]:
    """Pass iff SOME stage's ``scans`` cover every required scan type.

 The required set is the union of ``params["scans"]``; the provided set is the
 union of every stage's ``scans``. A violation is reported (naming the missing
 scan types) when ``required - provided`` is non-empty. ``clock_day`` is unused
 (this rule is not temporal).

 Args:
 pipeline: the vendor-neutral IR.
 params: ``{"scans": [<scan type>,...]}`` — the required scan types.

 Returns:
 ``[]`` when satisfied, else a single :class:`Violation` on
 ``stages[].scans`` naming the missing scan type(s).
 """
    required = _scan_set(params.get("scans", []))
    if not required:
        return []
    provided: set[str] = set()
    for stage in pipeline.stages:
        for scan in stage.scans or ():
            provided.add(str(scan))
    missing = sorted(required - provided)
    if not missing:
        return []
    return [
        Violation(
            rule="require_security_scan",
            message=(
                f"required security scan(s) {missing} are not declared by any "
                f"stage; declared scans across all stages: "
                f"{sorted(provided) if provided else '(none)'}."
            ),
            ir_path="stages[].scans",
        )
    ]


def minimum_coverage(
    pipeline: Pipeline,
    params: Mapping[str, Any],
    *,
    clock_day: str | None = None,
) -> list[Violation]:
    """Pass iff ``thresholds.coverage_min_percent`` is present AND ``>= percent``.

 A missing ``thresholds`` block (or a missing ``coverage_min_percent``) is a
 violation: the org floor is not demonstrably met. ``clock_day`` is unused.

 Args:
 pipeline: the vendor-neutral IR.
 params: ``{"percent": <number in [0, 100]>}`` — the minimum floor.

 Returns:
 ``[]`` when satisfied, else a single :class:`Violation` on
 ``thresholds.coverage_min_percent``.
 """
    floor = params.get("percent")
    if floor is None:
        return []
    thresholds = pipeline.thresholds
    actual = thresholds.coverage_min_percent if thresholds is not None else None
    if actual is not None and actual >= floor:
        return []
    have = "unset" if actual is None else f"{actual}"
    return [
        Violation(
            rule="minimum_coverage",
            message=(
                f"coverage floor not met: policy requires "
                f"thresholds.coverage_min_percent >= {floor}, IR has {have}."
            ),
            ir_path="thresholds.coverage_min_percent",
        )
    ]


def block_prod_deploy_friday(
    pipeline: Pipeline,
    params: Mapping[str, Any],
    *,
    clock_day: str | None = None,
) -> list[Violation]:
    """Temporal rule: block prod deploys on the blocked day (default Friday).

 The day-of-week is **INJECTED** via ``clock_day`` and is never read from the
 wall clock inside generation, so evaluation is deterministic and
 reproducible. Passes iff the injected day is NOT the blocked day OR the IR
 contains
 no deploy stage targeting one of the ``params["environments"]`` (prod)
 environments. When ``clock_day`` is ``None`` the rule cannot assert a temporal
 violation and so does not fire (keeps generation deterministic by default).

 A "prod deploy stage" is an ENABLED stage whose ``environment`` names one of
 the blocked environments — the vendor-neutral IR signal for "this stage
 deploys to that environment".

 Args:
 pipeline: the vendor-neutral IR.
 params: ``{"environments": [<name>,...], "day": <weekday>?}``. ``day``
 defaults to ``"friday"`` and is matched case-insensitively.

 Returns:
 ``[]`` when satisfied, else a single :class:`Violation` on
 ``stages[].environment`` naming the blocked environment(s).
 """
    if clock_day is None:
        return []
    blocked_day = str(params.get("day", "friday")).strip().casefold()
    if clock_day.strip().casefold() != blocked_day:
        return []

    blocked_envs = {str(e) for e in params.get("environments", [])}
    if not blocked_envs:
        return []

    offending = sorted(
        {
            stage.environment
            for stage in pipeline.stages
            if stage.enabled
            and stage.environment is not None
            and stage.environment in blocked_envs
        }
    )
    if not offending:
        return []
    return [
        Violation(
            rule="block_prod_deploy_friday",
            message=(
                f"prod deploy is blocked on {blocked_day}: the IR deploys to "
                f"protected environment(s) {offending} via a stage, and the "
                f"injected day is the blocked day."
            ),
            ir_path="stages[].environment",
        )
    ]


# ---------------------------------------------------------------------------
# Data-only rule registry — adding a rule never adds a vendor branch
# ---------------------------------------------------------------------------
#: The rule name -> predicate table. Pure DATA: the engine looks a rule up here
#: and calls it; no vendor/platform branch exists anywhere. Adding an org rule is
#: a one-line addition here plus its pure predicate above.
RULES: dict[str, Predicate] = {
    "require_security_scan": require_security_scan,
    "minimum_coverage": minimum_coverage,
    "block_prod_deploy_friday": block_prod_deploy_friday,
}


class PolicyDocumentError(ValueError):
    """Raised when ``policies.yaml`` is structurally invalid (bad shape / rule).

 Fail-fast and precise, mirroring the IR's validation style: the message names
 the offending key / unknown rule. Distinct from :class:`PolicyError`, which
 signals that a (valid) policy set was BROKEN by the IR.
 """


def _scan_set(scans: Any) -> set[str]:
    """Normalise a params scan list to a set of canonical scan-type strings.

 Accepts the schema's scan values (``sast`` / ``secrets`` / ``dependency``) as
 plain strings or :class:`ScanType` members; an unknown value raises
:class:`PolicyDocumentError` naming it, so a typo in ``policies.yaml`` fails
 fast rather than silently never matching.
 """
    if not isinstance(scans, (list, tuple)):
        raise PolicyDocumentError(
            f"require_security_scan.params.scans must be a list, got {type(scans).__name__}."
        )
    out: set[str] = set()
    valid = {s.value for s in ScanType}
    for raw in scans:
        value = str(raw)
        if value not in valid:
            raise PolicyDocumentError(
                f"require_security_scan.params.scans contains unknown scan type "
                f"'{value}'; valid scan types are {sorted(valid)}."
            )
        out.add(value)
    return out


def _iter_policy_entries(policy_doc: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    """Validate the document shape and return ``[(rule, params),...]``.

 Enforces: ``version == 1``, ``policies`` is a list, each entry is a mapping
 with a known ``rule`` and a mapping ``params`` (defaulting to ``{}``). Unknown
 rules fail fast naming the rule and the known set (the known set is the
 data-only registry, not a vendor list).
 """
    version = policy_doc.get("version")
    if version != 1:
        raise PolicyDocumentError(
            f"policies.yaml version must be 1 (the only supported version), got {version!r}."
        )
    entries = policy_doc.get("policies", [])
    if not isinstance(entries, (list, tuple)):
        raise PolicyDocumentError(
            f"policies.yaml 'policies' must be a list, got {type(entries).__name__}."
        )
    resolved: list[tuple[str, Mapping[str, Any]]] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise PolicyDocumentError(
                f"policies[{i}] must be a mapping with a 'rule' key, got "
                f"{type(entry).__name__}."
            )
        rule = entry.get("rule")
        if rule not in RULES:
            raise PolicyDocumentError(
                f"policies[{i}] names unknown rule {rule!r}; known rules are "
                f"{sorted(RULES)}."
            )
        params = entry.get("params", {})
        if not isinstance(params, Mapping):
            raise PolicyDocumentError(
                f"policies[{i}] (rule '{rule}') params must be a mapping, got "
                f"{type(params).__name__}."
            )
        resolved.append((str(rule), params))
    return resolved


def evaluate_policies(
    pipeline: Pipeline,
    policy_doc: Mapping[str, Any] | None,
    *,
    clock_day: str | None = None,
) -> list[Violation]:
    """Run every configured rule against the IR and aggregate ALL violations.

 Platform-blind: reads only the vendor-neutral IR and the policy document;
 references no vendor/platform literal. The rules run in document
 order and their violations are concatenated in that order, so the result is
 deterministic for a given ``(pipeline, policy_doc, clock_day)``.

 Args:
 pipeline: the vendor-neutral IR.
 policy_doc: the parsed ``policies.yaml`` mapping, or ``None`` when no
 policy file exists — in which case the result is empty (the gate is a
 no-op).
 clock_day: the INJECTED day-of-week for temporal rules (lower-case full
 weekday name, e.g. ``"friday"``). Never read from the wall clock here.
 ``None`` means no temporal rule fires.

 Returns:
 Every :class:`Violation` found, aggregated across all configured rules
 (empty when the IR satisfies every policy).

 Raises:
 PolicyDocumentError: if ``policy_doc`` is structurally invalid (bad
 version/shape or an unknown rule).
 """
    if policy_doc is None:
        return []
    violations: list[Violation] = []
    for rule_name, params in _iter_policy_entries(policy_doc):
        predicate = RULES[rule_name]
        violations.extend(predicate(pipeline, params, clock_day=clock_day))
    return violations


def load_policy_doc(repo_root: Path | str) -> Mapping[str, Any] | None:
    """Load the committed ``.cicd/policies.yaml`` under ``repo_root``.

 Returns the parsed mapping, or ``None`` when no policy file exists (the gate
 is then a no-op). An empty/blank file parses to an
 empty document ``{}`` rather than ``None`` so a deliberately empty policy file
 is distinguishable from an absent one;:func:`evaluate_policies` treats an
 empty doc as "zero rules" via :func:`_iter_policy_entries` (which still checks
 the version — a blank file has no version and fails fast, matching the
 fail-fast posture; ship ``version: 1\\npolicies: []`` for an explicit no-op).

 Args:
 repo_root: the repo being generated into.

 Returns:
 The parsed policy mapping, or ``None`` if the file is absent.

 Raises:
 PolicyDocumentError: if the file exists but is not a YAML mapping.
 """
    path = Path(repo_root) / POLICY_RELPATH
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PolicyDocumentError(
            f"{POLICY_RELPATH} is not valid YAML: {exc}."
        ) from exc
    if raw is None:
        # An empty file: treat as an explicit-but-empty document so the version
        # check in _iter_policy_entries fires with a precise message.
        return {}
    if not isinstance(raw, Mapping):
        raise PolicyDocumentError(
            f"{POLICY_RELPATH} must be a mapping with 'version' and 'policies' "
            f"keys, got {type(raw).__name__}."
        )
    return raw


def enforce_policies(
    pipeline: Pipeline,
    policy_doc: Mapping[str, Any] | None,
    *,
    clock_day: str | None = None,
) -> None:
    """Run the policy gate: evaluate every policy and raise if ANY is broken.

 Aggregates every :class:`Violation` first (via :func:`evaluate_policies`) and,
 if the list is non-empty, raises a single :class:`PolicyError` listing ALL of
 them in one pass — so a developer fixes the IR in one edit. A ``None`` document
 makes this a no-op.

 Args:
 pipeline: the vendor-neutral IR.
 policy_doc: the parsed ``policies.yaml``, or ``None`` when absent.
 clock_day: the INJECTED day-of-week for temporal rules.

 Raises:
 PolicyError: listing every violation when the IR breaks one or more rules.
 PolicyDocumentError: if the document is structurally invalid.
 """
    violations = evaluate_policies(pipeline, policy_doc, clock_day=clock_day)
    if violations:
        raise PolicyError(violations)
