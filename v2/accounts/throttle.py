"""Failed sign-in counters in PostgreSQL (design 5.9).

They work without Redis and across every web process. Only failures count.
Limits per 15-minute window:
- 5 per (username, IP): stops guessing one account from one place;
- 20 per username: a looser limit, so nobody can lock the owner out from elsewhere;
- 200 per IP: loose, because clinic routers and carrier NAT share addresses.
"""

import datetime as dt

from django.db import connections
from django.utils import timezone

WINDOW = dt.timedelta(minutes=15)
LIMITS = {"user_ip": 5, "user": 20, "ip": 200, "totp": 5}


def _window_start(now):
    epoch = int(now.timestamp())
    return dt.datetime.fromtimestamp(epoch - epoch % int(WINDOW.total_seconds()), tz=dt.UTC)


def keys_for(username, ip):
    username = (username or "").strip().lower()
    return [("user_ip", f"{username}|{ip}"), ("user", username), ("ip", ip or "-")]


def wait_seconds(keys, now=None):
    """Seconds until the caller may try again, or 0."""
    now = now or timezone.now()
    start = _window_start(now)
    with connections["default"].cursor() as cursor:
        for scope, key in keys:
            cursor.execute(
                "SELECT failures FROM accounts_throttle WHERE scope = %s AND key = %s AND window_start = %s",
                [scope, key, start],
            )
            row = cursor.fetchone()
            if row and row[0] >= LIMITS[scope]:
                return max(1, int((start + WINDOW - now).total_seconds()))
    return 0


def record_failure(keys, now=None):
    start = _window_start(now or timezone.now())
    with connections["default"].cursor() as cursor:
        for scope, key in keys:
            cursor.execute(
                "INSERT INTO accounts_throttle (scope, key, window_start, failures) VALUES (%s, %s, %s, 1) "
                "ON CONFLICT (scope, key, window_start) DO UPDATE SET failures = accounts_throttle.failures + 1",
                [scope, key, start],
            )


def clear_old(now=None):
    """Nightly: drop windows older than a day."""
    cutoff = (now or timezone.now()) - dt.timedelta(days=1)
    with connections["default"].cursor() as cursor:
        cursor.execute("DELETE FROM accounts_throttle WHERE window_start < %s", [cutoff])
