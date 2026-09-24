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

def _build_grand_total_payload(state):
    """
    Build (message_text, data_hash) for a given DailyReportState.
    Returns (None, None) if there's nothing to report.
    """
    entries = (
        DailyKIEMSEntry.objects
        .filter(
            ward__constituency=state.constituency,
            entry_date=state.report_date,
            entry_type="REGISTRATION",
        )
        .select_related("ward")
    )

    if not entries.exists():
        return None, None

    totals = entries.aggregate(
        male=Sum("registered_male"),
        female=Sum("registered_female"),
        total=Sum("total_registered"),
        transferred=Sum("total_transferred"),
    )
    by_ward = list(
        entries.values("ward__name").annotate(
            male=Sum("registered_male"),
            female=Sum("registered_female"),
            total=Sum("total_registered"),
        ).order_by("ward__name")
    )

    # Stable hash of the underlying data — same numbers => same hash
    fingerprint_payload = {
        "c": state.constituency_id,
        "d": state.report_date.isoformat(),
        "t": int(totals["total"] or 0),
        "m": int(totals["male"] or 0),
        "f": int(totals["female"] or 0),
        "w": [(w["ward__name"], int(w["total"] or 0)) for w in by_ward],
    }
    data_hash = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True).encode()
    ).hexdigest()

    msg = f"*{state.constituency.name.upper()} — DAILY REPORT*\n"
    msg += f"_{state.report_date.strftime('%d %b %Y')}_\n"
    msg += "------------------------------\n"
    for w in by_ward:
        msg += f"{w['ward__name']}: M {w['male'] or 0}  F {w['female'] or 0}  = {w['total'] or 0}\n"
    msg += "------------------------------\n"
    msg += f"*TOTAL:* M {totals['male'] or 0}  F {totals['female'] or 0}  = *{totals['total'] or 0}*"
    if totals["transferred"]:
        msg += f"\nTransferred: {totals['transferred']}"
    msg += f"\n\n_{state.submitted_wards}/{state.total_wards} wards submitted_"

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