"""Provider-neutral grouping package for Kubernetes target-platform plugins.

``k8s`` is not a cloud provider — it is the provider-neutral home for the generic
Kubernetes plugin. The legacy ``kubernetes`` alias resolves here via the data-only
:data:`cicdgen.plugins.target_layout.TARGET_ALIASES` table (``"kubernetes" ->
("k8s", "generic")``), freeing ``gcp/gke`` for the first-class native ``gcp-gke``
plugin.

Groups the ``<service>`` plugin packages (``generic`` — the home of the legacy
``kubernetes`` alias) under ``plugins/target/k8s/``. It carries no plugin itself —
each service sub-package *is* a plugin, discovered by the loader via
``resolve_target_dir`` + the data-only alias table. Discovery scans this
provider dir exactly like ``aws``/``azure``/``gcp``.
"""
