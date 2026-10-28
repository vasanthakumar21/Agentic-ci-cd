"""Agent driver — turn a free-text instruction into generation.

The *code* half of the Kiro custom agent (`.kiro/agents/cicd-generator.md`).
Given an instruction like::

 generate for ci_vendor=github target_platform=aws-ecs with region=eu-west-2 project=orders-api

it parses the two axes + overrides, assembles an IR (seeded from stack
discovery, with the explicit instruction winning), validates it, and drives
``cicdgen.core.generate`` — **dry-run by default**, ``apply=True`` to write.

The driver only ever assembles the IR *inputs* and routes all structure through
the core/plugins: it never writes a pipeline/IaC file itself, never hard-codes a
vendor/platform, never inlines a secret, never invents a verb, and never guesses
a missing axis (it returns a needs-confirmation result instead).

Public surface:

 from cicdgen.agent import run_instruction, parse_instruction
 result = run_instruction("generate for ci_vendor=github target_platform=aws-ecs with region=eu-west-2",
 repo_root="/path/to/client/repo",)
 # result.needs_confirmation -> bool; result.plan (dry-run) / result.result (apply)
"""

from __future__ import annotations

from cicdgen.agent.driver import DriverResult, run_instruction
from cicdgen.agent.instruction import ParsedInstruction, parse_instruction

__all__ = [
    "run_instruction",
    "DriverResult",
    "parse_instruction",
    "ParsedInstruction",
]
