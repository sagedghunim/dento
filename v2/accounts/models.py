"""People who sign in (design 4.1). A global table: one row per person, across clinics."""

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower

from core.text import normalize_phone


class UserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, username, password=None, **fields):
        username = (username or "").strip().lower()
        if not username:
            raise ValueError("A username is required.")
        if fields.get("phone"):
            fields["phone"] = normalize_phone(fields["phone"])
        user = self.model(username=username, **fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def get_by_natural_key(self, username):
        return self.get(username=(username or "").strip().lower())


class User(AbstractBaseUser):
    class Language(models.TextChoices):
        ARABIC = "ar", "العربية"
        ENGLISH = "en", "English"

    username = models.CharField(max_length=150, unique=True)
    # National form for Egyptian mobiles (core.text.normalize_phone). Optional, unique when set.
    phone = models.CharField(max_length=20, null=True, blank=True, unique=True)
    full_name = models.CharField(max_length=120)
    language = models.CharField(max_length=2, choices=Language.choices, default=Language.ARABIC)
    is_active = models.BooleanField(default=True)
    is_platform_admin = models.BooleanField(default=False)
    must_change_password = models.BooleanField(default=False)
    # Two-factor sign-in (design 5.9). The last accepted step stops a code being used twice.
    totp_secret = models.CharField(max_length=64, blank=True, default="")
    totp_confirmed_at = models.DateTimeField(null=True, blank=True)
    totp_last_step = models.BigIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = UserManager()

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(username=Lower("username")), name="user_username_lower"),
            models.CheckConstraint(condition=Q(language__in=["ar", "en"]), name="user_language_valid"),
        ]

    @property
    def has_totp(self):
        return bool(self.totp_secret and self.totp_confirmed_at)

    def __str__(self):
        return self.username
