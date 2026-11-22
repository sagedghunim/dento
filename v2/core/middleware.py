import time

from django.conf import settings
from django.utils import translation

SESSION_RENEWED = "renewed_at"
RENEW_EVERY = 3600  # seconds: at most one session write an hour


class SessionRenewMiddleware:
    """A sign-in lasts while it is used (Saged, 2026-11-22; replaces design 5.9's fixed 12 hours).

    Each use, at most once an hour, saves the session again, which pushes its end back
    by SESSION_COOKIE_AGE and re-sends the cookie. Closing the browser keeps the sign-in.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            now = time.time()
            if now - request.session.get(SESSION_RENEWED, 0) > RENEW_EVERY:
                request.session[SESSION_RENEWED] = now
        return self.get_response(request)


class AppHeadersMiddleware:
    """`no-store` on every answer and the app version (design 6.1, 8).

    Patient data must never reach a shared desk PC's disk cache. Static files are
    served by the proxy in production and keep their own caching there.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if not request.path.startswith(settings.STATIC_URL):
            response["Cache-Control"] = "no-store"
            response["X-App-Version"] = settings.APP_VERSION
        return response


class UserLanguageMiddleware:
    """The language last picked on this browser (the cookie), else the account's, else Arabic.

    The cookie wins so that switching accounts on one PC doesn't flip the language (Saged, 2026-11-22).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        language = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME)
        if language not in ("ar", "en"):
            language = user.language if user is not None and user.is_authenticated else settings.LANGUAGE_CODE
        if language not in ("ar", "en"):
            language = settings.LANGUAGE_CODE
        translation.activate(language)
        request.LANGUAGE_CODE = language
        response = self.get_response(request)
        response.setdefault("Content-Language", language)
        translation.deactivate()
        return response
