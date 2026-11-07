from django.conf import settings
from django.utils import translation


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
    """The user's language from their account; before sign-in, the language cookie (default Arabic)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            language = user.language
        else:
            language = request.COOKIES.get(settings.LANGUAGE_COOKIE_NAME, settings.LANGUAGE_CODE)
        if language not in ("ar", "en"):
            language = settings.LANGUAGE_CODE
        translation.activate(language)
        request.LANGUAGE_CODE = language
        response = self.get_response(request)
        response.setdefault("Content-Language", language)
        translation.deactivate()
        return response
