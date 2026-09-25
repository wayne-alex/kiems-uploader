"""
Movement-schedule message formatting + thin WhatsApp dispatch helpers.

State machine (reevaluate / send_ready) and the reaper live in
`home.services.daily_report` — this module is only responsible for
turning a MovementSchedule / MovementScheduleState into text and
handing that text to the WhatsApp bot, while logging the attempt.
"""

import hashlib

from home.models import (
    MovementSchedule,
    MovementScheduleLog,
)
from home.services.whatsapp import send_to_constituency


# ============================================================
# MESSAGE FORMATTERS
# ============================================================

def format_single_movement_message(schedule, is_update=False):
    """
    Message sent when a VRA/Clerk submits or edits one kit's venue.
    Uses the plain-ASCII "MOVEMENT CONFIRMED / UPDATED" prefix so it
    renders cleanly in WhatsApp regardless of client emoji support.
    """
    prefix = "MOVEMENT UPDATED" if is_update else "MOVEMENT CONFIRMED"
    return (
        f"{schedule.ward.name.upper()} {prefix}\n"
        f"{schedule.kiems_kit.kit_name} ({schedule.kiems_kit.serial_no})\n"
        f"Venue: {schedule.venue}\n"
        f"Date: {schedule.schedule_date.strftime('%d %b %Y')}"
    )
def _sanitize(text: str) -> str:
    """Strip WhatsApp markdown control chars from user-entered text."""
    return (text or "").replace("*", "").replace("_", "").replace("~", "").replace("`", "").strip()

import re
from collections import defaultdict


def _kit_sort_key(kit_name: str):
    """
    Extract the numeric part of a kit name for natural ordering,
    regardless of formatting ("KIT 16", "KIT-10", "KIT-1", etc.).
    Falls back to a high number + the raw string so unparseable
    names sort last instead of raising.
    """
    match = re.search(r"(\d+)", kit_name or "")
    return (int(match.group(1)) if match else 10_000_000, kit_name or "")


def format_grand_movement_message(constituency, schedule_date):
    """
    Grand report: 'Kits Movement Schedule', kits numbered and ordered
    by the numeric part of their own kit_name. Wards are ordered by
    the lowest kit number they contain (so the ward with kits 1-3
    appears first, the ward with 16-18 appears last), not
    alphabetically. The OFFICE ward is excluded entirely since its
    venue is always the office.
    """
    schedules = list(
        MovementSchedule.objects
        .filter(constituency=constituency, schedule_date=schedule_date)
        .exclude(ward__name__iexact="OFFICE")
        .select_related("ward", "kiems_kit")
    )

    # Precompute each schedule's kit number, then find the lowest
    # kit number per ward to decide ward ordering.
    ward_min_kit = defaultdict(lambda: (10_000_001, ""))
    for s in schedules:
        key = _kit_sort_key(s.kiems_kit.kit_name)
        if key < ward_min_kit[s.ward.name]:
            ward_min_kit[s.ward.name] = key

    schedules.sort(
        key=lambda s: (
            ward_min_kit[s.ward.name],           # ward order: by its lowest kit number
            _kit_sort_key(s.kiems_kit.kit_name),  # kit order within the ward
        )
    )

    county_name = (
        constituency.county.name
        if getattr(constituency, "county", None)
        else "County"
    )

    header_lines = [
        f"*{county_name} County — {constituency.name} Constituency*",
        "*Kits Movement Schedule*",
        f"*{schedule_date.strftime('%d/%m/%Y')}*",
        "",
    ]

    if not schedules:
        return "\n".join(header_lines + ["_No venues have been scheduled yet._"])

    body_lines = []
    current_ward = None

    for s in schedules:
        if s.ward.name != current_ward:
            if current_ward is not None:
                body_lines.append("")  # blank line between wards
            current_ward = s.ward.name
            body_lines.append(f"*{current_ward} Ward*")

        venue = _sanitize(s.venue)
        if venue and not venue.endswith("."):
            venue += "."

        kit_num, _ = _kit_sort_key(s.kiems_kit.kit_name)
        body_lines.append(f"• Kit {kit_num}: {venue}")

    return "\n".join(header_lines + body_lines)


# ============================================================
# PUBLIC ALIASES (used by ICT views)
# ============================================================

def build_grand_message(constituency, schedule_date):
    """Read-only wrapper used by the ICT 'Preview Grand' endpoint."""
    return format_grand_movement_message(constituency, schedule_date)


# ============================================================
# THIN SEND WRAPPERS (log the attempt, never raise)
# ============================================================

def _hash(msg: str) -> str:
    return hashlib.sha256(msg.encode("utf-8")).hexdigest()


def _log_attempt(
    *,
    constituency,
    schedule_date,
    kind,
    message,
    sent_ok,
    error="",
    group_id="",
    group_name="",
    ward=None,
    kiems_kit=None,
    sent_by=None,
):
    """Write a MovementScheduleLog row. Swallows its own errors so
    logging can never break the caller's flow."""
    try:
        MovementScheduleLog.objects.create(
            constituency=constituency,
            schedule_date=schedule_date,
            kind=kind,
            ward=ward,
            kiems_kit=kiems_kit,
            group_id=group_id or "",
            group_name=group_name or "",
            message=message,
            sent_ok=sent_ok,
            error=error or "",
            sent_by=sent_by,
        )
    except Exception as e:  # pragma: no cover — defensive
        print(f"[movement] Failed to write MovementScheduleLog: {e}")


def send_single_movement_message(schedule, user=None):
    """
    Send the per-kit confirmation for a freshly-saved or edited schedule.
    Fire-and-forget: returns (ok, err) but never raises.
    """
    is_update = (schedule.edit_count or 0) > 0
    msg = format_single_movement_message(schedule, is_update=is_update)

    try:
        ok, err, group_id = send_to_constituency(schedule.constituency, msg)
    except Exception as e:
        ok, err, group_id = False, str(e), ""

    _log_attempt(
        constituency=schedule.constituency,
        schedule_date=schedule.schedule_date,
        kind="PER_KIT",
        message=msg,
        sent_ok=ok,
        error=err or "",
        group_id=group_id or "",
        ward=schedule.ward,
        kiems_kit=schedule.kiems_kit,
        sent_by=user,
    )
    return ok, err


def send_grand_movement_message(state, user=None):
    """
    Send the grand movement report for a MovementScheduleState.
    Caller is responsible for flipping state.status → SENDING first if
    they want strict locking; this function just sends + logs.

    Idempotent: if an identical message (by hash) was already sent
    successfully for this constituency/date, skip the network call.

    Returns (ok, err).
    """
    msg = format_grand_movement_message(state.constituency, state.schedule_date)
    msg_hash = _hash(msg)

    # Idempotency guard — same content already delivered?
    already = MovementScheduleLog.objects.filter(
        constituency=state.constituency,
        schedule_date=state.schedule_date,
        kind="GRAND",
        sent_ok=True,
        message=msg,
    ).exists()
    if already:
        return True, "already sent (identical content)"

    try:
        ok, err, group_id = send_to_constituency(state.constituency, msg)
    except Exception as e:
        ok, err, group_id = False, str(e), ""

    _log_attempt(
        constituency=state.constituency,
        schedule_date=state.schedule_date,
        kind="GRAND",
        message=msg,
        sent_ok=ok,
        error=err or "",
        group_id=group_id or "",
        sent_by=user,
    )
    return ok, err