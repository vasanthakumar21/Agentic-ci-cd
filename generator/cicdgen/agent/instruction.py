"""Parse a natural-language-ish generation instruction into structured data.

The agent accepts an instruction phrased like::

 generate for ci_vendor=github target_platform=aws-ecs with region=eu-west-2 project=orders-api

and turns it into a :class:`ParsedInstruction`: the two axes (when stated) plus
an ``overrides`` dict of ``key=value`` pairs. The parser is deliberately small
and robust — it extracts ``key=value`` tokens anywhere in the string and treats
``ci_vendor`` / ``target_platform`` as the two special axis keys. Everything
else is an override the driver will fold into the IR (``region``, ``project``,
``language``) or forward to the plugins as non-secret ``settings``.

This module parses only. It never validates against the IR enums (that is the
driver +:mod:`cicdgen.ir` job), never touches discovery, and never writes
anything. Keeping parsing pure makes it trivially testable and keeps the
"assemble the IR, go through the plugins" logic in :mod:`cicdgen.agent.driver`.

The parser extracts *inputs* only; it invents nothing. An instruction that names
no axis yields ``ci_vendor=None`` / ``target_platform=None`` so the driver can
gap-fill from discovery or flag for confirmation rather than guessing silently.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# The two axis keys the IR names independently (everything else is an override).
CI_VENDOR_KEY = "ci_vendor"
TARGET_PLATFORM_KEY = "target_platform"

# A key=value token: a bare identifier key, '=', then a value that runs until the
# next whitespace. Values may be quoted to allow spaces (e.g. project="my app").
_PAIR_RE = re.compile(
    r"""(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)\s*=\s*(?P<value>"[^"]*"|'[^']*'|\S+)"""
)

# Keys that are routed into dedicated IR locations rather than left as free
# settings. The driver knows how to place these; the rest become verb settings.
_KNOWN_OVERRIDE_KEYS = frozenset({"region", "project", "language"})


@dataclass(frozen=True)
class ParsedInstruction:
    """The structured form of a free-text generation instruction.

 Attributes:
 raw: the original instruction string (kept for diagnostics / echo).
 ci_vendor: the explicitly-named CI vendor, or ``None`` if absent.
 target_platform: the explicitly-named target platform, or ``None``.
 overrides: every other ``key=value`` pair, in first-seen order. Includes
 both the "known" keys (``region`` / ``project`` / ``language``) and
 any extra knobs the driver forwards to the plugins as ``settings``.
 """

    raw: str
    ci_vendor: str | None
    target_platform: str | None
    overrides: dict[str, str] = field(default_factory=dict)

    @property
    def extra_settings(self) -> dict[str, str]:
        """Overrides that are NOT the IR-placed known keys → plugin settings.

 These are the repo-specific, non-secret knobs forwarded into every verb
 context — gap-fill the inputs, never the machinery.
 """
        return {k: v for k, v in self.overrides.items() if k not in _KNOWN_OVERRIDE_KEYS}

    def missing_axes(self) -> tuple[str, ...]:
        """Which of the two axes the instruction did not name (in axis order)."""
        missing: list[str] = []
        if self.ci_vendor is None:
            missing.append(CI_VENDOR_KEY)
        if self.target_platform is None:
            missing.append(TARGET_PLATFORM_KEY)
        return tuple(missing)


def _unquote(value: str) -> str:
    """Strip a single matching pair of surrounding quotes, if present."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_instruction(text: str) -> ParsedInstruction:
    """Parse a free-text generation instruction into a :class:`ParsedInstruction`.

 Extracts every ``key=value`` token anywhere in ``text`` (quoted values may
 contain spaces). ``ci_vendor`` and ``target_platform`` are pulled out as the
 two axes; all other pairs land in ``overrides`` (first value wins on repeats).
 Keys are lower-cased so ``CI_VENDOR=GitHub`` and ``ci_vendor=github`` are
 equivalent; values keep their case except axis values, which are lower-cased
 to match the IR enums.

 The leading ``generate for`` / ``with`` phrasing is purely cosmetic — the
 parser keys off the ``key=value`` pairs, so any ordering or wording works.

 Args:
 text: the instruction, e.g.
 ``"generate for ci_vendor=github target_platform=aws-ecs with region=eu-west-2"``.

 Returns:
 A :class:`ParsedInstruction`. Absent axes are ``None`` (never guessed).

 Raises:
 ValueError: if ``text`` is empty / whitespace-only.
 """
    if text is None or not text.strip():
        raise ValueError("instruction is empty — expected something like "
                         "'generate for ci_vendor=github target_platform=aws-ecs with region=...'.")

    ci_vendor: str | None = None
    target_platform: str | None = None
    overrides: dict[str, str] = {}

    for match in _PAIR_RE.finditer(text):
        key = match.group("key").lower()
        value = _unquote(match.group("value"))
        if key == CI_VENDOR_KEY:
            if ci_vendor is None:
                ci_vendor = value.lower()
        elif key == TARGET_PLATFORM_KEY:
            if target_platform is None:
                target_platform = value.lower()
        elif key not in overrides:
            overrides[key] = value

    return ParsedInstruction(
        raw=text,
        ci_vendor=ci_vendor,
        target_platform=target_platform,
        overrides=overrides,
    )
