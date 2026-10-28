"""The Dataflow template metadata the gcp-dataflow ``build`` verb emits.

``deploy`` on gcp-dataflow is *launch a Dataflow job from a staged template via the
CLI* — there is NO required container image. The Beam/Dataflow
templating flow accepts a template metadata JSON describing the template and its
parameters; this module supplies that JSON as a committed support file so the
``build``/``deploy`` verbs have a concrete, reviewable spec.

Why a committed dataflow-template.json
--------------------------------------
Dataflow launches jobs from a *staged template* (a classic template, or a flex
template spec). The metadata file is the Dataflow analogue of the aws-glue job
definition: it names the template, its GCS template path, and its parameters.
Emitting it as a file (rather than inlining a large JSON string in a shell command)
keeps the ``deploy`` command readable and version-controllable. We prefer the
staged-template path to stay no-image; a flex-template image, when used, is only a
by-reference knob the client supplies.

Secrets by reference / determinism
----------------------------------------------
The template GCS path, the temp/staging locations and the job name are
``settings``-resolved values or ``$REF`` placeholders the CI step substitutes —
never a literal GCP project id or bucket owned by a specific account. The JSON is
assembled from a fixed, ordered template (``sort_keys`` + fixed indent) so
regeneration is byte-identical.
"""

from __future__ import annotations

import json


def render_template_metadata(
    *,
    job_name: str,
    template_path: str,
    temp_location: str,
    staging_location: str,
) -> str:
    """Render the Dataflow template metadata as deterministic text.

 Args:
 job_name: the Dataflow job name ``deploy`` launches (value or ``$REF``).
 template_path: GCS URI of the staged template the job runs from (value or
 ``$REF``) — never a literal project-owned path in the committed template.
 temp_location: GCS temp location the job uses (value or ``$REF``).
 staging_location: GCS staging location for the template/artifacts (value or
 ``$REF``).

 The metadata describes a job launched from a *staged template* — NO image
 required. ``json.dumps`` with ``sort_keys`` + a fixed indent gives byte-stable
 output.
 """
    spec = {
        "jobName": job_name,
        "templatePath": template_path,
        "environment": {
            "tempLocation": temp_location,
            "stagingLocation": staging_location,
        },
        "parameters": {},
    }
    # sort_keys=True + fixed indent => deterministic, review-friendly JSON.
    return json.dumps(spec, indent=2, sort_keys=True) + "\n"
