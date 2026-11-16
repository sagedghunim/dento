from django.urls import include, path

from accounts import views as accounts
from clinics import views as clinics
from core import views as core
from web import views as web

api = "api/v1/"

urlpatterns = [
    path("healthz", core.healthz),
    path("readyz", core.readyz),
    # Pages (design 7.3)
    path("", web.home, name="home"),
    path("login", web.login_page, name="login"),
    path("clinics", web.clinics_page, name="clinics"),
    path("account/password", web.password_page, name="password"),
    path("account/security", web.security_page, name="security"),
    # Account actions (design 6.2)
    path(api + "auth/login", accounts.login_view),
    path(api + "auth/code", accounts.code_view),
    path(api + "auth/logout", accounts.logout_view),
    path(api + "me", accounts.me_view),
    path(api + "me/clinic", accounts.pick_clinic_view),
    path(api + "me/password", accounts.password_view),
    path(api + "me/language", accounts.language_view),
    path(api + "me/totp", accounts.totp_setup_view),
    path(api + "me/totp/confirm", accounts.totp_confirm_view),
    # Clinic configuration
    path(api + "clinic", clinics.clinic_view),
    path(api + "settings/clinic", clinics.clinic_settings_view),
    # Platform admin (directory)
    path("", include("directory.urls")),
    # Each M1+ app adds one include line below for its own urls.py.
    path("", include("clinics.urls")),
    path("", include("patients.urls")),
    path("", include("scheduling.urls")),
    path("", include("billing.urls")),
    path("", include("clinical.urls")),
    path("", include("billing.package_urls")),
    path("", include("billing.discount_urls")),
    path("", include("patients.tag_urls")),
    path("", include("inventory.urls")),
    path("", include("finance.urls")),
    path("", include("reports.urls")),
    path("", include("web.urls")),  # M1 pages and fragments
]
