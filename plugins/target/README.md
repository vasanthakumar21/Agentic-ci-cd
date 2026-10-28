# `plugins/target/` — target-platform plugins

Each subdirectory is a **target-platform plugin** describing *what* gets built and
deployed. A plugin supplies the implementation of the stable stage verbs
(`lint`, `test`, `build`, `package`, `deploy`, `verify`) plus its backing IaC (R4.2).

The verb meaning is platform-specific — e.g. `build` = `docker build` for `aws-ecs`,
but = assemble notebooks / build wheel for `databricks` (which has no image step).

Planned layout:

```
plugins/target/
├── aws-ecs/          # docker build → ECR → ECS task def/service + TF   (task 8)
├── databricks/       # notebooks/wheel → DBFS/Volumes → job via CLI/API (task 9)
├── azure-functions/  # func package → publish                          (later)
└── kubernetes/       # image → kubectl/helm apply                      (later)
```

Adding a new target platform SHALL NOT require changing the core generator (R3.4).
See the target-platform mapping table in design §8b.
