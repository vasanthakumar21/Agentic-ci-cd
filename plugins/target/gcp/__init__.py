"""Provider grouping package for GCP target-platform plugins.

Groups the GCP ``<service>`` plugin packages — ``cloud-run`` (the native
``gcp-cloud-run`` plugin) and ``gke`` (the native ``gcp-gke`` plugin) — under
``plugins/target/gcp/``. Both are first-class ``<provider>-<service>`` services
resolved by the ``gcp-`` prefix rule; neither is the legacy generic ``kubernetes``
plugin, which now lives provider-neutrally under ``plugins/target/k8s/generic/``. This package carries no plugin itself — each service
sub-package *is* a plugin, discovered by the loader via ``resolve_target_dir`` +
the data-only alias table.
"""
