from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = "List users in the system."

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            help="Filter by email containing this value.",
        )
        parser.add_argument(
            "--active-only",
            action="store_true",
            help="Show only active users.",
        )
        parser.add_argument(
            "--staff-only",
            action="store_true",
            help="Show only staff users.",
        )
        parser.add_argument(
            "--superuser-only",
            action="store_true",
            help="Show only superusers.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Maximum number of users to display.",
        )
    
    def handle(self, *args, **kwargs):
        User = get_user_model()
        qs = User.objects.all()
        
        email = kwargs.get("email")
        if email:
            qs = qs.filter(email__icontains=email)
        
        active_only = kwargs.get("active_only")
        if active_only:
            qs = qs.filter(is_active=True)
        
        staff_only = kwargs.get("staff_only")
        if staff_only:
            qs = qs.filter(is_staff=True)
        
        superuser_only = kwargs.get("superuser_only")
        if superuser_only:
            qs = qs.filter(is_superuser=True)
        
        limit = kwargs.get("limit")
        if limit:
            qs = qs[:limit]
        
        users = list(qs)
        if not users:
            self.stdout.write(self.style.WARNING("No users found."))
            return
        
        header = (
            f"{'ID':<6} {'EMAIL':<30} {'USERNAME':<20} "
            f"{'ACTIVE':<8} {'STAFF':<8} {'SUPERUSER':<10}"
        )
        self.stdout.write(header)
        self.stdout.write("-" * len(header))

        for user in users:
            user_id = str(user.id)
            email = getattr(user, "email", "") or "-"
            username = getattr(user, "username", "") or "-"
            is_active = str(getattr(user, "is_active", False))
            is_staff = str(getattr(user, "is_staff", False))
            is_superuser = str(getattr(user, "is_superuser", False))

            self.stdout.write(
                f"{user_id:<6} {email:<30} {username:<20} "
                f"{is_active:<8} {is_staff:<8} {is_superuser:<10}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"Total users: {len(users)}"))
        