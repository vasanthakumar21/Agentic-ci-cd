"""Axis-swap migration detection — the "vendor/target changed" signal.

The generator *removes* stale files on any regenerate (see
:mod:`cicdgen.core.generate` — the manifest records every owned file, and a later
run unlinks the paths the previous manifest owned but the new run does not). That
mechanism handles an axis swap in a *general* sense, but it never names the thing
that happened: "you changed ``ci_vendor`` and/or ``target_platform``".

This module classifies that migration as a first-class signal. It compares the
*existing* ``.cicd/generated.manifest.json`` (what the generator last wrote into
this repo, and under which axes) against the *new* run's axes and reports whether
this regenerate is an **axis swap** — a CI-vendor change, a target-platform
change, or both.

It adds **no** side effects: removal of the old vendor/target files is done by
:func:`cicdgen.core.generate.generate` via manifest ownership, which is also the
safety guarantee — only files the manifest OWNS are ever removed, never
client-authored files the generator never wrote. This module only *reads* the
prior manifest and the new axes to classify the run; it is pure and
side-effect-free.

:class:`GenerationPlan` /:class:`GenerationResult` carry an ``axis_change`` field
that defaults to ``None`` (meaning "first run — no prior manifest, nothing to
migrate from").
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cicdgen.core.manifest import Manifest


@dataclass(frozen=True)
class AxisChange:
    """A classified comparison of the prior run's axes vs. the new run's axes.

 ``old_ci_vendor`` / ``old_target_platform`` come from the manifest a previous
 ``generate`` wrote into the repo; ``new_ci_vendor`` / ``new_target_platform``
 are the axes the current run resolves to. The three booleans classify the run:

 *:attr:`ci_changed` — ``ci_vendor`` differs from the prior run.
 *:attr:`target_changed` — ``target_platform`` differs from the prior run.
 *:attr:`is_axis_change` — either axis differs (an actual migration).

 A no-op regenerate (same axes as last time) yields an ``AxisChange`` with all
 three flags ``False``; that is intentionally distinct from "first run", which
 is represented by ``axis_change=None`` on the plan/result.
 """

    old_ci_vendor: str
    old_target_platform: str
    new_ci_vendor: str
    new_target_platform: str

    @property
    def ci_changed(self) -> bool:
        """True when the CI vendor differs from the previous run."""
        return self.old_ci_vendor != self.new_ci_vendor

    @property
    def target_changed(self) -> bool:
        """True when the target platform differs from the previous run."""
        return self.old_target_platform != self.new_target_platform

    @property
    def is_axis_change(self) -> bool:
        """True when either axis changed — i.e. this regenerate is a migration."""
        return self.ci_changed or self.target_changed

    def summary(self) -> str:
        """A short, value-free, human-readable description of the swap.

 Used by the migration runbook and surfaced in logs. It names only the
 axes (vendor/platform identifiers), never any secret.
 """
        if not self.is_axis_change:
            return (
                f"no axis change (ci_vendor={self.new_ci_vendor}, "
                f"target_platform={self.new_target_platform})"
            )
        parts: list[str] = []
        if self.ci_changed:
            parts.append(f"ci_vendor: {self.old_ci_vendor} -> {self.new_ci_vendor}")
        if self.target_changed:
            parts.append(
                f"target_platform: {self.old_target_platform} -> {self.new_target_platform}"
            )
        return "axis change — " + "; ".join(parts)


def detect_axis_change(
    prev_manifest: Manifest | None,
    *,
    new_ci_vendor: str,
    new_target_platform: str,
) -> AxisChange | None:
    """Classify a regenerate against the manifest a prior run left behind.

 Compare the existing manifest's recorded ``ci_vendor`` / ``target_platform``
 to the new run's axes and return an :class:`AxisChange` describing the
 comparison.

 Args:
 prev_manifest: the manifest a previous ``generate`` wrote into the repo,
 or ``None`` when there is no prior manifest (a first run).
 new_ci_vendor: the CI vendor this run resolves to.
 new_target_platform: the target platform this run resolves to.

 Returns:
 ``None`` when ``prev_manifest`` is ``None`` (first run — there is nothing
 to migrate *from*), otherwise an :class:`AxisChange`. Note an
:class:`AxisChange` with :attr:`~AxisChange.is_axis_change` ``False`` means
 a regenerate with the *same* axes — distinct from a first run.
 """
    if prev_manifest is None:
        return None
    return AxisChange(
        old_ci_vendor=prev_manifest.ci_vendor,
        old_target_platform=prev_manifest.target_platform,
        new_ci_vendor=new_ci_vendor,
        new_target_platform=new_target_platform,
    )


def detect_axis_change_for_repo(
    repo_root: Path | str,
    *,
    new_ci_vendor: str,
    new_target_platform: str,
) -> AxisChange | None:
    """Convenience wrapper: load the repo's manifest, then classify the run.

 Reads ``.cicd/generated.manifest.json`` under ``repo_root`` (returning
 ``None`` when there is none) and delegates to :func:`detect_axis_change`. Pure
 apart from the one manifest read; removes nothing — removal remains the job of
:func:`cicdgen.core.generate.generate` via manifest ownership.
 """
    # Imported here to avoid a circular import (generate imports this module).
    from cicdgen.core.generate import load_manifest

    prev = load_manifest(repo_root)
    return detect_axis_change(
        prev,
        new_ci_vendor=new_ci_vendor,
        new_target_platform=new_target_platform,
    )
