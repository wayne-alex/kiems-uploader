"""
Parser for the IEBC news pages.

Two functions:
  - parse_news_list(html) → list of {slug, url, title_hint, image_url, date, excerpt}
  - parse_news_detail(html) → {title, published_at, body_html, image_url}

Titles on the list page are truncated ("IEBC Joins NCAJ in Launch of S...").
We derive a best-guess full title from the URL slug, then override with
the real title when we fetch the detail page.
"""
import re
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from bs4 import BeautifulSoup, Comment


# ── Utility ────────────────────────────────────────────
def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _parse_date(s: str):
    """'4th Nov 2026' → date(2026, 11, 4)."""
    if not s:
        return None
    s = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", s.strip())
    try:
        return datetime.strptime(s, "%d %b %Y").date()
    except ValueError:
        return None


def _slug_from_url(url: str) -> str:
    """
    Extract the IEBC article slug from the query string.

    IEBC uses bare query strings as slugs: `?PRESS_RELEASE_-__MATTER_OF_...`
    — no `=` — so parse_qs() doesn't work here. We just take the raw query.
    """
    try:
        parsed = urlparse(url)
        return parsed.query or ""
    except Exception:
        return ""


def _title_from_slug(slug: str) -> str:
    """
    '_IEBC_Joins_NCAJ_in_Launch_of_Strategic_Plan_2026_-_2030...'
    → 'IEBC Joins NCAJ in Launch of Strategic Plan 2026 - 2030...'
    Rough but usually good enough for a headline.
    """
    if not slug:
        return ""
    s = slug.lstrip("_")
    s = s.replace("_-_", " - ").replace("__", " ")
    s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    # Trim trailing '...' left over from truncated listings, if present
    s = re.sub(r"\.{2,}$", "", s).strip()
    return s


def _absolutize_image(src: str) -> str:
    """IEBC sometimes uses '//uploads/...' — fix that."""
    if not src:
        return ""
    src = src.strip()
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return "https://www.iebc.or.ke" + src
    return src


# ── List page parser ──────────────────────────────────
def parse_news_list(html: str) -> list:
    """
    Return a list of dicts, one per article on the news listing page.
    """
    soup = BeautifulSoup(html, "html.parser")
    out = []

    for article in soup.select("article.post"):
        title_el = article.select_one("h5.entry-title a")
        if not title_el:
            continue

        url = (title_el.get("href") or "").strip()
        slug = _slug_from_url(url)
        title_listing = _clean(title_el.get_text())

        img_el = article.select_one(".post-thumb img, img.img-responsive")
        image_url = _absolutize_image(img_el.get("src") if img_el else "")

        date_el = article.select_one(".entry-date li span, .entry-date span")
        published_at = _parse_date(date_el.get_text() if date_el else "")

        excerpt_el = article.select_one("p.text-left, .entry-content p")
        excerpt = _clean(excerpt_el.get_text() if excerpt_el else "")
        if excerpt.startswith("...") or excerpt == "...":
            excerpt = ""

        out.append({
            "slug": slug,
            "url": url,
            "title_listing": title_listing,
            "title_guess": _title_from_slug(slug),
            "image_url": image_url,
            "published_at": published_at,
            "excerpt": excerpt,
        })

    # Dedupe by URL, preserving first occurrence
    seen = set()
    deduped = []
    for item in out:
        if item["url"] in seen:
            continue
        seen.add(item["url"])
        deduped.append(item)
    return deduped


# ── Detail page parser ────────────────────────────────
def parse_news_detail(html: str) -> dict:
    """
    Return {title, published_at, body_html, image_url}.
    Best-effort: IEBC's detail page structure varies across years.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove noise before extracting body
    for tag in soup.select(
        "script, style, nav, header, footer, aside, "
        ".sidebar, .widget, .breadcrumb, .comments, "
        ".share, .social, .related-posts"
    ):
        tag.decompose()
    # Drop HTML comments
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        c.extract()

    # Title
    title = ""
    for sel in ("h1.entry-title", ".post-title h1", "h1", "h2.entry-title"):
        el = soup.select_one(sel)
        if el:
            title = _clean(el.get_text())
            if title:
                break

    # Date — same style as listing
    published_at = None
    for sel in (".entry-date span", ".post-date span", "time", ".date"):
        el = soup.select_one(sel)
        if el:
            published_at = _parse_date(el.get_text())
            if published_at:
                break

    # Image (featured)
    image_url = ""
    for sel in (".post-thumb img", ".entry-header img", ".featured-image img",
                "article img", ".content img"):
        el = soup.select_one(sel)
        if el:
            image_url = _absolutize_image(el.get("src", ""))
            if image_url:
                break

    # Body — try the common containers, prefer whichever is largest
    body_html = ""
    candidates = (
        ".entry-content",
        ".post-content",
        ".article-content",
        "article .content",
        "main article",
    )
    best = None
    best_len = 0
    for sel in candidates:
        for el in soup.select(sel):
            inner = el.decode_contents()
            if len(inner) > best_len:
                best = inner
                best_len = len(inner)
    if best:
        body_html = best
    else:
        # Last-resort fallback
        main = soup.select_one("main") or soup.select_one(".main-content")
        if main:
            body_html = main.decode_contents()

    # Scrub body: remove leftover scripts/ads/hidden elements
    if body_html:
        body_soup = BeautifulSoup(body_html, "html.parser")
        for tag in body_soup.select(
            "script, style, iframe, form, input, button, "
            ".share, .social, .related, .ad, .advertisement"
        ):
            tag.decompose()
        # Absolutize any relative images / links
        for img in body_soup.select("img[src]"):
            img["src"] = _absolutize_image(img["src"])
        for a in body_soup.select("a[href]"):
            href = a["href"].strip()
            if href.startswith("/"):
                a["href"] = "https://www.iebc.or.ke" + href
        # Strip inline event handlers and style
        for el in body_soup.find_all(True):
            for attr in list(el.attrs):
                if attr.startswith("on") or attr in ("style", "class", "id"):
                    del el.attrs[attr]
        body_html = body_soup.decode_contents().strip()

    return {
        "title": title,
        "published_at": published_at,
        "image_url": image_url,
        "body_html": body_html,
    }