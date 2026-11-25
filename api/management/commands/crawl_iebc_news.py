from datetime import date

import httpx
from django.core.management.base import BaseCommand

from api.models import Announcement
from api.parsers.iebc_news import parse_news_list, parse_news_detail

LIST_URL = "https://www.iebc.or.ke/news/"
UA = "IEBC-KIEMS-Field/2.0 (+crawler)"
TIMEOUT = 20
MAX_DETAILS_PER_RUN = 15  # cap so we don't hammer IEBC


class Command(BaseCommand):
    help = "Crawl IEBC news page and upsert new announcements."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--max", type=int, default=MAX_DETAILS_PER_RUN,
            help="Max detail pages to fetch this run",
        )

    def handle(self, *args, **opts):
        dry = opts["dry_run"]
        max_details = opts["max"]

        self.stdout.write(f"Fetching {LIST_URL} …")
        try:
            with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
                r = c.get(LIST_URL, headers={"User-Agent": UA})
        except httpx.RequestError as e:
            self.stderr.write(f"Network error: {e}")
            return

        if r.status_code != 200:
            self.stderr.write(f"HTTP {r.status_code}")
            return

        items = parse_news_list(r.text)
        self.stdout.write(f"Parsed {len(items)} articles from listing.")

        existing_slugs = set(
            Announcement.objects
            .filter(source="IEBC")
            .values_list("external_slug", flat=True)
        )

        new_items = [i for i in items if i["slug"] and i["slug"] not in existing_slugs]
        self.stdout.write(
            f"{len(new_items)} new articles to import "
            f"(of which {min(len(new_items), max_details)} will get full detail this run)."
        )

        created = 0
        enriched = 0
        with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as c:
            for i, item in enumerate(new_items):
                body_html = ""
                title = item["title_guess"] or item["title_listing"] or "IEBC news"

                if i < max_details:
                    try:
                        dr = c.get(item["url"], headers={"User-Agent": UA})
                        if dr.status_code == 200:
                            detail = parse_news_detail(dr.text)
                            body_html = detail.get("body_html", "") or ""
                            title = detail.get("title") or title
                            item["image_url"] = detail.get("image_url") or item["image_url"]
                            item["published_at"] = (
                                    detail.get("published_at") or item["published_at"]
                            )
                    except httpx.RequestError as e:
                        self.stderr.write(f"  detail fetch failed for {item['url']}: {e}")

                if dry:
                    self.stdout.write(
                        f"  [dry] {title[:70]} — "
                        f"{len(body_html)} body chars, date={item['published_at']}"
                    )
                    continue

                _, was_created = Announcement.objects.update_or_create(
                    source="IEBC",
                    external_slug=item["slug"],
                    defaults={
                        "title": title[:300],
                        "body": item["excerpt"] or "",
                        "body_html": body_html,
                        "image_url": item["image_url"] or "",
                        "external_url": item["url"],
                        "published_at": item["published_at"] or date.today(),
                    },
                )
                if was_created:
                    created += 1
                elif body_html:
                    enriched += 1

        self.stdout.write(self.style.SUCCESS(
            f"Done. Created: {created}, enriched existing: {enriched}, "
            f"skipped (no detail): {max(0, len(new_items) - max_details)}."
        ))
