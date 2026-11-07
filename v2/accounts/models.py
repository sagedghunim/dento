"""People who sign in (design 4.1). Directory tables: one row per person, across clinics."""

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone

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
    temp_password_expires_at = models.DateTimeField(null=True, blank=True)
    # Two-factor sign-in (design 5.9). The secret is encrypted (accounts.crypto);
    # the last accepted step stops a code being used twice, even on two servers.
    totp_secret = models.CharField(max_length=255, blank=True, default="")
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


class RecoveryCode(models.Model):
    """Ten single-use codes per person, stored hashed (design 5.9)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=128)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "accounts_recovery_code"


class Throttle(models.Model):
    """Failed sign-in counters, kept in PostgreSQL so they work without Redis (design 5.9)."""

    class Scope(models.TextChoices):
        USER_IP = "user_ip"
        USER = "user"
        IP = "ip"
        TOTP = "totp"

    scope = models.CharField(max_length=10, choices=Scope.choices)
    key = models.CharField(max_length=200)
    window_start = models.DateTimeField()
    failures = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "accounts_throttle"
        constraints = [models.UniqueConstraint(fields=["scope", "key", "window_start"], name="throttle_window_uniq")]


class AccountEvent(models.Model):
    """Sign-ins and account changes. Partitioned by month in SQL (design 4.1, 4.10)."""

    id = models.BigAutoField(primary_key=True)
    at = models.DateTimeField(default=timezone.now)
    user_id = models.BigIntegerField(null=True)
    kind = models.CharField(max_length=24)
    ip = models.GenericIPAddressField(null=True)

    class Meta:
        managed = False
        db_table = "accounts_account_event"


class AccountRequestKey(models.Model):
    """Idempotency keys for account and platform endpoints (design 4.11)."""

    pk = models.CompositePrimaryKey("user_id", "key")
    user_id = models.BigIntegerField()
    key = models.UUIDField()
    endpoint = models.CharField(max_length=80)
    request_hash = models.CharField(max_length=64)
    status = models.PositiveSmallIntegerField(default=200)
    target_type = models.CharField(max_length=40, blank=True, default="")
    target_id = models.BigIntegerField(null=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "accounts_request_key"
