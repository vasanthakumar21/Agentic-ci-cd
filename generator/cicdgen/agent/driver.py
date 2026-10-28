"""Drive the generator from a free-text instruction.

This is the *code* half of the custom agent. Given an instruction like::

 generate for ci_vendor=github target_platform=aws-ecs with region=eu-west-2 project=orders-api

and a ``repo_root``,:func:`run_instruction`:

 1. parses the instruction (:mod:`cicdgen.agent.instruction`);
 2. assembles an IR by starting from a discovery *draft*
 (:func:`cicdgen.discovery.discover_stack`) when the repo has signals, then
 layering the explicitly-stated axes + overrides on top — **explicit wins
 over discovery**;
 3. validates the assembled IR (:func:`cicdgen.ir.validate_ir`);
 4. drives :func:`cicdgen.core.generate` — **dry-run by default** (propose a
 plan first) — and returns the plan, or the real
:class:`~cicdgen.core.GenerationResult` when ``apply=True``.

The one hard rule: this driver assembles the IR *inputs* and ALWAYS routes
structure through ``cicdgen.core.generate`` (the plugins). It NEVER writes a
pipeline / IaC file itself, never hard-codes a vendor/platform, never inlines a
secret, and never invents a verb. Gap-fill is for inputs only.

If a required axis is named by neither the instruction nor discovery (no signal
to lean on), the driver does NOT guess: it returns a :class:`DriverResult` with
``needs_confirmation=True`` and a clear question, matching discovery's
"propose, never silently assume" contract.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cicdgen.agent.instruction import ParsedInstruction, parse_instruction
from cicdgen.core import (
    GenerationPlan,
    GenerationResult,
    generate,
)
from cicdgen.discovery import Confidence, StackProposal, discover_stack
from cicdgen.ir import IRValidationError, validate_ir
from cicdgen.ir.models import Pipeline
from cicdgen.plugins.loader import Registry

# The two axes the IR names independently — both are required to generate.
_REQUIRED_AXES = ("ci_vendor", "target_platform")


@dataclass(frozen=True)
class DriverResult:
    """The structured outcome of driving the generator from an instruction.

 Exactly one of two shapes:

 * **needs confirmation** (``needs_confirmation=True``) — a required axis
 could not be resolved from the instruction OR discovery, so the driver
 refused to guess. ``questions`` explains what the human must
 supply; ``ir`` / ``plan`` / ``result`` are ``None``.
 * **ready** (``needs_confirmation=False``) — the IR was assembled and
 validated and the generator was driven. ``plan`` holds the dry-run plan
 (default), or ``result`` holds the real :class:`GenerationResult` when
 ``apply=True``.

 ``parsed`` is always set (the parsed instruction). ``ir`` is the assembled,
 validated :class:`Pipeline` when generation proceeded. ``assembled_ir`` is
 the raw IR dict the driver built (handy for echoing / review), even in the
 needs-confirmation case where it is incomplete. ``gap_fills`` records the
 repo-specific inputs the agent filled from discovery/overrides, for audit.
 """

    parsed: ParsedInstruction
    applied: bool
    needs_confirmation: bool
    assembled_ir: dict[str, Any] = field(default_factory=dict)
    ir: Pipeline | None = None
    plan: GenerationPlan | None = None
    result: GenerationResult | None = None
    questions: tuple[str, ...] = ()
    gap_fills: tuple[str, ...] = ()
    proposal: StackProposal | None = field(default=None, repr=False)

    @property
    def ci_vendor(self) -> str | None:
        return self.assembled_ir.get("ci_vendor")

    @property
    def target_platform(self) -> str | None:
        return self.assembled_ir.get("target_platform")


def _axis_from_discovery(
    proposal: StackProposal | None, axis: str
) -> tuple[str | None, bool]:
    """Return (value, is_confident) for an axis from a discovery proposal.

 A value is only treated as a usable discovery *signal* when its evidence
 confidence is MEDIUM or HIGH. A LOW-confidence default (discovery's "PLEASE
 CONFIRM" fallback) is NOT a signal — leaning on it would be guessing, which
 the driver refuses. Returns ``(None, False)`` when there is no
 proposal or no confident value.
 """
    if proposal is None:
        return None, False
    ev = proposal.evidence.get(axis)
    if ev is None or ev.value is None:
        return None, False
    confident = ev.confidence in (Confidence.HIGH, Confidence.MEDIUM)
    return (ev.value, True) if confident else (ev.value, False)


def _assemble_ir(
    parsed: ParsedInstruction,
    proposal: StackProposal | None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Build the IR dict: discovery draft as a base, explicit instruction on top.

 Returns ``(ir_dict, gap_fills, missing_axes)``:

 * ``ir_dict`` — the assembled IR (a deep copy of the discovery draft, or a
 minimal scaffold when there is no proposal), with explicit axes + overrides
 layered on top (explicit wins);
 * ``gap_fills`` — human-readable notes on what was filled from discovery /
 overrides (audit trail);
 * ``missing_axes`` — required axes that neither the instruction nor a
 *confident* discovery signal could supply (→ needs confirmation).
 """
    gap_fills: list[str] = []

    # Base: a copy of the discovery draft when we have one, else a scaffold the
    # driver completes. The scaffold mirrors discovery's sensible defaults so a
    # no-discovery run (e.g. a brand-new repo) still produces a valid IR once the
    # two axes are supplied.
    if proposal is not None:
        ir: dict[str, Any] = copy.deepcopy(proposal.ir)
        gap_fills.append(
            f"started from discovery draft for '{proposal.repo_root.name}' "
            f"(ci_vendor={proposal.ir.get('ci_vendor')}, "
            f"target_platform={proposal.ir.get('target_platform')})"
        )
    else:
        ir = {
            "version": 1,
            "project": {},
            "environments": [
                {"name": "dev", "gate": "auto", "protection": "none"},
                {"name": "prod", "gate": "manual", "protection": "required-approvals"},
            ],
            "stages": [
                {"id": "test", "runs": "make test"},
                {"id": "build", "runs": "make build"},
                {"id": "deploy-dev", "environment": "dev", "runs": "make deploy ENV=dev"},
                {
                    "id": "deploy-prod",
                    "environment": "prod",
                    "when": "manual",
                    "runs": "make deploy ENV=prod",
                },
            ],
        }
        ir.setdefault("project", {})

    # --- Axis resolution: explicit instruction > confident discovery -----------
    missing_axes: list[str] = []
    for axis in _REQUIRED_AXES:
        explicit = getattr(parsed, axis)
        if explicit is not None:
            prior = ir.get(axis)
            ir[axis] = explicit
            if prior is not None and prior != explicit:
                gap_fills.append(
                    f"{axis}: instruction '{explicit}' overrides discovery '{prior}'"
                )
            else:
                gap_fills.append(f"{axis}: '{explicit}' (from instruction)")
            continue

        # No explicit value — can discovery supply it *confidently*?
        value, confident = _axis_from_discovery(proposal, axis)
        if confident and value is not None:
            ir[axis] = value
            gap_fills.append(f"{axis}: '{value}' (gap-filled from discovery)")
        else:
            # Remove any low-confidence default so validation/needs-confirm is honest.
            ir.pop(axis, None)
            missing_axes.append(axis)

    # --- Overrides → their IR homes (gap-fill repo-specific inputs) -------------
    project = ir.setdefault("project", {})
    if not project.get("name"):
        # Fall back to a discovered/overridden project name so the IR is complete.
        if proposal is not None and proposal.ir.get("project", {}).get("name"):
            project["name"] = proposal.ir["project"]["name"]

    if "project" in parsed.overrides:
        project["name"] = parsed.overrides["project"]
        gap_fills.append(f"project.name: '{parsed.overrides['project']}' (from override)")

    if "language" in parsed.overrides:
        project["language"] = parsed.overrides["language"]
        gap_fills.append(f"project.language: '{parsed.overrides['language']}' (from override)")

    if "region" in parsed.overrides:
        cloud = ir.setdefault("cloud", {})
        cloud["region"] = parsed.overrides["region"]
        # Keep a provider if discovery proposed one; cloud.region alone is fine
        # otherwise the IR validator requires a provider — so only set region when
        # a provider is already present, else stash it as a setting (handled below).
        if "provider" not in cloud:
            # Cannot have a region without a provider under the IR schema; move it
            # to settings instead of inventing a provider (never guess).
            ir.pop("cloud", None)
            gap_fills.append(
                "region: no cloud.provider known; forwarded as a plugin setting "
                "rather than inventing a provider"
            )
        else:
            gap_fills.append(f"cloud.region: '{parsed.overrides['region']}' (from override)")

    return ir, gap_fills, missing_axes


def _needs_confirmation(
    parsed: ParsedInstruction,
    assembled_ir: dict[str, Any],
    missing_axes: list[str],
    gap_fills: list[str],
    proposal: StackProposal | None,
) -> DriverResult:
    """Build a needs-confirmation result — refuse to guess a missing axis."""
    questions = tuple(
        f"Which '{axis}' should I use? It was not named in the instruction and "
        f"discovery had no confident signal for it — I won't guess."
        for axis in missing_axes
    )
    return DriverResult(
        parsed=parsed,
        applied=False,
        needs_confirmation=True,
        assembled_ir=assembled_ir,
        questions=questions,
        gap_fills=tuple(gap_fills),
        proposal=proposal,
    )


def run_instruction(
    instruction: str,
    repo_root: Path | str,
    *,
    apply: bool = False,
    use_discovery: bool = True,
    plugins_root: Path | str | None = None,
    registry: Registry | None = None,
    scan_secrets: bool = True,
    secret_values: dict[str, str] | None = None,
) -> DriverResult:
    """Parse an instruction, assemble the IR, and drive the generator.

 Dry-run by default (``apply=False``): assembles + validates the IR and returns
 the :class:`GenerationPlan` the core would write, touching nothing on disk.
 With ``apply=True`` it writes via :func:`cicdgen.core.generate` (through the
 plugins) and returns a :class:`GenerationResult`.

 The driver never writes pipeline/IaC files itself — all structure goes through
 the core/plugins. It only assembles the IR *inputs* (axes + overrides, layered
 over a discovery draft) and forwards non-secret overrides to the plugins as
 ``settings``.

 Args:
 instruction: free-text, e.g.
 ``"generate for ci_vendor=github target_platform=aws-ecs with region=eu-west-2 project=orders-api"``.
 repo_root: the client repo to discover from and generate into.
 apply: when True, actually write (through the core); otherwise dry-run.
 use_discovery: when True (default), seed the IR from
:func:`discover_stack`; set False to build purely from the instruction.
 plugins_root: the plugins *directory* that holds ``ci/`` and ``target/``
 (defaults to the product's ``plugins/``). Consistent with
:func:`cicdgen.core.generate`; the IR plugin-existence check is given
 this directory's parent as its repo root.
 registry: optional in-memory plugin registry (tests / embedding) — bypasses
 the on-disk plugin tree, exactly like :func:`cicdgen.core.generate`.
 scan_secrets: run the secret-leak heuristic in the core (never disable in
 production).
 secret_values: optional real secret ``name -> value`` map forwarded to the
 core's exact-value leak guard (values are never written/echoed).

 Returns:
 A :class:`DriverResult`. ``needs_confirmation=True`` when a required axis
 could not be resolved without guessing; otherwise ``plan`` (dry-run)
 or ``result`` (apply) is populated.

 Raises:
 ValueError: if the instruction is empty.
 IRValidationError: if the assembled IR fails validation (bad enum, unknown
 plugin, missing required field) — surfaced unchanged so the caller sees
 the precise, field-level message.
 """
    parsed = parse_instruction(instruction)
    root = Path(repo_root)

    proposal: StackProposal | None = None
    if use_discovery:
        try:
            proposal = discover_stack(root)
        except NotADirectoryError:
            # No usable repo to discover from — fall back to instruction-only.
            proposal = None

    assembled_ir, gap_fills, missing_axes = _assemble_ir(parsed, proposal)

    # Fold the non-axis, non-IR-placed overrides into plugin settings.
    settings: dict[str, str] = dict(parsed.extra_settings)
    # A region we could not place on cloud.provider is still a useful verb input.
    if "region" in parsed.overrides and "region" not in settings:
        if "cloud" not in assembled_ir or "region" not in assembled_ir.get("cloud", {}):
            settings["region"] = parsed.overrides["region"]

    # Refuse to guess a missing axis.
    if missing_axes:
        return _needs_confirmation(parsed, assembled_ir, missing_axes, gap_fills, proposal)

    # Validate the assembled IR. When a registry is injected the on-disk plugin
    # existence check is meaningless (plugins live in memory), so skip it — matches
    # cicdgen.core behaviour.
    #
    # Root convention: ``plugins_root`` names the *plugins directory* (the folder
    # that holds ``ci/`` and ``target/``), consistent with ``cicdgen.core.generate``
    # and the plugin loader. The IR plugin-existence check, however, discovers from
    # a *repo root* and looks under ``<repo_root>/plugins/``. So when an explicit
    # ``plugins_root`` is supplied we hand the IR validator that directory's PARENT
    # (the repo root), keeping a single ``plugins_root`` correct for both consumers.
    ir_check_root = Path(plugins_root).parent if plugins_root is not None else None
    pipeline: Pipeline = validate_ir(
        assembled_ir,
        source=f"<agent instruction: {parsed.raw!r}>",
        check_plugins=registry is None,
        repo_root=ir_check_root,
    )

    out = generate(
        pipeline,
        root,
        plugins_root=plugins_root,
        registry=registry,
        settings=settings or None,
        check_plugins=registry is None,
        scan_secrets=scan_secrets,
        secret_values=secret_values,
        dry_run=not apply,
    )

    if apply:
        assert isinstance(out, GenerationResult)
        return DriverResult(
            parsed=parsed,
            applied=True,
            needs_confirmation=False,
            assembled_ir=assembled_ir,
            ir=pipeline,
            result=out,
            gap_fills=tuple(gap_fills),
            proposal=proposal,
        )

    assert isinstance(out, GenerationPlan)
    return DriverResult(
        parsed=parsed,
        applied=False,
        needs_confirmation=False,
        assembled_ir=assembled_ir,
        ir=pipeline,
        plan=out,
        gap_fills=tuple(gap_fills),
        proposal=proposal,
    )


__all__ = [
    "DriverResult",
    "run_instruction",
    "IRValidationError",
]
