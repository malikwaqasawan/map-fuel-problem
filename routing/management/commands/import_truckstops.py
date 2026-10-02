import csv
import json
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from routing.management.commands.import_gazetteer import gazetteer_key
from routing.models import TruckStop

DEFAULT_CSV_PATH = settings.BASE_DIR / "routing" / "data" / "fuel-prices-for-be-assessment.csv"
GAZETTEER_LOOKUP_PATH = settings.BASE_DIR / "routing" / "data" / "gazetteer_lookup.json"


class Command(BaseCommand):
    help = "Load fuel-prices-for-be-assessment.csv into TruckStop, joining the Gazetteer lookup for coordinates."

    def add_arguments(self, parser):
        parser.add_argument("--csv-path", default=str(DEFAULT_CSV_PATH))

    def handle(self, *args, **opts):
        gazetteer = {}
        if GAZETTEER_LOOKUP_PATH.exists():
            gazetteer = json.loads(GAZETTEER_LOOKUP_PATH.read_text())
        else:
            self.stderr.write(self.style.WARNING(
                f"{GAZETTEER_LOOKUP_PATH} not found - run `import_gazetteer` first. "
                "Proceeding with all rows unlocated."
            ))

        to_create = []
        rejected = 0
        located_city = 0
        located_none = 0

        with open(opts["csv_path"], newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row_number, row in enumerate(reader):
                city = row["City"].strip()
                state = row["State"].strip().upper()

                try:
                    price = Decimal(row["Retail Price"].strip())
                except (InvalidOperation, AttributeError, KeyError):
                    rejected += 1
                    continue
                if not price.is_finite() or price <= 0:
                    rejected += 1
                    continue

                try:
                    opis_id = int(row["OPIS Truckstop ID"].strip())
                except (ValueError, AttributeError):
                    rejected += 1
                    continue

                coords = gazetteer.get(gazetteer_key(city, state))
                if coords:
                    lat, lon = coords
                    precision, source = "city", "gazetteer"
                    located_city += 1
                else:
                    lat, lon = None, None
                    precision, source = "none", "none"
                    located_none += 1

                to_create.append(TruckStop(
                    source_row_number=row_number,
                    opis_id=opis_id,
                    name=row["Truckstop Name"].strip(),
                    address=row["Address"].strip(),
                    city=city,
                    state=state,
                    rack_id=row["Rack ID"].strip(),
                    retail_price=price,
                    latitude=lat,
                    longitude=lon,
                    coordinate_precision=precision,
                    coordinate_source=source,
                ))

        with transaction.atomic():
            deleted, _ = TruckStop.objects.all().delete()
            TruckStop.objects.bulk_create(to_create, batch_size=500)

        total = len(to_create)
        self.stdout.write(self.style.SUCCESS(
            f"Imported {total:,} rows (cleared {deleted:,} previous rows, rejected {rejected:,} "
            f"invalid rows). Coordinates: {located_city:,} city-level "
            f"({located_city/total*100:.1f}%), {located_none:,} unlocated "
            f"({located_none/total*100:.1f}%, excluded from routing until geocoded)."
        ))
