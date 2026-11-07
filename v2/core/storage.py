from django.contrib.staticfiles.storage import ManifestStaticFilesStorage


class ModuleManifestStaticFilesStorage(ManifestStaticFilesStorage):
    """Rewrites `import "./api.js"` to the hashed name too, so a cached page never mixes releases (design 7.1)."""

    support_js_module_import_aggregation = True
