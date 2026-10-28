"""Generate every service in this client — reads src/<svc>, writes generated/<svc>.

This is the ONE command a developer runs after editing source. It keeps the clean
separation:

    * SOURCE (hand-authored)   -> src/<service>/     (never written to by the tool)
    * OUTPUT  (tool-generated) -> generated/<service>/

For each service it:
    1. loads src/<svc>/pipeline.yaml, deep-merged onto the shared pipeline.base.yaml
       (auto-discovered up the tree) — so version/ci_vendor/cloud live in ONE place;
    2. runs the generator with
         repo_root    = this client dir  (so source_dir resolves against src/)
         output_root  = generated/<svc>  (so artifacts land out of the source tree)
         settings['source_dir'] = src/<svc>  (where the plugin reads your code)
    3. copies the generated workflow to generated/<svc>/ (already there) AND, for a
       real GitHub deploy, you point your repo's .github/ at the service you want to
       run (see README — GitHub only runs workflows at the repo root).

Run from the repo root:
    python examples/client-sales-pipeline/generate.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the generator importable when run from the repo root.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "generator"))

from cicdgen.core import generate  # noqa: E402
from cicdgen.ir import load_and_validate_ir  # noqa: E402

_CLIENT_DIR = Path(__file__).resolve().parent
_PLUGINS_ROOT = _REPO_ROOT / "plugins"

#: service name -> non-secret settings (source_dir is where the plugin reads code).
_SERVICES: dict[str, dict[str, str]] = {
    "lambda": {
        "source_dir": "src/lambda",
        # MUST match the Terraform-created function name (the IR project name
        # 'sales-file-check'), else update-function-code can't find the function.
        "function_name": "sales-file-check",
        "aws_region": "ap-southeast-2",
    },
    "stepfn": {
        "definition_src": "src/stepfn/asl/state-machine.json",
        # MUST match the Terraform-created name (the plugin appends '-sfn' to the
        # project name), else the deploy verb won't find it and tries to recreate.
        "state_machine_name": "sales-orchestrator-sfn",
        "aws_region": "ap-southeast-2",
    },
}


def main() -> int:
    for svc, settings in _SERVICES.items():
        ir_path = _CLIENT_DIR / "src" / svc / "pipeline.yaml"
        out_root = _CLIENT_DIR / "generated" / svc

        # The IR is merged with the shared base automatically by the loader.
        pipeline = load_and_validate_ir(ir_path, check_plugins=True, repo_root=None)

        result = generate(
            pipeline,
            _CLIENT_DIR,            # SOURCE root: source_dir resolves against this
            plugins_root=_PLUGINS_ROOT,
            settings=settings,
            output_root=out_root,   # OUTPUT root: artifacts land here, not in src/
        )
        rel = out_root.relative_to(_CLIENT_DIR).as_posix()
        print(f"[{svc}] {result.ci_vendor} x {result.target_platform} "
              f"-> {len(result.written)} files under {rel}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
