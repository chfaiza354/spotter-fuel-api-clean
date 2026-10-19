import csv
import os
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand
from django.db import transaction

from fuel.models import TruckStop

CSV_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "..", "fuel-prices-for-be-assessment.csv",
)
CSV_PATH = os.path.abspath(CSV_PATH)


class Command(BaseCommand):
    help = "Load the OPIS fuel-price CSV into the TruckStop table (deduped by OPIS ID, keeping the lowest price)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            default=CSV_PATH,
            help="Path to fuel-prices-for-be-assessment.csv",
        )

    def handle(self, *args, **options):
        path = options["csv"]
        if not os.path.exists(path):
            self.stderr.write(self.style.ERROR(f"CSV not found: {path}"))
            return

        # Dedupe by OPIS ID, keeping the cheapest price seen for each station.
        best = {}
        with open(path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                opis_id = (row.get("OPIS Truckstop ID") or "").strip()
                if not opis_id:
                    continue
                try:
                    price = Decimal(str(row.get("Retail Price") or "0").strip())
                except InvalidOperation:
                    continue
                if opis_id not in best or price < best[opis_id]["price"]:
                    best[opis_id] = {
                        "price": price,
                        "name": (row.get("Truckstop Name") or "").strip(),
                        "address": (row.get("Address") or "").strip(),
                        "city": (row.get("City") or "").strip(),
                        "state": (row.get("State") or "").strip(),
                        "rack_id": (row.get("Rack ID") or "").strip(),
                    }

        objs = [
            TruckStop(
                opis_id=opis_id,
                name=d["name"],
                address=d["address"],
                city=d["city"],
                state=d["state"],
                rack_id=d["rack_id"],
                retail_price=d["price"],
            )
            for opis_id, d in best.items()
        ]

        with transaction.atomic():
            TruckStop.objects.all().delete()
            TruckStop.objects.bulk_create(objs, batch_size=1000)

        self.stdout.write(
            self.style.SUCCESS(f"Loaded {len(objs)} unique truck stops.")
        )
