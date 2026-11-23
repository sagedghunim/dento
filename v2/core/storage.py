from django.contrib.staticfiles.storage import ManifestStaticFilesStorage, StaticFilesStorage


class ModuleManifestStaticFilesStorage(ManifestStaticFilesStorage):
    """Rewrites `import "./api.js"` to the hashed name too, so a cached page never mixes releases (design 7.1)."""

    support_js_module_import_aggregation = True


class DevelopmentStaticFilesStorage(StaticFilesStorage):
    """Development (DEBUG): each file's URL carries its modification time (Saged, 2026-11-23), so a browser that kept
    an old copy of a page's script loads the new one after a change, even without a hard reload."""

    def url(self, name):
        import os

        from django.contrib.staticfiles import finders

        url = super().url(name)
        path = finders.find(name) if name else None
        if isinstance(path, str) and os.path.exists(path):
            url += ("&" if "?" in url else "?") + "v=" + str(int(os.path.getmtime(path)))
        return url
