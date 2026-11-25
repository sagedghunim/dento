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


# Places created per client IP (design D2): 3 an hour and 10 a day count successful sign-ups and new
# places; `signup_try` counts every attempt that passes validation, so a taken username or mobile
# (409) can be probed only so often from one address.
PLACE_LIMITS = {
    "signup_h": (3, dt.timedelta(hours=1)),
    "signup_d": (10, dt.timedelta(days=1)),
    "signup_try": (20, dt.timedelta(hours=1)),
    # Wrong six-digit codes per account per day (keyed by username), across sign-ins and devices.
    "totp_day": (15, dt.timedelta(days=1)),
}


def _start(now, window):
    seconds = int(window.total_seconds())
    epoch = int(now.timestamp())
    return dt.datetime.fromtimestamp(epoch - epoch % seconds, tz=dt.UTC)


def place_wait(scopes, key, now=None):
    """Seconds until another place may be created from `key` (an IP) under these scopes, or 0."""
    now = now or timezone.now()
    with connections["default"].cursor() as cursor:
        for scope in scopes:
            limit, window = PLACE_LIMITS[scope]
            start = _start(now, window)
            cursor.execute("SELECT failures FROM accounts_throttle WHERE scope = %s AND key = %s AND window_start = %s",
                           [scope, key, start])
            row = cursor.fetchone()
            if row and row[0] >= limit:
                return max(1, int((start + window - now).total_seconds()))
    return 0


def place_take(scope, key, now=None):
    """Count one more under `scope` and say whether it is within the limit.

    The upsert locks the window's row, so two requests from one address can't both take the last
    place. Inside a transaction that later rolls back, the count goes back with it.
    """
    limit, window = PLACE_LIMITS[scope]
    with connections["default"].cursor() as cursor:
        cursor.execute(
            "INSERT INTO accounts_throttle (scope, key, window_start, failures) VALUES (%s, %s, %s, 1) "
            "ON CONFLICT (scope, key, window_start) DO UPDATE SET failures = accounts_throttle.failures + 1 "
            "RETURNING failures",
            [scope, key, _start(now or timezone.now(), window)],
        )
        return cursor.fetchone()[0] <= limit


def clear_old(now=None):
    """Nightly: drop windows older than two days (the longest window is a day)."""
    cutoff = (now or timezone.now()) - dt.timedelta(days=2)
    with connections["default"].cursor() as cursor:
        cursor.execute("DELETE FROM accounts_throttle WHERE window_start < %s", [cutoff])
