"""Provider grouping package for AWS target-platform plugins.

This directory groups the AWS ``<service>`` plugin packages (``ecs``, and future
services) under ``plugins/target/aws/``. It carries no plugin itself — each
service sub-package *is* a plugin, discovered by the loader via
``resolve_target_dir`` + the data-only alias table.
"""
