"""Settings for Hamada v2.

Everything that differs between machines comes from the environment. For local
work, `v2/.env` is read first (see `.env.example`).
"""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env_file(path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file(BASE_DIR / ".env")


def env(name, default=None):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


TESTING = sys.argv[1:2] == ["test"]
DEBUG = env_bool("DJANGO_DEBUG", False)
SECRET_KEY = env("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY is not set. Run scripts\\pg.ps1 init or copy .env.example to .env.")
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
if TESTING:
    ALLOWED_HOSTS.append("testserver")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "procrastinate.contrib.django",
    "core",
    "accounts",
    "directory",
    "clinics",
    "audit",
    "web",
]

# Background jobs live in each app's jobs.py (design 3.1).
PROCRASTINATE_AUTODISCOVER_MODULE_NAME = "jobs"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "core.middleware.UserLanguageMiddleware",
    "core.middleware.AppHeadersMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
            ],
        },
    },
]

# PostgreSQL only. `default` is the directory; each shard holds whole clinics
# (design 3.3). Timeouts are per-role settings (design 4.0), not connection
# options, so they survive PgBouncer's transaction pooling.
def _database(name):
    return {
        "ENGINE": "django.db.backends.postgresql",
        "HOST": env("PGHOST", "127.0.0.1"),
        "PORT": env("PGPORT", "5432"),
        "NAME": name,
        "USER": env("PGUSER", "hamada"),
        "PASSWORD": env("PGPASSWORD", ""),
        "CONN_MAX_AGE": int(env("PG_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
        # Behind PgBouncer in transaction mode (design 9.4).
        "DISABLE_SERVER_SIDE_CURSORS": env_bool("PG_BEHIND_PGBOUNCER", False),
    }


DATABASES = {"default": _database(env("PGDATABASE", "hamada_dir"))}
# HAMADA_SHARDS=shard_1=hamada_s001,shard_2=hamada_s002
SHARDS = {}
for _item in env_list("HAMADA_SHARDS", "shard_1=hamada_s001"):
    _alias, _name = _item.split("=", 1)
    if not _alias.startswith("shard_") or not _alias[6:].isdigit():
        raise ImproperlyConfigured(f"Shard aliases look like shard_<n>, not {_alias!r}.")
    DATABASES[_alias] = _database(_name)
    SHARDS[_alias] = int(_alias[6:])
DATABASE_ROUTERS = ["core.routing.ShardRouter"]

# Encrypts two-factor secrets (design 5.9).
FIELD_KEY = env("HAMADA_FIELD_KEY", "")
if not FIELD_KEY and not DEBUG:
    raise ImproperlyConfigured("HAMADA_FIELD_KEY is not set.")

# Redis is optional (design 3.1): without it, the cache is per process and
# sessions live in PostgreSQL.
if env("REDIS_URL"):
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": env("REDIS_URL")}}
    SESSION_ENGINE = "django.contrib.sessions.backends.cached_db"
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    SESSION_ENGINE = "django.contrib.sessions.backends.db"

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
if TESTING:
    # Real hashing is slow on purpose; tests only need it to be correct.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

LANGUAGE_CODE = "ar"
LANGUAGES = [("ar", "العربية"), ("en", "English")]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True
# The server works in UTC. Each clinic has its own time zone (design 4.0).
TIME_ZONE = "UTC"
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Hashed names in production, including the names inside ES module imports (design 7.1).
if not DEBUG:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "core.storage.ModuleManifestStaticFilesStorage"},
    }
APP_VERSION = env("HAMADA_APP_VERSION", "2.0.0-dev")
CSRF_FAILURE_VIEW = "core.views.csrf_failure"
LOGIN_URL = "/login"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
DATA_UPLOAD_MAX_MEMORY_SIZE = 64 * 1024  # design 6.1

# The prototype and the warehouse run on the same host on other ports, and
# cookies ignore ports, so Hamada v2 uses its own cookie names.
SESSION_COOKIE_NAME = "hamada_session"
CSRF_COOKIE_NAME = "hamada_csrftoken"
LANGUAGE_COOKIE_NAME = "hamada_language"
SESSION_COOKIE_AGE = 12 * 3600  # design 5.9
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
LANGUAGE_COOKIE_SECURE = not DEBUG

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", True)
    SECURE_HSTS_SECONDS = int(env("DJANGO_HSTS_SECONDS", "31536000"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:"],
    "connect-src": [CSP.SELF],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.SELF],
    "form-action": [CSP.SELF],
    "frame-ancestors": [CSP.NONE],
}

# The header a trusted proxy sets with the client's address; empty means the socket address.
CLIENT_IP_HEADER = env("CLIENT_IP_HEADER", "")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
