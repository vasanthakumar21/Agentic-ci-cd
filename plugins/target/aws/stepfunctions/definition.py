"""The Amazon States Language (ASL) definition the stepfunctions ``build`` verb emits.

``deploy`` on aws-stepfunctions is *create/update a state machine via the CLI/API* — there is NO container image and NO ECS task definition. The
``aws stepfunctions create-state-machine`` / ``update-state-machine`` CLI takes an
ASL definition (JSON); this module supplies that JSON as a committed support file so
the ``deploy`` verb has a concrete definition to apply.

Why a committed state-machine.asl.json
---------------------------------------
The ASL definition is the orchestration analogue of a task/job definition: it
describes the states, transitions, and task invocations the state machine runs.
Emitting it as a file (rather than inlining a large JSON string in a shell command)
keeps the ``deploy`` command readable and lets a client review/extend the definition
in version control.

Secrets by reference / determinism
----------------------------------------------
The generated starting definition is a minimal, correct pass-through state machine
with NO literal account id, ARN, or resource owned by a specific account — a client
replaces the states/tasks with their own. The JSON is assembled from a fixed, ordered
template (``sort_keys`` + fixed indent) so regeneration is byte-identical.
"""

from __future__ import annotations

import json


def render_state_machine_definition(*, project_name: str) -> str:
    """Render a minimal, valid ASL state-machine definition as deterministic text.

 Args:
 project_name: the IR project name — used only in the human-readable
 ``Comment`` so the committed definition is self-describing (non-secret).

 The default is a correct single-state ``Pass`` machine a client extends with
 their own ``Task`` states (which reference Lambda/ECS/etc. by ARN at deploy
 time, supplied by reference — never a literal account id here, R6).
 ``json.dumps`` with ``sort_keys`` + a fixed indent gives byte-stable output.
 """
    definition = {
        "Comment": f"State machine for {project_name} (generated starting ASL).",
        "StartAt": "Start",
        "States": {
            "Start": {
                "Type": "Pass",
                "Result": {"status": "ok"},
                "Next": "Done",
            },
            "Done": {
                "Type": "Succeed",
            },
        },
    }
    # sort_keys=True + fixed indent => deterministic, review-friendly JSON.
    return json.dumps(definition, indent=2, sort_keys=True) + "\n"
