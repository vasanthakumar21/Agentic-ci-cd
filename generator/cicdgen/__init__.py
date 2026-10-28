"""cicdgen — agentic, platform-agnostic CI/CD generator (pipeline-as-data compiler).

Two independent plugin axes:
 * ci_vendor — where the pipeline runs (github, azure-devops, …)
 * target_platform — what gets built and deployed (aws-ecs, databricks, …)

The core validates the IR, resolves defaults, loads the two named plugins, renders
deterministic templates, and writes committed files + a manifest.
"""

__version__ = "0.1.0"
