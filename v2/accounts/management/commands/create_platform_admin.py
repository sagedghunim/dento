"""Create a platform admin account (design 5.12). It must set up two-step sign-in at first sign-in."""

import getpass

from django.core.management.base import BaseCommand, CommandError

from accounts.models import User
from directory.models import UserClinic


class Command(BaseCommand):
    help = "Create a platform admin. Platform admins hold no clinic memberships and see no patient data."

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument("--name", default="Hamada support")
        parser.add_argument("--password", help="Prompted for if omitted.")

    def handle(self, *args, username, name, password, **options):
        if User.objects.filter(username=username.lower()).exists():
            user = User.objects.get(username=username.lower())
            if UserClinic.objects.filter(user=user).exists():
                raise CommandError("That account belongs to a clinic; platform admins can't hold memberships.")
            User.objects.filter(pk=user.pk).update(is_platform_admin=True)
            self.stdout.write(self.style.SUCCESS(f"{username} is now a platform admin."))
            return
        password = password or getpass.getpass("Password: ")
        if len(password) < 10:
            raise CommandError("Use at least 10 characters.")
        User.objects.create_user(username, password=password, full_name=name, is_platform_admin=True)
        self.stdout.write(self.style.SUCCESS(f"Platform admin {username} created. Set up two-step sign-in at first sign-in."))
