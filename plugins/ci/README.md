# `plugins/ci/` — CI-vendor plugins

Each subdirectory is a **CI-vendor plugin** describing *where* the pipeline runs.
A plugin implements a fixed interface: emit that vendor's pipeline files for a given
stage graph (order, gates, triggers) — orchestration only, no platform business
logic (R4.3).

Planned layout:

```
plugins/ci/
├── github/         # .github/workflows/*.yml   (task 6)
├── azure-devops/   # azure-pipelines.yml        (task 7)
├── gitlab/         # .gitlab-ci.yml             (later — task 12)
├── bitbucket/      # bitbucket-pipelines.yml    (later — task 12)
└── jenkins/        # Jenkinsfile                (later — task 12)
```

Adding a new CI vendor SHALL NOT require changing the core generator (R3.4).
See the CI-vendor mapping table in design §8a.
