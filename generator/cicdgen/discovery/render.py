"""Render a :class:`StackProposal` as a readable, review-ready report.

The report has three parts:

 1. a draft ``pipeline.yaml`` (the proposed IR as YAML the developer can save
 *after review* — discovery itself never writes it);
 2. the per-axis evidence (what signal drove each inference + confidence);
 3. the assumptions / "please confirm" notes.

Only :func:`render_proposal` returns a string; nothing here touches the disk.
"""

from __future__ import annotations

import yaml

from cicdgen.discovery.stack import Confidence, StackProposal

_CONFIDENCE_BADGE = {
    Confidence.HIGH: "HIGH  ",
    Confidence.MEDIUM: "MEDIUM",
    Confidence.LOW: "LOW   ",
}


def render_draft_yaml(proposal: StackProposal) -> str:
    """Render just the draft IR as YAML (what the developer saves after review)."""
    header = (
        "# DRAFT pipeline.yaml — PROPOSED by stack discovery (R10.1/R10.2).\n"
        "# This is a proposal only. Review every field, confirm the ci_vendor and\n"
        "# target_platform, then save as pipeline.yaml. Nothing was written for you.\n"
    )
    body = yaml.safe_dump(proposal.ir, sort_keys=False, default_flow_style=False)
    return header + body


def render_proposal(proposal: StackProposal) -> str:
    """Render the full proposal: draft YAML + evidence + confirmation notes."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"Stack discovery proposal for: {proposal.repo_root}")
    draft_flag = "  [DRAFT — not generation-ready]" if proposal.is_draft else ""
    lines.append(f"Overall confidence: {proposal.lowest_confidence().value}{draft_flag}")
    lines.append("=" * 72)
    lines.append("")

    # Part 1 — the draft IR.
    lines.append("--- Proposed draft pipeline.yaml ".ljust(72, "-"))
    lines.append(render_draft_yaml(proposal).rstrip())
    lines.append("")

    # Part 2 — evidence per axis.
    lines.append("--- Evidence ".ljust(72, "-"))
    for axis in ("ci_vendor", "target_platform", "cloud.provider", "project.language"):
        ev = proposal.evidence.get(axis)
        if ev is None:
            continue
        badge = _CONFIDENCE_BADGE[ev.confidence]
        value = ev.value if ev.value is not None else "(unresolved)"
        signals = ", ".join(ev.signals) if ev.signals else "(no direct signal)"
        lines.append(f"[{badge}] {axis} = {value}")
        lines.append(f"           signals: {signals}")
        if ev.note:
            lines.append(f"           note: {ev.note}")
    lines.append("")

    # Part 3 — please confirm.
    lines.append("--- Please confirm before generating (R10.2) ".ljust(72, "-"))
    if proposal.assumptions:
        for a in proposal.assumptions:
            lines.append(f"  • {a}")
    else:
        lines.append("  • (no outstanding assumptions — still confirm before generating)")
    lines.append("")
    lines.append(
        "Discovery PROPOSES only; it did not write any files. Confirm/edit the draft "
        "above, then save it as pipeline.yaml to proceed."
    )
    lines.append("=" * 72)

    return "\n".join(lines)
