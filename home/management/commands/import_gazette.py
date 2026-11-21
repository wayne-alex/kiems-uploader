"""
Load the IEBC gazette CSV into County, Constituency, Ward, RegistrationCentre.

Place at:  <your_app>/management/commands/import_gazette.py
           (add empty __init__.py in management/ and management/commands/ if missing)

Usage:
    python manage.py import_gazette registration_centres_turbo.csv --app <your_app_label>
    python manage.py import_gazette registration_centres_all.csv   --app <your_app_label> --dry-run

It is idempotent: safe to run again, it updates instead of duplicating.
"""
import csv

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Import IEBC gazette registration centres from CSV."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")
        parser.add_argument("--app", required=True, help="App label that holds the models")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **opts):
        County = apps.get_model(opts["app"], "County")
        Constituency = apps.get_model(opts["app"], "Constituency")
        Ward = apps.get_model(opts["app"], "Ward")
        Centre = apps.get_model(opts["app"], "RegistrationCentre")

        with open(opts["csv_path"], newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            raise CommandError("CSV is empty")

        counts = {"county": 0, "const": 0, "ward": 0, "centre": 0}
        county_cache, const_cache, ward_cache = {}, {}, {}

        with transaction.atomic():
            for r in rows:
                # ---- County (match on name, fill code) ----
                ck = r["county_code"]
                if ck not in county_cache:
                    county, made = County.objects.get_or_create(
                        name=r["county_name"], defaults={"code": ck}
                    )
                    if not made and county.code != ck:
                        county.code = ck
                        county.save(update_fields=["code"])
                    counts["county"] += made
                    county_cache[ck] = county
                county = county_cache[ck]

                # ---- Constituency (match on code, else name) ----
                kk = r["const_code"]
                if kk not in const_cache:
                    const = (Constituency.objects.filter(code=kk).first()
                             or Constituency.objects.filter(name=r["const_name"]).first())
                    if const is None:
                        const = Constituency.objects.create(
                            name=r["const_name"], code=kk, county=county)
                        counts["const"] += 1
                    else:
                        const.code, const.county = kk, county
                        const.save(update_fields=["code", "county"])
                    const_cache[kk] = const
                const = const_cache[kk]

                # ---- Ward (match on code, else name inside the constituency) ----
                wk = r["ward_code"]
                if wk not in ward_cache:
                    ward = (Ward.objects.filter(code=wk).first()
                            or Ward.objects.filter(name=r["ward_name"], constituency=const).first()
                            or Ward.objects.filter(name=r["ward_name"], constituency__isnull=True).first())
                    if ward is None:
                        name = r["ward_name"]
                        # Ward.name is unique=True in your model, but Kenya has
                        # 33 repeated ward names (e.g. TOWNSHIP x9). Disambiguate.
                        if Ward.objects.filter(name=name).exists():
                            name = f"{name} ({const.name})"
                        ward = Ward.objects.create(name=name, code=wk, constituency=const)
                        counts["ward"] += 1
                    else:
                        ward.code, ward.constituency = wk, const
                        ward.save(update_fields=["code", "constituency"])
                    ward_cache[wk] = ward
                ward = ward_cache[wk]

                # ---- Registration centre (match on full_code) ----
                _, made = Centre.objects.update_or_create(
                    full_code=r["full_code"],
                    defaults={"code": r["centre_code"], "name": r["centre_name"], "ward": ward},
                )
                counts["centre"] += made

            if opts["dry_run"]:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("DRY RUN - rolled back"))

        self.stdout.write(self.style.SUCCESS(
            f"Rows read: {len(rows)} | created -> counties {counts['county']}, "
            f"constituencies {counts['const']}, wards {counts['ward']}, centres {counts['centre']}"
        ))
