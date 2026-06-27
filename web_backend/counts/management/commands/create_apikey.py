from django.core.management.base import BaseCommand

from counts.models import ApiKey


class Command(BaseCommand):
    help = "Create an API key authorizing a client to POST to /api/log."

    def add_arguments(self, parser):
        parser.add_argument(
            "name",
            help="Which device/client the key is for, e.g. 'entrance-01'.",
        )

    def handle(self, *args, **options):
        obj, raw = ApiKey.generate(options["name"])
        self.stdout.write(self.style.SUCCESS(f"Created API key for '{obj.name}'."))
        self.stdout.write("")
        self.stdout.write("  " + raw)
        self.stdout.write("")
        self.stdout.write(
            "Copy it now — only a hash is stored, so it cannot be shown again."
        )
        self.stdout.write("Set this as AUTH_TOKEN in esp32/src/config.py.")
