
import csv

from itertools import islice

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


BATCH_SIZE = 1000


def chunks(items, size):
    iterator = iter(items)
    while True:
        batch = list(islice(iterator, size))
        if not batch:
            break
        yield batch


class Command(BaseCommand):
    help = "Import IEBC gazette registration centres from CSV."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument(
            "--app",
            required=True,
            help="App label that holds the models"
        )
        parser.add_argument(
            "--dry-run",
            action="store_true"
        )

    def handle(self, *args, **opts):
        County = apps.get_model(opts["app"], "County")
        Constituency = apps.get_model(opts["app"], "Constituency")
        Ward = apps.get_model(opts["app"], "Ward")
        Centre = apps.get_model(opts["app"], "RegistrationCentre")

        csv_path = opts["csv_path"]

        # Read CSV
        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))

        if not rows:
            raise CommandError("CSV is empty")

        required_columns = [
            "county_code", "county_name",
            "const_code", "const_name",
            "ward_code", "ward_name",
            "full_code", "centre_code", "centre_name",
        ]

        missing = [
            column for column in required_columns
            if column not in rows[0]
        ]

        if missing:
            raise CommandError(
                f"CSV is missing columns: {', '.join(missing)}"
            )

        counts = {
            "county": 0,
            "const": 0,
            "ward": 0,
            "centre": 0,
        }

        county_cache = {}
        const_cache = {}
        ward_cache = {}

        self.stdout.write(
            self.style.NOTICE(
                f"Loaded {len(rows):,} CSV rows. Starting import..."
            )
        )

        with transaction.atomic():

            # --------------------------------------------------
            # 1. COUNTIES
            # --------------------------------------------------

            self.stdout.write("Processing counties...")

            for r in rows:
                ck = r["county_code"]

                if ck in county_cache:
                    continue

                county, created = County.objects.get_or_create(
                    name=r["county_name"],
                    defaults={"code": ck},
                )

                if not created and county.code != ck:
                    county.code = ck
                    county.save(update_fields=["code"])

                if created:
                    counts["county"] += 1

                county_cache[ck] = county

            # --------------------------------------------------
            # 2. CONSTITUENCIES
            # --------------------------------------------------

            self.stdout.write("Processing constituencies...")

            for r in rows:
                kk = r["const_code"]

                if kk in const_cache:
                    continue

                county = county_cache[r["county_code"]]

                const = (
                    Constituency.objects.filter(code=kk).first()
                    or Constituency.objects.filter(
                        name=r["const_name"]
                    ).first()
                )

                if const is None:
                    const = Constituency.objects.create(
                        name=r["const_name"],
                        code=kk,
                        county=county,
                    )
                    counts["const"] += 1
                else:
                    const.code = kk
                    const.county = county
                    const.save(update_fields=["code", "county"])

                const_cache[kk] = const

            # --------------------------------------------------
            # 3. WARDS
            # --------------------------------------------------

            self.stdout.write("Processing wards...")

            for r in rows:
                wk = r["ward_code"]

                if wk in ward_cache:
                    continue

                const = const_cache[r["const_code"]]

                ward = (
                    Ward.objects.filter(code=wk).first()
                    or Ward.objects.filter(
                        name=r["ward_name"],
                        constituency=const,
                    ).first()
                    or Ward.objects.filter(
                        name=r["ward_name"],
                        constituency__isnull=True,
                    ).first()
                )

                if ward is None:
                    name = r["ward_name"]

                    # Handle repeated ward names
                    if Ward.objects.filter(name=name).exists():
                        name = f"{name} ({const.name})"

                    ward = Ward.objects.create(
                        name=name,
                        code=wk,
                        constituency=const,
                    )

                    counts["ward"] += 1

                else:
                    ward.code = wk
                    ward.constituency = const
                    ward.save(update_fields=["code", "constituency"])

                ward_cache[wk] = ward

            # --------------------------------------------------
            # 4. REGISTRATION CENTRES - BULK UPSERT
            # --------------------------------------------------

            self.stdout.write(
                self.style.NOTICE(
                    "Preparing registration centres..."
                )
            )

            # Deduplicate by full_code. If a CSV code occurs more
            # than once, the last occurrence wins.
            centre_data = {}

            for r in rows:
                centre_data[r["full_code"]] = {
                    "code": r["centre_code"],
                    "name": r["centre_name"],
                    "ward": ward_cache[r["ward_code"]],
                }

            centre_codes = list(centre_data.keys())

            self.stdout.write(
                f"Prepared {len(centre_codes):,} unique centres."
            )

            # Find existing records so we can report accurate
            # created counts.
            existing_codes = set()

            for code_batch in chunks(centre_codes, BATCH_SIZE):
                existing_codes.update(
                    Centre.objects.filter(
                        full_code__in=code_batch
                    ).values_list("full_code", flat=True)
                )

            counts["centre"] = len(
                set(centre_codes) - existing_codes
            )

            # Build objects
            centre_objects = [
                Centre(
                    full_code=full_code,
                    code=data["code"],
                    name=data["name"],
                    ward=data["ward"],
                )
                for full_code, data in centre_data.items()
            ]

            # Save in batches using PostgreSQL ON CONFLICT
            total = len(centre_objects)

            for index, batch in enumerate(
                chunks(centre_objects, BATCH_SIZE),
                start=1,
            ):
                Centre.objects.bulk_create(
                    batch,
                    batch_size=BATCH_SIZE,
                    update_conflicts=True,
                    unique_fields=["full_code"],
                    update_fields=[
                        "code",
                        "name",
                        "ward",
                    ],
                )

                processed = min(index * BATCH_SIZE, total)

                self.stdout.write(
                    f"\rCentres processed: {processed:,}/{total:,}",
                    ending="",
                )
                self.stdout.flush()

            self.stdout.write("")

            # --------------------------------------------------
            # 5. DRY RUN
            # --------------------------------------------------

            if opts["dry_run"]:
                transaction.set_rollback(True)

                self.stdout.write(
                    self.style.WARNING(
                        "DRY RUN: All database changes rolled back."
                    )
                )

        # ------------------------------------------------------
        # 6. SUMMARY
        # ------------------------------------------------------

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                "IMPORT COMPLETED"
            )
        )

        self.stdout.write(f"CSV rows: {len(rows):,}")
        self.stdout.write(
            f"Unique centres: {len(centre_data):,}"
        )
        self.stdout.write(
            f"New counties: {counts['county']:,}"
        )
        self.stdout.write(
            f"New constituencies: {counts['const']:,}"
        )
        self.stdout.write(
            f"New wards: {counts['ward']:,}"
        )
        self.stdout.write(
            f"New centres: {counts['centre']:,}"
        )

        if opts["dry_run"]:
            self.stdout.write(
                self.style.WARNING(
                    "No changes were committed."
                )
            )
