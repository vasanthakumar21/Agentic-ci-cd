# `examples/` — per-client IRs

Each client gets its **own self-contained subdirectory**: its own `pipeline.yaml`
(IR) and its own `.cicd/*.local.yaml`. No cross-contamination between clients (R10.3).

Planned demonstration clients (different axis combinations, task 13):

```
examples/
├── client-github-ecs/       # ci_vendor: github       × target_platform: aws-ecs
│   ├── pipeline.yaml
│   └── .cicd/               # *.local.yaml are git-ignored; *.example committed
└── client-azdo-databricks/  # ci_vendor: azure-devops × target_platform: databricks
    ├── pipeline.yaml
    └── .cicd/
```

These prove the two-axis model end to end: the same stage verbs compile to wildly
different deploy shapes (Docker image vs Databricks job).
