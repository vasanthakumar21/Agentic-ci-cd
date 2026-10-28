# `generator/` — generator core (Python)

Vendor- and platform-agnostic core. The importable package lives at
`generator/cicdgen/`. Responsibilities (design §5):

1. Validate the IR against the JSON Schema → fail fast (task 2).
2. Resolve defaults for omitted knobs.
3. Load the two named plugins (`ci_vendor`, `target_platform`) — no hard-coded
   vendor/platform knowledge in the core (R3.4, task 3).
4. Render templates + agent gap-fill (task 5).
5. Write files + update `.cicd/generated.manifest.json` (tracks what it owns, R8.3).

Mirrors the tekflow-ingest fail-fast Pydantic approach. Tests live in `../tests/`.
