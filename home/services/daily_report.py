import hashlib
import json
import logging
import socket
from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from home.models import (
    Ward, KIEMSKit, DailyKIEMSEntry, DailyReportState, Phase,
    MovementSchedule, MovementScheduleState,
)

logger = logging.getLogger(__name__)


# ============================================================
# HELPERS — ward / kit submission state
# ============================================================

def _submitted_kit_ids(ward, phase, report_date):
    """
    Kits in this ward that have an explicit REGISTRATION submission for
    the given day. Includes honest 0/0/0 submissions, because the VRA
    still had to save the row — that's the signal "I'm done".
    """
    return set(
        DailyKIEMSEntry.objects
        .filter(
            ward=ward,
            phase=phase,
            entry_date=report_date,
            entry_type="REGISTRATION",
        )
        .values_list("kiems_kit_id", flat=True)
        .distinct()
    )


def _ward_is_submitted(ward, phase, report_date):
    """
    A ward is submitted only when EVERY active KIEMS kit assigned to it
    has reported for the given day.
    """
    active_kit_ids = set(
        KIEMSKit.objects
        .filter(ward=ward, status=True)
        .values_list("id", flat=True)
    )
    if not active_kit_ids:
        return True  # ward with no kits is trivially done

    return active_kit_ids.issubset(_submitted_kit_ids(ward, phase, report_date))


def constituency_kit_progress(constituency, report_date=None):
    """
    Returns {"total": N, "submitted": M} for the constituency on the given
    date. A kit is 'submitted' when it has a REGISTRATION entry for that
    date (explicit VRA submission, even at 0/0/0).
    """
    report_date = report_date or timezone.localdate()
    phase = Phase.objects.filter(active=True).first()
    if not phase:
        return {"total": 0, "submitted": 0}

    total = KIEMSKit.objects.filter(
        ward__constituency=constituency,
        ward__active=True,
        status=True,
    ).count()

    submitted = (
        DailyKIEMSEntry.objects
        .filter(
            ward__constituency=constituency,
            phase=phase,
            entry_date=report_date,
            entry_type="REGISTRATION",
        )
        .values("kiems_kit_id")
        .distinct()
        .count()
    )

    return {"total": total, "submitted": min(submitted, total)}


# ============================================================
# TRIGGER — called from submission views (never sends)
# ============================================================

def reevaluate_constituency_report(constituency, report_date=None):
    """
    Idempotent. Called after every submission. Never sends.
    Returns the resulting DailyReportState (or None if it can't evaluate).
    """
    report_date = report_date or timezone.localdate()
    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        return None

    wards = list(Ward.objects.filter(constituency=constituency, active=True))
    total_wards = len(wards)
    if total_wards == 0:
        return None

    submitted = sum(
        1 for w in wards
        if _ward_is_submitted(w, active_phase, report_date)
    )

    with transaction.atomic():
        state, _ = DailyReportState.objects.select_for_update().get_or_create(
            constituency=constituency,
            report_date=report_date,
            defaults={
                "total_wards": total_wards,
                "submitted_wards": submitted,
            },
        )

        state.total_wards = total_wards
        state.submitted_wards = submitted

        if submitted < total_wards:
            # Still partial — make sure we're not falsely READY
            if state.status != "SENT":
                state.status = "PENDING"
                state.ready_at = None
        else:
            # All wards in — transition to READY if not already sent
            if state.status in ("PENDING", "FAILED"):
                state.status = "READY"
                state.ready_at = timezone.now()
            # If already SENT/READY/SENDING, leave it alone

        state.save()

    return state


# ============================================================
# SENDER — daily registration report (the ONLY place that sends it)
# ============================================================

import re


# Matches 'KIT-1', 'KIT 1', 'KIT-12', 'KIT 7', etc.
# Deliberately does NOT match 'KIT-OFFICE ...' because the next
# character after 'KIT' must be a digit (after optional - or space).
_KIT_RE = re.compile(r"^\s*KIT[-\s]*(\d+)\s*$", re.IGNORECASE)
_OFFICE_RE = re.compile(r"\boffice\b", re.IGNORECASE)


def _is_office_kit(kit_name):
    """Office kits always print, even at 0/0."""
    return bool(_OFFICE_RE.search(kit_name or ""))


def _kit_number(kit_name):
    """
    Extract the number from a kit name.
      'KIT-1'    -> 1
      'KIT 7'    -> 7
      'KIT-13'   -> 13
      'KIT-OFFICE 001' -> None  (office kits have no KIT n)
      'Nyenyilel 3'    -> 3     (fallback: last integer in the name)
    Returns None if no number can be found.
    """
    if not kit_name:
        return None
    m = _KIT_RE.match(kit_name)
    if m:
        return int(m.group(1))
    nums = re.findall(r"\d+", kit_name)
    return int(nums[-1]) if nums else None
import re
from django.db.models import Sum
from home.models import DailyKIEMSEntry, Ward

_KIT_RE    = re.compile(r"^\s*KIT[-\s]*(\d+)\s*$", re.IGNORECASE)
_OFFICE_RE = re.compile(r"\boffice\b", re.IGNORECASE)


def _is_office_kit(name):
    return bool(_OFFICE_RE.search(name or ""))


def _kit_number(name):
    if not name:
        return None
    m = _KIT_RE.match(name)
    if m:
        return int(m.group(1))
    nums = re.findall(r"\d+", name)
    return int(nums[-1]) if nums else None


def build_daily_report_text(constituency, report_date):
    """
    Plain-text WhatsApp message for one constituency / one day.

    Layout:
        UASIN GISHU COUNTY
        AINABKOI SUB COUNTY
        DAILY REPORT PER KIT
        01/10/2026

        *KAPTAGAT WARD*
        KIT 1 - Registered......007
        KIT 2 - Registered......005

        *KAPSOYA WARD*
        KIT 1 - Registered......005
        ...
        TOTALS – 67
        MALE-  36
        FEMALE - 31

    Returns None if nothing to show.
    """
    entries = (
        DailyKIEMSEntry.objects
        .filter(
            ward__constituency=constituency,
            entry_date=report_date,
            entry_type="REGISTRATION",
        )
        .select_related("ward", "kiems_kit")
    )
    if not entries.exists():
        return None

    kit_rows = list(
        entries
        .values("ward_id", "ward__name", "kiems_kit_id", "kiems_kit__kit_name")
        .annotate(
            male=Sum("registered_male"),
            female=Sum("registered_female"),
            total=Sum("total_registered"),
            transferred=Sum("total_transferred"),
        )
    )

    printable = [
        r for r in kit_rows
        if (r["total"] or 0) > 0
        or (r["transferred"] or 0) > 0
        or _is_office_kit(r["kiems_kit__kit_name"])
    ]
    if not printable:
        return None

    wards = {}
    for r in printable:
        wards.setdefault(r["ward_id"], {"name": r["ward__name"], "kits": []})["kits"].append(r)

    def _key(row):
        n = _kit_number(row["kiems_kit__kit_name"])
        return (n if n is not None else 10 ** 9, row["kiems_kit__kit_name"] or "")

    for w in wards.values():
        w["kits"].sort(key=_key)

    grand_male   = sum(r["male"]   or 0 for r in printable)
    grand_female = sum(r["female"] or 0 for r in printable)
    grand_total  = sum(r["total"]  or 0 for r in printable)
    grand_trans  = sum(r["transferred"] or 0 for r in printable)

    county_name     = getattr(getattr(constituency, "county", None), "name", None)
    sub_county_name = getattr(constituency, "sub_county", None) or constituency.name

    lines = []
    if county_name:
        lines.append(f"{county_name.upper()} COUNTY")
    lines.append(f"{str(sub_county_name).upper()} SUB COUNTY")
    lines.append("DAILY REPORT PER KIT")
    lines.append(report_date.strftime("%d/%m/%Y"))

    for w in sorted(wards.values(), key=lambda x: x["name"]):
        lines.append("")
        lines.append(f"*{w['name'].upper()} WARD*")
        for r in w["kits"]:
            kit_name = r["kiems_kit__kit_name"] or ""
            total    = r["total"] or 0
            if _is_office_kit(kit_name):
                lines.append(f"OFFICE KIT – Registered … {total}")
            else:
                n = _kit_number(kit_name)
                label = f"KIT {n}" if n is not None else kit_name.upper()
                lines.append(f"{label} - Registered......{int(total):03d}")

    lines.append("")
    lines.append(f"TOTALS – {grand_total}")
    lines.append(f"MALE-  {grand_male}")
    lines.append(f"FEMALE - {grand_female}")
    if grand_trans:
        lines.append(f"Transferred: {grand_trans}")

    return "\n".join(lines)

def _build_grand_total_payload(state):
    """
    Build (message_text, data_hash) for a given DailyReportState.

    Output format (kit number taken from the kit's own name, which in
    this deployment already runs 1..18 across the whole constituency):

        UASIN GISHU COUNTY
        AINABKOI SUB COUNTY
        DAILY REPORT PER KIT
        01/10/2026

        *NGENYILEL WARD*
        KIT 1 - Registered......007
        KIT 2 - Registered......005
        KIT 3 - Registered......009

        *TAPSAGOI WARD*
        KIT 4 - Registered......005
        KIT 5 - Registered......014
        KIT 6 - Registered......010

        *KAMAGUT WARD*
        KIT 7 - Registered......011
        KIT 8 - Registered......006
        KIT 9 - Registered......004

        *KIPLOMBE WARD*
        KIT 10 - Registered......008
        KIT 11 - Registered......000
        KIT 12 - Registered......009

        *KAPSAOS WARD*
        KIT 13 - Registered......007
        KIT 14 - Registered......012
        KIT 15 - Registered......010

        *HURUMA WARD*
        KIT 16 - Registered......006
        KIT 17 - Registered......014
        KIT 18 - Registered......008

        *OFFICE WARD*
        OFFICE KIT – Registered … 0
        OFFICE KIT – Registered … 0

        TOTALS – 67
        MALE-  36
        FEMALE - 31

    Rules:
      - Ward name in bold above its kits.
      - KIT n comes from the kit's own trailing number, so 'KIT-1',
        'KIT 7', 'KIT-13' all normalise to 'KIT 1', 'KIT 7', 'KIT 13'.
      - Kits sorted numerically WITHIN each ward (1,2,3 then 4,5,6 ...).
      - Kits with 0 registered AND no transfers are dropped, EXCEPT
        office kits, which always print as 'OFFICE KIT – Registered … n'.
      - Wards with no printable kits are omitted entirely.
      - Returns (None, None) if there's nothing to report.
    """
    entries = (
        DailyKIEMSEntry.objects
        .filter(
            ward__constituency=state.constituency,
            entry_date=state.report_date,
            entry_type="REGISTRATION",
        )
        .select_related("ward", "kiems_kit")
    )

    if not entries.exists():
        return None, None

    # --- Per-kit aggregation -------------------------------------------
    kit_rows = list(
        entries
        .values(
            "ward_id",
            "ward__name",
            "kiems_kit_id",
            "kiems_kit__kit_name",
        )
        .annotate(
            male=Sum("registered_male"),
            female=Sum("registered_female"),
            total=Sum("total_registered"),
            transferred=Sum("total_transferred"),
        )
    )

    # --- Drop empty kits, keep office kits ------------------------------
    printable = []
    for r in kit_rows:
        total = r["total"] or 0
        trans = r["transferred"] or 0
        if total == 0 and trans == 0 and not _is_office_kit(r["kiems_kit__kit_name"]):
            continue
        printable.append(r)

    if not printable:
        return None, None

    # --- Group by ward, sort kits numerically within each ward ----------
    wards = {}
    for r in printable:
        wards.setdefault(r["ward_id"], {
            "name": r["ward__name"],
            "kits": [],
        })["kits"].append(r)

    def _sort_key(row):
        n = _kit_number(row["kiems_kit__kit_name"])
        return (n if n is not None else 10 ** 9, row["kiems_kit__kit_name"] or "")

    for w in wards.values():
        w["kits"].sort(key=_sort_key)

    # --- Constituency totals --------------------------------------------
    grand_male   = sum(r["male"]   or 0 for r in printable)
    grand_female = sum(r["female"] or 0 for r in printable)
    grand_total  = sum(r["total"]  or 0 for r in printable)
    grand_trans  = sum(r["transferred"] or 0 for r in printable)

    # --- Stable hash ----------------------------------------------------
    fingerprint_payload = {
        "c": state.constituency_id,
        "d": state.report_date.isoformat(),
        "t": int(grand_total),
        "m": int(grand_male),
        "f": int(grand_female),
        "kits": [
            (r["ward__name"], r["kiems_kit__kit_name"], int(r["total"] or 0))
            for r in printable
        ],
    }
    data_hash = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True).encode()
    ).hexdigest()

    # --- Header ---------------------------------------------------------
    constituency = state.constituency
    county_name     = getattr(getattr(constituency, "county", None), "name", None)
    sub_county_name = (
        getattr(constituency, "sub_county", None)
        or getattr(constituency, "name", None)
    )

    msg  = ""
    if county_name:
        msg += f"{county_name.upper()} COUNTY\n"
    msg += f"{str(sub_county_name).upper()} SUB COUNTY\n"
    msg += "DAILY REPORT PER KIT\n"
    msg += f"{state.report_date.strftime('%d/%m/%Y')}\n"

    # --- Ward blocks ----------------------------------------------------
    for w in sorted(wards.values(), key=lambda x: x["name"]):
        msg += f"\n*{w['name'].upper()} WARD*\n"

        for r in w["kits"]:
            kit_name = r["kiems_kit__kit_name"] or ""
            total    = r["total"] or 0

            if _is_office_kit(kit_name):
                msg += f"OFFICE KIT – Registered … {total}\n"
                continue

            n = _kit_number(kit_name)
            label = f"KIT {n}" if n is not None else kit_name.upper()
            msg += f"{label} - Registered......{int(total):03d}\n"

    # --- Footer ---------------------------------------------------------
    msg += f"\nTOTALS – {grand_total}\n"
    msg += f"MALE-  {grand_male}\n"
    msg += f"FEMALE - {grand_female}"

    if grand_trans:
        msg += f"\nTransferred: {grand_trans}"

    return msg, data_hash


def send_ready_reports(limit=20, max_attempts=5):
    """
    The ONLY function that actually sends daily grand totals.
    Safe to call from cron, a background thread, or manually.
    Idempotent by design (uses select_for_update + terminal SENT state).

    Returns: {"sent": int, "failed": int, "skipped": int}
    """
    from home.services.whatsapp import send_to_constituency

    now = timezone.now()
    hostname = socket.gethostname()

    # --- Lock a batch of READY states ---
    with transaction.atomic():
        ready_states = list(
            DailyReportState.objects
            .select_for_update(skip_locked=True)
            .filter(status="READY")
            .filter(attempts__lt=max_attempts)
            .order_by("ready_at")[:limit]
        )
        for s in ready_states:
            s.status = "SENDING"
            s.locked_at = now
            s.locked_by = hostname
            s.attempts = s.attempts + 1
            s.last_attempt_at = now
        if ready_states:
            DailyReportState.objects.bulk_update(
                ready_states,
                ["status", "locked_at", "locked_by", "attempts", "last_attempt_at"],
            )

    results = {"sent": 0, "failed": 0, "skipped": 0}

    # --- Send each one ---
    for state in ready_states:
        message, data_hash = _build_grand_total_payload(state)

        if not message:
            with transaction.atomic():
                s = DailyReportState.objects.select_for_update().get(pk=state.pk)
                s.status = "FAILED"
                s.last_error = "No registration data at send time"
                s.locked_at = None
                s.locked_by = ""
                s.save(update_fields=["status", "last_error", "locked_at", "locked_by"])
            results["skipped"] += 1
            continue

        # --- Route to the constituency's group ---
        ok, err, group_id = send_to_constituency(state.constituency, message)

        with transaction.atomic():
            s = DailyReportState.objects.select_for_update().get(pk=state.pk)
            s.locked_at = None
            s.locked_by = ""

            if ok:
                s.status = "SENT"
                s.sent_at = timezone.now()
                s.message_hash = data_hash
                s.last_error = ""
                results["sent"] += 1
            else:
                # Exhausted attempts — park as FAILED; otherwise retry next tick
                s.status = "FAILED" if s.attempts >= max_attempts else "READY"
                s.last_error = (
                        f"{err or 'Unknown error'}"
                        + (f" (group: {group_id})" if group_id else "")
                )
                results["failed"] += 1
            s.save()

    return results


# ============================================================
# MOVEMENT SCHEDULE — evaluation + sender
# ============================================================

def reevaluate_movement_schedule(constituency, schedule_date=None):
    """
    Mirror of `reevaluate_constituency_report` but for MovementSchedule.
    Called from the client submission endpoint after every save. Never sends.
    Returns the resulting MovementScheduleState (or None).
    """
    schedule_date = schedule_date or (timezone.localdate() + timedelta(days=1))

    if not constituency:
        return None

    total_wards = Ward.objects.filter(
        constituency=constituency, active=True
    ).count()
    if total_wards == 0:
        return None

    submitted_wards = (
        MovementSchedule.objects
        .filter(constituency=constituency, schedule_date=schedule_date)
        .values("ward_id")
        .distinct()
        .count()
    )

    with transaction.atomic():
        state, _ = MovementScheduleState.objects.select_for_update().get_or_create(
            constituency=constituency,
            schedule_date=schedule_date,
            defaults={
                "total_wards": total_wards,
                "submitted_wards": submitted_wards,
            },
        )

        state.total_wards = total_wards
        state.submitted_wards = submitted_wards

        if submitted_wards < total_wards:
            if state.status != "SENT":
                state.status = "PENDING"
                state.ready_at = None
        else:
            if state.status in ("PENDING", "FAILED"):
                state.status = "READY"
                state.ready_at = timezone.now()
            # If SENT/SENDING/READY, leave it alone

        state.save()

    return state


def _build_movement_payload(state):
    """
    Build (message_text, data_hash) for a MovementScheduleState.
    Returns (None, None) if no schedules exist.
    """
    schedules = list(
        MovementSchedule.objects
        .filter(
            constituency=state.constituency,
            schedule_date=state.schedule_date,
        )
        .select_related("ward", "kiems_kit")
        .order_by("ward__name", "kiems_kit__kit_name")
    )

    if not schedules:
        return None, None

    # Stable data hash
    fingerprint_payload = {
        "c": state.constituency_id,
        "d": state.schedule_date.isoformat(),
        "rows": [
            (s.ward.name, s.kiems_kit.kit_name, s.venue)
            for s in schedules
        ],
    }
    data_hash = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True).encode()
    ).hexdigest()

    msg = f"*{state.constituency.name.upper()} — TOMORROW'S MOVEMENT PLAN*\n"
    msg += f"_{state.schedule_date.strftime('%d %b %Y')}_\n"
    msg += "------------------------------\n"

    current_ward = None
    for s in schedules:
        if s.ward.name != current_ward:
            current_ward = s.ward.name
            msg += f"\n*{current_ward}*\n"
        msg += f"  {s.kiems_kit.kit_name}: {s.venue}\n"

    msg += "------------------------------\n"
    msg += f"_{len(schedules)} kit(s) scheduled across {state.submitted_wards}/{state.total_wards} wards_"

    return msg, data_hash


def send_ready_movement_reports(limit=20, max_attempts=5):
    """
    The ONLY function that sends the grand movement-schedule report.
    Safe to call from cron or manually. Idempotent.

    Returns: {"sent": int, "failed": int, "skipped": int}
    """
    from home.services.whatsapp import send_to_constituency

    now = timezone.now()
    hostname = socket.gethostname()

    with transaction.atomic():
        ready_states = list(
            MovementScheduleState.objects
            .select_for_update(skip_locked=True)
            .filter(status="READY")
            .filter(attempts__lt=max_attempts)
            .order_by("ready_at")[:limit]
        )
        for s in ready_states:
            s.status = "SENDING"
            s.attempts = s.attempts + 1
            s.save(update_fields=["status", "attempts"])

    results = {"sent": 0, "failed": 0, "skipped": 0}

    for state in ready_states:
        message, data_hash = _build_movement_payload(state)

        if not message:
            with transaction.atomic():
                s = MovementScheduleState.objects.select_for_update().get(pk=state.pk)
                s.status = "FAILED"
                s.last_error = "No movement schedules at send time"
                s.save(update_fields=["status", "last_error"])
            results["skipped"] += 1
            continue

        # Dedupe: if the exact same content already went out, mark SENT
        # without re-sending (protects against re-eval loops).
        if state.message_hash and state.message_hash == data_hash and state.sent_at:
            with transaction.atomic():
                s = MovementScheduleState.objects.select_for_update().get(pk=state.pk)
                s.status = "SENT"
                s.save(update_fields=["status"])
            results["skipped"] += 1
            continue

        ok, err, group_id = send_to_constituency(state.constituency, message)

        with transaction.atomic():
            s = MovementScheduleState.objects.select_for_update().get(pk=state.pk)
            if ok:
                s.status = "SENT"
                s.sent_at = timezone.now()
                s.message_hash = data_hash
                s.last_error = ""
                results["sent"] += 1
            else:
                s.status = "FAILED" if s.attempts >= max_attempts else "READY"
                s.last_error = (
                        f"{err or 'Unknown error'}"
                        + (f" (group: {group_id})" if group_id else "")
                )
                results["failed"] += 1
            s.save()

    return results


# ============================================================
# REAPER — heals stuck SENDING rows (both daily + movement)
# ============================================================

def reap_stuck_sending_states(stale_after_minutes=10):
    """
    Any SENDING row older than stale_after_minutes is returned to READY
    so the next send tick can retry it. Handles workers that crashed
    mid-send. Applies to BOTH daily and movement state tables.
    """
    cutoff = timezone.now() - timedelta(minutes=stale_after_minutes)

    daily_reaped = DailyReportState.objects.filter(
        status="SENDING",
        locked_at__lt=cutoff,
    ).update(
        status="READY",
        locked_at=None,
        locked_by="",
        last_error="Reaped stale SENDING lock",
    )

    # MovementScheduleState has no locked_at field — use updated_at instead
    movement_reaped = MovementScheduleState.objects.filter(
        status="SENDING",
        updated_at__lt=cutoff,
    ).update(
        status="READY",
        last_error="Reaped stale SENDING lock",
    )

    return daily_reaped + movement_reaped


# ============================================================
# ONE-CALL TICK — used by cron / management command / button
# ============================================================

def run_daily_report_tick(max_send=20, max_movement_send=20):
    """
    Reap stale SENDING states, then attempt to send any READY ones
    for BOTH daily registration reports AND movement schedules.
    Safe to call repeatedly (idempotent).

    max_send / max_movement_send cap how many reports are sent per tick —
    useful on Vercel's serverless timeouts. Split so a backlog of one
    workflow can't starve the other.
    """
    reaped = reap_stuck_sending_states(stale_after_minutes=10)

    daily_summary = send_ready_reports(limit=max_send)
    movement_summary = send_ready_movement_reports(limit=max_movement_send)

    return {
        "reaped": reaped,
        "daily": daily_summary,
        "movement": movement_summary,
        # Top-level convenience keys (backward-compatible with existing callers)
        "sent": daily_summary["sent"] + movement_summary["sent"],
        "failed": daily_summary["failed"] + movement_summary["failed"],
        "skipped": daily_summary["skipped"] + movement_summary["skipped"],
    }