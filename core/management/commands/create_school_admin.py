"""
Create a school admin (or election officer) account.

Usage:
    python manage.py create_school_admin --email admin@school.edu.ng --role admin
    python manage.py create_school_admin --email officer@school.edu.ng --role officer
"""
import getpass

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Create a school admin (full control) or election officer (observer) account."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="Staff email (used to log in)")
        parser.add_argument("--name", default="", help="Full name of the staff member")
        parser.add_argument(
            "--role",
            choices=["admin", "officer"],
            default="admin",
            help="admin = full election administration; officer = observer dashboard",
        )
        parser.add_argument(
            "--password",
            default=None,
            help="Password (prompted interactively if omitted)",
        )

    def handle(self, *args, **options):
        User = get_user_model()

        email = options["email"].strip().lower()
        if User.objects.filter(email=email).exists():
            raise CommandError(f"An account with email '{email}' already exists.")

        password = options["password"]
        if not password:
            password = getpass.getpass("Password: ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                raise CommandError("Passwords do not match.")
        if len(password) < 8:
            raise CommandError("Password must be at least 8 characters.")

        user = User.objects.create_user(
            username=email,
            email=email,
            password=password,
            first_name=options["name"],
            user_type="ADMIN" if options["role"] == "admin" else "ELECTION_OFFICER",
        )
        user.is_staff = True
        user.save()

        self.stdout.write(self.style.SUCCESS(f"Created {options['role']} account: {email}"))
