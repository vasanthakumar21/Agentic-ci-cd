"""A tiny, deterministic YAML emitter for GitHub Actions workflows.

Why not just ``yaml.safe_dump``?
--------------------------------
Two reasons, both about *determinism* and *correctness* for the GitHub Actions
dialect:

1. **Byte-stable output.** The core requires byte-identical regeneration.
 A general dumper's defaults (key sorting, flow vs block style, line wrapping,
 quoting heuristics) can shift between library versions. We instead walk the
 structure in *insertion order* with one fixed style, so the same input always
 produces the same bytes.

2. **The ``on`` key.** GitHub workflows use the top-level key ``on:``. Under the
 YAML 1.1 rules PyYAML follows, a bare ``on`` is the boolean ``true``. A round
 trip through a naive dumper can turn ``on:`` into ``true:``. We always quote
 keys that YAML 1.1 would read as booleans/null, so ``on`` stays ``"on"`` and
 parses back to the string ``on`` — which GitHub accepts.

The emitter is intentionally minimal: it supports the node types this plugin
actually produces — ``dict`` (block mapping, insertion order preserved), ``list``
(block sequence), ``str`` / ``int`` / ``bool`` scalars, and a :class:`Comment`
marker (emitted as a ``# …`` line). That is all a workflow needs.

Everything is pure and side-effect free, so the same structure always renders to
the same string.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: A value the emitter knows how to render.
YamlValue = object

_INDENT = "  "

#: Tokens YAML 1.1 (PyYAML) would interpret as a bool/null rather than a string.
#: Keys/scalars equal to one of these (case-insensitively) are quoted so they
#: round-trip as strings — most importantly ``on`` (GitHub's trigger key).
_RESERVED_WORDS = frozenset(
    {
        "y", "n", "yes", "no", "true", "false", "on", "off", "null", "none", "~",
    }
)


@dataclass(frozen=True)
class Comment:
    """A standalone ``# comment`` line to emit inside a mapping.

 The owning mapping stores it under the reserved key ``__comment__``; the
 emitter renders just the ``# text`` line and skips the key itself. Used to
 record honest limitations (e.g. GitHub's lack of native job retry) in-band.
 """

    text: str


def dump_mapping(mapping: Mapping[str, YamlValue]) -> str:
    """Render a top-level mapping to a YAML document string (trailing newline)."""
    lines: list[str] = []
    _emit_mapping(mapping, 0, lines)
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _needs_quoting(text: str) -> bool:
    """True if a scalar string must be quoted to survive a YAML round-trip."""
    if text == "":
        return True
    if text.casefold() in _RESERVED_WORDS:
        return True
    # Leading/trailing space, or characters that start a different YAML construct.
    if text != text.strip():
        return True
    if text[0] in "!&*?|>%@`\"'#,[]{}":
        return True
    # A bare value that looks like a number should stay a string if we meant a
    # string; but all our numeric values are passed as int/bool, so any str that
    # parses as a number here is intentional text -> quote it.
    if text.lstrip("-+").isdigit():
        return True
    # Colon followed by space (or trailing colon) would start a nested mapping.
    if ": " in text or text.endswith(":"):
        return True
    return False


def _scalar(value: YamlValue) -> str:
    """Render a scalar (str/int/bool) deterministically."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        if _needs_quoting(value):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return value
    raise TypeError(f"unsupported scalar type for YAML emit: {type(value).__name__}")


def _is_scalar(value: YamlValue) -> bool:
    return isinstance(value, (str, int, bool))


def _is_multiline(value: YamlValue) -> bool:
    """True for a multi-line string, which renders as a ``|`` block scalar.

 Multi-line run commands (a target plugin's verb steps joined with newlines)
 must be emitted as a YAML literal block (``key: |``) rather than a quoted
 scalar with ``\\n`` escapes, so the generated pipeline is readable and each
 line is a real shell line. Deterministic: the block is the lines verbatim,
 indented one level under the key.
 """
    return isinstance(value, str) and "\n" in value


def _emit_block_scalar(prefix: str, key: str, text: str, indent: int, lines: list[str]) -> None:
    """Emit ``key: |`` followed by the text's lines, each indented one deeper.

 ``prefix`` is the already-computed pad+marker for the key line; ``indent`` is
 the key's indent level, so the block body sits at ``indent + 1``. A literal
 block (``|``) preserves newlines exactly, which is what shell steps need.
 """
    lines.append(f"{prefix}{_emit_key(key)}: |")
    body_pad = _INDENT * (indent + 1)
    for line in text.split("\n"):
        # Preserve blank lines as truly blank (no trailing pad) for clean output.
        lines.append(f"{body_pad}{line}" if line else "")


def _emit_key(key: str) -> str:
    """Render a mapping key, quoting reserved words like ``on``."""
    if _needs_quoting(key):
        escaped = key.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return key


def _emit_mapping(mapping: Mapping[str, YamlValue], indent: int, lines: list[str]) -> None:
    pad = _INDENT * indent
    for key, value in mapping.items():
        if isinstance(value, Comment) or key == "__comment__":
            comment = value if isinstance(value, Comment) else None
            if comment is not None:
                lines.append(f"{pad}# {comment.text}")
            continue

        if _is_multiline(value):
            assert isinstance(value, str)
            _emit_block_scalar(pad, key, value, indent, lines)
        elif _is_scalar(value):
            lines.append(f"{pad}{_emit_key(key)}: {_scalar(value)}")
        elif isinstance(value, Mapping):
            if len(value) == 0:
                lines.append(f"{pad}{_emit_key(key)}: {{}}")
            else:
                lines.append(f"{pad}{_emit_key(key)}:")
                _emit_mapping(value, indent + 1, lines)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) == 0:
                lines.append(f"{pad}{_emit_key(key)}: []")
            else:
                lines.append(f"{pad}{_emit_key(key)}:")
                _emit_sequence(value, indent, lines)
        else:
            raise TypeError(f"unsupported value type for key {key!r}: {type(value).__name__}")


def _emit_sequence(seq: Sequence[YamlValue], indent: int, lines: list[str]) -> None:
    pad = _INDENT * indent
    for item in seq:
        if _is_scalar(item):
            lines.append(f"{pad}- {_scalar(item)}")
        elif isinstance(item, Mapping):
            _emit_sequence_mapping(item, indent, lines)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            # Nested sequence under a sequence (rare for workflows) — emit inline
            # marker then the items one indent deeper.
            lines.append(f"{pad}-")
            _emit_sequence(item, indent + 1, lines)
        else:
            raise TypeError(f"unsupported sequence item type: {type(item).__name__}")


def _emit_sequence_mapping(mapping: Mapping[str, YamlValue], indent: int, lines: list[str]) -> None:
    """Emit a mapping that is an item of a sequence (the ``- key: value`` form)."""
    pad = _INDENT * indent
    first = True
    # Flatten comment-only handling the same way as _emit_mapping.
    items = [
        (k, v)
        for k, v in mapping.items()
        if not (isinstance(v, Comment) or k == "__comment__")
    ]
    if not items:
        lines.append(f"{pad}- {{}}")
        return

    for key, value in items:
        marker = "- " if first else "  "
        prefix = f"{pad}{marker}"
        if _is_multiline(value):
            assert isinstance(value, str)
            # The block body indents relative to the key column, which for a
            # sequence-item mapping is one level deeper than the sequence pad.
            _emit_block_scalar(prefix, key, value, indent + 1, lines)
        elif _is_scalar(value):
            lines.append(f"{prefix}{_emit_key(key)}: {_scalar(value)}")
        elif isinstance(value, Mapping):
            if len(value) == 0:
                lines.append(f"{prefix}{_emit_key(key)}: {{}}")
            else:
                lines.append(f"{prefix}{_emit_key(key)}:")
                _emit_mapping(value, indent + 2, lines)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            if len(value) == 0:
                lines.append(f"{prefix}{_emit_key(key)}: []")
            else:
                lines.append(f"{prefix}{_emit_key(key)}:")
                _emit_sequence(value, indent + 1, lines)
        else:
            raise TypeError(f"unsupported value type for key {key!r}: {type(value).__name__}")
        first = False
