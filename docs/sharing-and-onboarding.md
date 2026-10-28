# Sharing this tool with your team

This guide is for a developer using the generator for the first time to deploy
their own service. The tool turns a short declarative config into a complete
CI/CD pipeline plus the backing cloud infrastructure.

## 1. One-time machine setup (per developer)

Install the four tools the generator drives, then authenticate:

- **git**, **AWS CLI**, **Terraform**, **GitHub CLI (`gh`)**
- **Python 3.11+** (to run the generator)

Authenticate once:
```
aws configure          # enter your AWS access key, secret, region, output=json
gh auth login          # GitHub.com -> HTTPS -> login with a web browser
```
Verify:
```
aws sts get-caller-identity
gh auth status
```
Your AWS keys stay on your machine. They are never committed or shared.

## 2. Install the generator

From the repo root:
```
uv sync                # or: pip install -e ".[dev]"
pytest                 # optional: confirm the suite is green
```

## 3. Create your service from the template

Copy the worked example as your starting point:
```
cp -r examples/client-sales-pipeline  <your-client-name>
```

Each service lives in its own folder under `src/<service>/`:

```
<your-client>/
├── pipeline.base.yaml        # shared: ci_vendor, cloud, region, auth, branch strategy
├── .cicd/secrets.local.yaml  # real account values (git-ignored)
├── src/
│   └── <service>/
│       ├── pipeline.yaml      # this service's target_platform + stages
│       └── <your code>        # handler.py, glue_jobs/, asl/state-machine.json, ...
```

Edit:
- `pipeline.base.yaml` — set `ci_vendor`, `cloud.provider`, `region`, and the
  `auth` mode (see `docs/auth-modes.md`), and your branch strategy under
  `triggers` (e.g. `push_branches: ["main", "<KEY>-*"]`).
- `src/<service>/pipeline.yaml` — set `target_platform`, `project.name`, `stages`.
- Drop your code at the path the plugin reads:
  - Lambda: `src/<service>/handler.py` (+ `requirements.txt`)
  - Glue: `src/<service>/glue_jobs/*.py`
  - Step Functions: `src/<service>/asl/state-machine.json`

## 4. Generate

```
python <your-client>/generate.py
```
This reads `src/<service>/` and writes everything the tool produces into
`generated/<service>/` (CI workflow, Terraform, manifest). You never hand-edit
`generated/`.

## 5. Deploy

```
python <your-client>/deploy.py --dry-run   # review the plan, touches nothing
python <your-client>/deploy.py             # confirm each step; real deploy
```
`deploy.py` runs Terraform (creates the cloud resources once), creates the
GitHub repo, sets the required secrets, and pushes. Edit the knobs at the top of
`deploy.py` for your repo/owner/region.

## 6. Daily use

After the first deploy, a code change is just:
```
# edit src/<service>/<your code>
git commit -am "<KEY>-123: fix handler"
git push
```
The pipeline runs automatically: test -> build -> deploy -> verify. No Terraform
needed for a code change — Terraform only runs again when the infrastructure
itself changes.

## What the tool does vs. what you do

| You | The tool |
|---|---|
| Write app code + a short `pipeline.yaml` | Generates the full CI/CD pipeline + Terraform |
| `aws configure`, `gh auth login` once | Everything between: repo layout, terraform, secrets, push |
| Create a `<KEY>-123` branch, push | Runs test -> build -> deploy -> verify, keeps secrets by reference |

## Branch-per-ticket

Set your tracker prefix in `pipeline.base.yaml`:
```yaml
triggers:
  push_branches: ["main", "PROJ-*", "feature/**"]
  pr_branches: ["main"]
```
Every ticket branch then runs its own pipeline, so changes are monitored per
ticket. Filter GitHub Actions by branch to see one ticket's runs.

## Honest boundaries

- The tool deploys your code and provisions infrastructure; it does not write
  your application logic (your Lambda handler, Glue script, or state-machine
  graph are yours).
- Each service deploys as its own pipeline; one push can run several.
- The first deploy needs a one-time Terraform apply with admin credentials to
  create IAM/roles.
- Pipelines run on CI runners against a real cloud account; they are not run on
  your laptop.
