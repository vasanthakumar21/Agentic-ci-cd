# `templates/` — deterministic render templates

Per-vendor and per-platform templates, parameterized by the IR (R5.1). Generation is
deterministic: the same IR + repo context produces the same output, so diffs are
reviewable (R5.3).

The agent fills repo-specific gaps and makes judgement calls but SHALL NOT bypass
these templates for core structure (R5.2). Templates contain **key references** to
secrets (e.g. `${{ secrets.X }}`, `$X`), never secret values (R6.3).
