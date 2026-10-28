"""Command-line entry point for cicdgen.

The ``cicd doctor`` subcommand is a READ-ONLY drift check.

``cicd doctor``
---------------
Loads the IR + committed manifest for a repo, re-runs generation IN MEMORY to see
what WOULD be produced now, and prints a value-free report grouped by category
(missing / extra / changed / in-sync). It exits NON-ZERO on any drift so CI can run
it as a guard that generated files are current. There is deliberately NO ``--apply``
— ``doctor`` is read-only by construction and writes nothing.

``cicd doctor --live``
----------------------
Optionally also queries vendor/cloud wiring in read-only mode through the existing
bootstrap executor seam. Live access is not wired into the CLI by default, so this
reports what it could NOT check rather than failing hard — it never mutates anything.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cicdgen.core.doctor import (
    DoctorError,
    check_drift,
    check_live_wiring,
    render_drift_report,
    render_live_report,
)

#: Repo-relative default IR location (matches the examples/ convention).
DEFAULT_IR_RELPATH = "pipeline.yaml"


def _cmd_doctor(args: argparse.Namespace) -> int:
    """Run the read-only drift check; exit non-zero on any drift."""
    repo_root = Path(args.repo_root).resolve()
    ir_path = Path(args.ir) if args.ir else repo_root / DEFAULT_IR_RELPATH

    if not ir_path.is_file():
        print(
            f"cicd doctor: IR not found at {ir_path}. "
            f"Pass --ir <path> or place a {DEFAULT_IR_RELPATH} at the repo root.",
            file=sys.stderr,
        )
        return 2

    try:
        report = check_drift(ir_path, repo_root)
    except DoctorError as exc:
        print(f"cicd doctor: {exc}", file=sys.stderr)
        return 2

    print(render_drift_report(report, show_in_sync=args.show_in_sync))

    # The live compare is read-only and never changes the exit decision below;
    # it only surfaces what live wiring exists (or could not be checked).
    if args.live:
        # No live backend is wired into the CLI yet, so pass executor=None: the
        # check degrades gracefully, reporting every probe as UNCHECKABLE rather
        # than failing hard. A caller embedding cicdgen can inject a real
        # read-only executor via cicdgen.core.doctor.check_live_wiring directly.
        live = check_live_wiring(
            executor=None,
            repo_slug=f"{report.ci_vendor}/{report.target_platform}",
        )
        print()
        print(render_live_report(live))

    # Exit non-zero on any drift so `cicd doctor` works as a CI guard.
    return 1 if report.has_drift else 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cicd", description="cicdgen command-line interface.")
    sub = parser.add_subparsers(dest="command")

    doctor = sub.add_parser(
        "doctor",
        help="read-only drift check: compare committed generated files to a fresh render.",
        description=(
            "Compare the committed generated artifacts + manifest against what the "
            "generator WOULD produce now, and report drift. Read-only — writes nothing, "
            "and has no --apply. Exits non-zero on any drift."
        ),
    )
    doctor.add_argument(
        "repo_root",
        nargs="?",
        default=".",
        help="repo to check (defaults to the current directory).",
    )
    doctor.add_argument(
        "--ir",
        default=None,
        help=f"path to the IR (defaults to <repo>/{DEFAULT_IR_RELPATH}).",
    )
    doctor.add_argument(
        "--live",
        action="store_true",
        help="also query vendor/cloud wiring in READ-ONLY mode (writes nothing).",
    )
    doctor.add_argument(
        "--show-in-sync",
        action="store_true",
        help="also list the in-sync paths (otherwise only their count is shown).",
    )
    doctor.set_defaults(func=_cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print("cicdgen — scaffold. Subcommands land in later tasks.")
        return 0

    parser = _build_parser()
    # Only dispatch commands that are implemented; everything else falls through
    # to a placeholder message so unrelated callers are undisturbed.
    if args[0] == "doctor":
        ns = parser.parse_args(args)
        return int(ns.func(ns))

    print(f"cicdgen: not yet implemented ({' '.join(args)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
