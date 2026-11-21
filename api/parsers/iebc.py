# api/parsers/iebc.py
"""
Parse the IEBC verification portal's HTML response.

The portal returns a full HTML page on both success and 'not found'.
We extract fields from the success page's table structure.

IEBC has no public JSON API for this. Every time they redesign the page,
this parser is what breaks — that's why `health_check()` exists and is
called by a daily cron.
"""
from bs4 import BeautifulSoup


# Fields we try to extract. Keys are the human-readable names we
# expect IEBC to display in the details table. Order matters — first
# match wins when multiple labels differ slightly.
FIELD_LABELS = {
    "full_name": ["full name", "names", "voter name", "name"],
    "id_number": ["id number", "document number", "id no", "passport no"],
    "year_of_birth": ["year of birth", "yob", "birth year"],
    "gender": ["sex", "gender"],
    "county": ["county"],
    "constituency": ["constituency"],
    "ward": ["ward"],
    "polling_station": ["polling station", "polling centre", "station"],
    "polling_code": ["polling station code", "station code"],
    "registration_centre": ["registration centre", "registration center"],
}


def _normalize_key(s: str) -> str:
    return (s or "").strip().lower().rstrip(":").strip()


def parse_iebc_response(html: str) -> dict | None:
    """
    Return a dict of voter fields on success, None on 'not found'.
    Raises ValueError if the page doesn't look like either shape
    (i.e. IEBC changed the layout — the health check will catch this).
    """
    if not html:
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Fast path: known success container (tweak selector when IEBC
    # confirms a stable id; for now we fall back to full-page scan).
    container = (
        soup.select_one("#voter-details")
        or soup.select_one(".voter-details")
        or soup.select_one(".card-body")
        or soup
    )

    text = container.get_text(" ", strip=True).lower()
    if (
        "not found" in text
        or "no record" in text
        or "no matching" in text
        or "could not find" in text
    ):
        return None

    fields = {}
    seen_labels = set()

    # Strategy 1: table rows with <th>/<td> or two-cell <tr>
    for row in container.select("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 2:
            continue
        raw_k = _normalize_key(cells[0].get_text(" ", strip=True))
        v = cells[1].get_text(" ", strip=True)
        if not raw_k or not v:
            continue
        for key, labels in FIELD_LABELS.items():
            if key in seen_labels:
                continue
            if any(raw_k.startswith(lb) for lb in labels):
                fields[key] = v
                seen_labels.add(key)
                break

    # Strategy 2: definition-list style <dl><dt>...<dd>...
    if not fields:
        for dl in container.select("dl"):
            dts = dl.find_all("dt")
            dds = dl.find_all("dd")
            for dt, dd in zip(dts, dds):
                raw_k = _normalize_key(dt.get_text(" ", strip=True))
                v = dd.get_text(" ", strip=True)
                if not raw_k or not v:
                    continue
                for key, labels in FIELD_LABELS.items():
                    if key in seen_labels:
                        continue
                    if any(raw_k.startswith(lb) for lb in labels):
                        fields[key] = v
                        seen_labels.add(key)
                        break

    # Strategy 3: labelled spans/divs (data-label="Full Name" ...)
    if not fields:
        for el in container.select("[data-label]"):
            raw_k = _normalize_key(el.get("data-label", ""))
            v = el.get_text(" ", strip=True)
            if not raw_k or not v:
                continue
            for key, labels in FIELD_LABELS.items():
                if key in seen_labels:
                    continue
                if any(raw_k.startswith(lb) for lb in labels):
                    fields[key] = v
                    seen_labels.add(key)
                    break

    # If we found at least a name or an ID, consider it a success.
    if fields.get("full_name") or fields.get("id_number"):
        # Normalize year_of_birth to integer if present
        if "year_of_birth" in fields:
            try:
                fields["year_of_birth"] = int(fields["year_of_birth"])
            except (ValueError, TypeError):
                pass
        return fields

    # No known shape → raise so the caller logs PARSE_ERROR (health
    # check will fail loudly).
    raise ValueError("IEBC response did not match any known layout")


def looks_like_not_found(html: str) -> bool:
    """Cheap heuristic used by the health check."""
    if not html:
        return False
    low = html.lower()
    return any(s in low for s in (
        "not found", "no record", "no matching", "could not find",
    ))