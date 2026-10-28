"""Provider grouping package for Azure target-platform plugins.

Groups the Azure ``<service>`` plugin packages (``functions``, ``databricks``,
and future services) under ``plugins/target/azure/``. It carries no plugin
itself — each service sub-package *is* a plugin, discovered by the loader via
``resolve_target_dir`` + the data-only alias table.
"""
