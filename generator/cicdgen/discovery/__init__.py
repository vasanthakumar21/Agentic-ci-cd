"""Client stack discovery.

Point the agent at a client repo; it inspects *filesystem signals only*
(deterministic, no network) and PROPOSES a draft IR naming a likely
``ci_vendor`` and ``target_platform``, with per-axis evidence + confidence and a
list of assumptions the developer must confirm.

Discovery PROPOSES only. It never writes generated output and never silently
assumes a client's platform — the developer confirms or edits the draft IR before
any generation happens. The draft can be rendered to YAML the developer saves to
``pipeline.yaml`` *after review*.

Public surface:

 from cicdgen.discovery import discover_stack, render_proposal
 proposal = discover_stack("/path/to/client/repo")
 print(render_proposal(proposal))
"""

from __future__ import annotations

from cicdgen.discovery.render import render_proposal
from cicdgen.discovery.stack import (
    Confidence,
    Evidence,
    StackProposal,
    discover_stack,
)

__all__ = [
    "discover_stack",
    "render_proposal",
    "StackProposal",
    "Evidence",
    "Confidence",
]
