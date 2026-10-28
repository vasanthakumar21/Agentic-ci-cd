"""Secrets-by-reference enforcement for generated output.

The core never injects secret *values*; plugins emit secret *references* only
(``${{ secrets.X }}`` on GitHub, ``$X`` / ``$(X)`` elsewhere). This module is the
authoritative guard that proves that promise about the files about to be written,
and refuses to write anything that would leak a literal credential.

Two layers, strongest first
---------------------------
1. **Exact-value check (strongest).** When the caller can supply the ACTUAL
 secret ``name -> value`` map (from a loaded :class:`~cicdgen.localconfig.LocalConfig`
 /:class:`~cicdgen.localconfig.models.SecretStore`), every generated file is
 scanned for each real value appearing verbatim. This catches leaks no regex
 could — the real credential, byte-for-byte. Values shorter than
:data:`MIN_VALUE_LEN` are skipped to avoid matching coincidental short strings
 (e.g. a region ``"eu"`` or a port). A detected leak names the secret KEY and
 the file PATH only — **never the value**.

2. **Heuristic fallback.** When actual values are not available, fall back to a
 conservative pattern scan for obvious literal-credential shapes (AWS keys,
 private-key blocks, ``password = "…"`` assignments). By-reference forms are
 explicitly allowed because they carry no value.

Positive contract (best-effort, never raises)
----------------------------------------------
:func:`references_are_plausible` reports, per secret name, whether the generated
files reference it *by key* at all. It is advisory only (a plugin may legitimately
not use every catalogued secret), so it never raises and is not part of the
enforcement gate.

Hard rule: no secret value is ever logged, echoed, repr'd, str'd, or placed into
an exception message. Every message here names KEYS and PATHS only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from cicdgen.plugins.base import GeneratedFile

# Secret values shorter than this are not scanned for verbatim leaks: very short
# strings produce false positives (a region code, a port, a one-word setting) and
# are not meaningfully "secret" to leak-scan on. Tunable; 6 is deliberately low.
MIN_VALUE_LEN = 6


class SecretLeak(RuntimeError):
    """Raised when generated content would leak a secret.

 The message names the offending secret KEY and file PATH only — never the
 secret value.
 """


# ---------------------------------------------------------------------------
# By-reference recognition (shared by both layers)
# ---------------------------------------------------------------------------
# The legitimate forms a plugin emits. These carry NO value and must never be
# flagged: ``${{ secrets.X }}`` (GitHub), ``$X`` / ``${X}`` (GitLab / shell),
# ``$(X)`` (Azure DevOps). Used to allow an otherwise-suspicious regex match that
# is actually a reference expression.
def _looks_like_reference(snippet: str) -> bool:
    """True if ``snippet`` is a secret *reference* expression (no value)."""
    return "${{" in snippet or "$(" in snippet or snippet.startswith("$")


# ---------------------------------------------------------------------------
# Layer 2 — heuristic pattern guard
# ---------------------------------------------------------------------------
# Deliberately conservative: flag obvious *literal-value* leaks, NOT the
# by-reference forms. Either a well-known cloud-credential/private-key shape, or
# an assignment of a long opaque value to a secret-ish key.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # AWS access key id / secret access key literal formats.
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"aws_secret_access_key\s*=\s*['\"]?[A-Za-z0-9/+=]{40}"),
    # Private key blocks.
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    # key = "long-opaque-value" where the key name screams secret and the value
    # is NOT a by-reference expression.
    re.compile(
        r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key)"
        r"\s*[:=]\s*['\"]([^'\"$}\s]{12,})['\"]"
    ),
)


def _scan_for_secret_values(f: GeneratedFile) -> str | None:
    """Return a human message if ``f`` looks like it leaks a secret *value*.

 By-reference forms (``${{ secrets.X }}``, ``$X``, ``$(X)``) are never flagged
 because they carry no value. Returns ``None`` when the file looks clean.
 """
    for pat in _SECRET_VALUE_PATTERNS:
        m = pat.search(f.content)
        if m is None:
            continue
        # Allow a match that is actually a by-reference expression.
        if _looks_like_reference(m.group(0)):
            continue
        return (
            f"generated file '{f.path}' appears to contain a literal secret value "
            f"(matched pattern {pat.pattern!r}); plugins must emit secret references "
            f"only (R6.3/R6.6)."
        )
    return None


# ---------------------------------------------------------------------------
# Layer 1 — exact-value check (strongest)
# ---------------------------------------------------------------------------
def _scan_for_exact_value(f: GeneratedFile, name: str, value: str) -> str | None:
    """Return a value-free message if the real secret ``value`` appears in ``f``.

 Skips values shorter than :data:`MIN_VALUE_LEN`. The message names the secret
 KEY and the file PATH only — never the value.
 """
    if len(value) < MIN_VALUE_LEN:
        return None
    if value in f.content:
        return (
            f"generated file '{f.path}' contains the literal value of secret "
            f"'{name}'; generated files must reference secrets by key only, never "
            f"inline the value (R6.3/R6.6)."
        )
    return None


# ---------------------------------------------------------------------------
# Positive contract (advisory, never raises)
# ---------------------------------------------------------------------------
def references_are_plausible(
    files: Iterable[GeneratedFile],
    secret_names: Iterable[str],
) -> dict[str, bool]:
    """Report, per secret name, whether the files reference it *by key*.

 Best-effort and advisory only — a plugin may legitimately not use every
 catalogued secret, so this NEVER raises and is not part of the enforcement
 gate. A name is considered "referenced" if it appears anywhere in any file's
 content (its key, not a value — values live only in the local config).

 Args:
 files: the generated files about to be written.
 secret_names: the catalogued secret KEYS (e.g. from ``secret_names``).

 Returns:
 ``{name: True|False}`` — True when the key is referenced by at least one
 file. Keys only; never touches values.
 """
    blob = "\n".join(f.content for f in files)
    return {name: name in blob for name in secret_names}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def enforce_secrets_by_reference(
    files: Iterable[GeneratedFile],
    *,
    secret_values: Mapping[str, str] | None = None,
    heuristic: bool = True,
) -> None:
    """Refuse to proceed if any generated file leaks a secret value.

 Runs the strongest available check first:

 1. **Exact-value check** (when ``secret_values`` is provided): every real
 value is scanned for verbatim across all files. Values shorter than
:data:`MIN_VALUE_LEN` are skipped.
 2. **Heuristic fallback** (when ``heuristic`` is True): the conservative
 pattern scan for obvious literal-credential shapes.

 Both layers allow the legitimate by-reference forms. The first leak found
 raises :class:`SecretLeak` with a value-free message naming the secret KEY
 and the file PATH only.

 Args:
 files: the generated files about to be written.
 secret_values: optional ACTUAL ``name -> value`` map (e.g. materialised
 from a loaded ``LocalConfig``/``SecretStore``). When omitted, only the
 heuristic layer runs.
 heuristic: run the heuristic pattern guard. Defaults to True; set False to
 opt out of the lightweight net (the exact check, if values were given,
 still runs).

 Raises:
 SecretLeak: on the first detected leak — message names KEY + PATH only.
 """
    materialised = tuple(files)

    # Layer 1 — exact value match (strongest). Deterministic iteration order:
    # files by path, then secret names sorted, so the first-leak message is stable.
    if secret_values:
        for f in sorted(materialised, key=lambda g: g.path):
            for name in sorted(secret_values):
                problem = _scan_for_exact_value(f, name, secret_values[name])
                if problem is not None:
                    raise SecretLeak(problem)

    # Layer 2 — heuristic fallback.
    if heuristic:
        for f in sorted(materialised, key=lambda g: g.path):
            problem = _scan_for_secret_values(f)
            if problem is not None:
                raise SecretLeak(problem)
