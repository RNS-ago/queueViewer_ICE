import csv

from django.core.management.base import BaseCommand, CommandError
from django.utils.dateparse import parse_datetime

from counts.models import CountRecord


class Command(BaseCommand):
    help = (
        "Load count records from a counts.csv exported by the device "
        "(columns: ts,device_id,boot_id,event,in,out,occupancy)."
    )

    def add_arguments(self, parser):
        parser.add_argument("csvfile", help="Path to a counts.csv file.")
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete all existing count records first (API keys/users are kept).",
        )

    def handle(self, *args, **options):
        path = options["csvfile"]
        try:
            with open(path, newline="") as f:
                rows = list(csv.DictReader(f))
        except OSError as exc:
            raise CommandError(f"Could not read {path}: {exc}")

        if not rows:
            raise CommandError(f"No rows found in {path}")

        if options["clear"]:
            deleted, _ = CountRecord.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Deleted {deleted} existing record(s)."))

        records = []
        for row in rows:
            ts = (row.get("ts") or "").strip()
            records.append(
                CountRecord(
                    device_id=(row.get("device_id") or "").strip()[:64],
                    boot_id=(row.get("boot_id") or "").strip()[:64],
                    ts=ts[:64],
                    ts_parsed=parse_datetime(ts),
                    event=(row.get("event") or "").strip()[:16],
                    count_in=int(row.get("in") or 0),
                    count_out=int(row.get("out") or 0),
                    occupancy=int(row.get("occupancy") or 0),
                )
            )

        CountRecord.objects.bulk_create(records)
        self.stdout.write(self.style.SUCCESS(f"Loaded {len(records)} record(s) from {path}."))
