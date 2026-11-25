import hmac
import io
import json
import logging
import os
import time
from datetime import datetime, timedelta

import requests
from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command, CommandError
from django.db import models, transaction
from django.db.models import Sum, Count
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from home.models import (
    DailyKIEMSEntry,
    Device,
    KIEMSKit,
    MovementSchedule,
    Phase,
    VRA,
    Ward,
    RegistrationCentre,
)
from home.services.daily_report import reevaluate_constituency_report
from home.services.form_a_sync import sync_form_a_for_entry
from home.services.whatsapp_queue import enqueue_whatsapp, drain_async
from .models import (
    Announcement,
    Incident,
    PushSubscription,
    SyncEvent,
    FormAReceipt,
    FormAUsage,
    FormASpoilage,
    FormAAllocation,
    VoterVerification,
)
from .parsers.iebc import parse_iebc_response
from .utils import with_idempotency

# ═══════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════
IEBC_VERIFY_URL = getattr(settings, "IEBC_VERIFY_URL", "https://verify.iebc.or.ke/verify")
IEBC_CACHE_SECONDS = getattr(settings, "IEBC_CACHE_SECONDS", 60 * 60)
IEBC_TIMEOUT = getattr(settings, "IEBC_TIMEOUT", 12)
VOTER_LOOKUP_ENABLED = getattr(settings, "VOTER_LOOKUP_ENABLED", True)
VOTER_LOOKUP_RL_PER_MIN = getattr(settings, "VOTER_LOOKUP_RL_PER_MIN", 30)


# ═══════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════

def _json_body(request):
    try:
        return json.loads(request.body.decode() or "{}")
    except Exception:
        return {}


def _fingerprint(request, payload=None):
    fp = (
            request.headers.get("X-Device-Fingerprint")
            or (payload or {}).get("fingerprint")
            or request.POST.get("fingerprint")
            or ""
    )
    request.fingerprint = fp
    return fp


def _ok(data, status=200):
    return status, {"ok": True, **data}


def _err(msg, status=400, **extra):
    return status, {"ok": False, "error": msg, **extra}


def _resolve_device(fp):
    if not fp:
        return None
    return Device.objects.filter(fingerprint=fp).first()


def _resolve_vra(fp):
    if not fp:
        return None
    return VRA.objects.filter(device_fingerprint=fp).first()


def _resolve_phase(entry_date, explicit_id=None):
    if explicit_id:
        phase = Phase.objects.filter(pk=explicit_id).first()
        if phase:
            return phase
    return (
        Phase.objects
        .filter(start_date__lte=entry_date, end_date__gte=entry_date, active=True)
        .order_by("-start_date")
        .first()
    )


# ═══════════════════════════════════════════════════════════
# ENTRY SUBMIT — fast path, WhatsApp deferred
# ═══════════════════════════════════════════════════════════

@csrf_exempt
@require_POST
def submit_entries(request):
    """POST /api/kiems/submit-entries/

    DB-only write path. WhatsApp messages are queued and sent by the
    every-minute cron (cron_drain_whatsapp).
    """
    _fingerprint(request)

    status, body, replayed = with_idempotency(
        request, "entry.submit", lambda: _submit_entries_impl(request),
    )
    if replayed:
        body = {**body, "replayed": True}

    return JsonResponse(body, status=status)

def _submit_entries_impl(request):
    """The actual work for POST /kiems/submit-entries/.

    Returns (status_code, body_dict) for with_idempotency.
    All validation happens BEFORE any write, so a bad row can never
    leave earlier rows half-saved.
    """
    fingerprint = request.POST.get("fingerprint") or ""
    date_str = request.POST.get("date") or ""
    kit_ids = request.POST.getlist("kit_id[]") or request.POST.getlist("kit_id")
    venues = request.POST.getlist("venue[]") or request.POST.getlist("venue")
    males = request.POST.getlist("registered_male[]") or request.POST.getlist("registered_male")
    females = request.POST.getlist("registered_female[]") or request.POST.getlist("registered_female")

    if not fingerprint or not date_str or not kit_ids:
        return _err("fingerprint, date and kit_id[] are required.")

    if not (len(kit_ids) == len(venues) == len(males) == len(females)):
        return _err(
            "Mismatched list lengths across kit_id[], venue[], "
            "registered_male[], registered_female[]."
        )

    try:
        entry_date = datetime.fromisoformat(date_str).date()
    except Exception:
        return _err("Invalid date (expected YYYY-MM-DD).")

    device = _resolve_device(fingerprint)
    if not device:
        return _err("Device not registered.", 403)
    if not device.is_burned:
        return _err("Device not authorized.", 403)

    vra = _resolve_vra(fingerprint)
    if not vra:
        return _err("VRA not resolved for this device.", 403)

    phase = _resolve_phase(entry_date)
    if not phase:
        return _err("No active phase for that date.", 409)

    try:
        kit_pks = [int(x) for x in kit_ids]
    except ValueError:
        return _err("kit_id[] must be integers.")

    kits = {k.pk: k for k in KIEMSKit.objects.select_related("ward__constituency").filter(pk__in=kit_pks)}
    missing = [pk for pk in kit_pks if pk not in kits]
    if missing:
        return _err(f"Unknown kit id(s): {missing}", 404)

    for pk in kit_pks:
        if kits[pk].ward_id != vra.ward_id:
            return _err(f"Kit {pk} does not belong to your ward.", 403)

    # ---- Validate every row up front ----
    rows = []
    for kit_id, venue, m, f in zip(kit_pks, venues, males, females):
        try:
            male = int(m or 0)
            female = int(f or 0)
        except ValueError:
            return _err(f"Non-numeric count for kit {kit_id}.")
        if male < 0 or female < 0:
            return _err(f"Negative count for kit {kit_id}.")
        venue_clean = (venue or "").strip()
        if not venue_clean:
            return _err(f"Venue required for kit {kit_id}.")
        rows.append((kit_id, venue_clean, male, female))

    # ---- Commit phase: DB writes + Form A reconciliation, one transaction ----
    saved = []  # (entry, created, previous_total)
    form_a_warnings = []

    with transaction.atomic():
        for kit_id, venue_clean, male, female in rows:
            existing = DailyKIEMSEntry.objects.filter(
                kiems_kit_id=kit_id,
                phase=phase,
                entry_date=entry_date,
                vra=vra,
            ).first()
            previous_total = existing.total_registered if existing else 0

            obj, created = DailyKIEMSEntry.objects.update_or_create(
                kiems_kit_id=kit_id,
                phase=phase,
                entry_date=entry_date,
                vra=vra,
                defaults={
                    "ward": kits[kit_id].ward,
                    "entry_type": "REGISTRATION",
                    "venue": venue_clean,
                    "registered_male": male,
                    "registered_female": female,
                    "total_registered": male + female,
                },
            )
            saved.append((obj, created, previous_total))

            # Savepoint so a Form A DB error can't poison the outer transaction.
            try:
                with transaction.atomic():
                    result = sync_form_a_for_entry(obj, previous_total, acting_vra=vra)
                if not result.ok and result.warning:
                    form_a_warnings.append({
                        "kit_id": kit_id,
                        "kit_name": kits[kit_id].kit_name,
                        "warning": result.warning,
                    })
            except Exception as e:
                logger.exception("Form A sync failed for kit %s", kit_id)
                form_a_warnings.append({
                    "kit_id": kit_id,
                    "kit_name": kits[kit_id].kit_name,
                    "warning": f"Form A sync failed: {e}",
                })

    # ---- Re-evaluate the daily report state (never sends) ----
    seen = set()
    for entry, _created, _prev in saved:
        c = entry.ward.constituency
        if c and c.pk not in seen:
            seen.add(c.pk)
            try:
                reevaluate_constituency_report(c, entry_date)
            except Exception:
                logger.exception("reevaluate failed for constituency %s", c.pk)

    # ---- Notify phase: queue WhatsApp, drained in background ----
    for entry, created, previous_total in saved:
        total = entry.total_registered or 0
        if total <= 0:
            continue
        message = _format_entry_message(
            entry, is_update=not created, previous_total=previous_total,
        )
        if message:
            dedupe_key = f"entry:{entry.ward.constituency_id}:{entry.kiems_kit_id}:{entry.entry_date}"
            enqueue_whatsapp(
                constituency=entry.ward.constituency,
                message=message,
                kind="entry.submit",
                dedupe_key=dedupe_key,
            )

    body = {"saved": len(saved)}
    if form_a_warnings:
        body["form_a_warnings"] = form_a_warnings
    return _ok(body)


def _format_entry_message(entry, is_update=False, previous_total=0):
    """WhatsApp text for a single registration entry."""
    total = (entry.registered_male or 0) + (entry.registered_female or 0)
    if total == 0:
        return None
    label = "UPDATED" if is_update else "CONFIRMED"
    msg = f"{entry.ward.name.upper()} {label}\n"
    msg += (
        f"{entry.kiems_kit.kit_name}:"
        f"Male:{entry.registered_male} Female:{entry.registered_female} ={total}"
    )
    if is_update:
        msg += f"\n(Previous total: {previous_total})"
    if entry.total_transferred and entry.total_transferred > 0:
        msg += f"\nTransferred: {entry.total_transferred}"
    return msg


def _format_entry_message(entry):
    """WhatsApp confirmation text for a single registration entry."""
    total = (entry.registered_male or 0) + (entry.registered_female or 0)
    if total == 0:
        return None
    msg = f"{entry.ward.name.upper()} CONFIRMED ✓\n"
    msg += (
        f"{entry.kiems_kit.kit_name}:"
        f"Male:{entry.registered_male} Female:{entry.registered_female} ={total}"
    )
    if entry.total_transferred and entry.total_transferred > 0:
        msg += f"\nTransferred: {entry.total_transferred}"
    return msg


# ═══════════════════════════════════════════════════════════
# MOVEMENT SAVE
# ═══════════════════════════════════════════════════════════

@csrf_exempt
@require_POST
def movement_save(request):
    """POST /api/movement/save/ — idempotent."""
    payload = _json_body(request)
    _fingerprint(request, payload)

    status, body, replayed = with_idempotency(
        request, "movement.save", lambda: _movement_save_impl(request, payload),
    )
    if replayed:
        body = {**body, "replayed": True}
    return JsonResponse(body, status=status)


def _movement_save_impl(request, payload):
    ward_id = payload.get("ward_id")
    fingerprint = payload.get("fingerprint") or ""
    schedule_date_str = payload.get("schedule_date")
    entries = payload.get("entries") or []

    if not ward_id or not schedule_date_str or not entries:
        return _err("ward_id, schedule_date and entries are required.")

    try:
        schedule_date = datetime.fromisoformat(schedule_date_str).date()
    except Exception:
        return _err("Invalid schedule_date (expected YYYY-MM-DD).")

    ward = Ward.objects.filter(pk=ward_id).first()
    if not ward:
        return _err("Ward not found.", 404)

    device = _resolve_device(fingerprint)
    if not device:
        return _err("Device not registered.", 403)
    if not device.is_burned:
        return _err("Device not authorized.", 403)

    vra = _resolve_vra(fingerprint)

    phase = _resolve_phase(schedule_date)
    if not phase:
        return _err("No active phase for that date.", 409)

    constituency = ward.constituency
    if not constituency:
        return _err("Ward has no constituency set.", 409)

    try:
        kit_pks = [int(e.get("kit_id")) for e in entries if e.get("kit_id")]
    except (TypeError, ValueError):
        return _err("Each entry needs an integer kit_id.")

    kits = {k.pk: k for k in KIEMSKit.objects.filter(pk__in=kit_pks)}
    for pk in kit_pks:
        if pk not in kits:
            return _err(f"Unknown kit id {pk}.", 404)
        if kits[pk].ward_id != ward.id:
            return _err(f"Kit {pk} does not belong to ward {ward.id}.", 403)

    created = 0
    updated = 0
    with transaction.atomic():
        for e in entries:
            kit_id = e.get("kit_id")
            venue = (e.get("venue") or "").strip()
            notes = (e.get("notes") or "").strip()
            if not kit_id or not venue:
                continue

            obj, was_created = MovementSchedule.objects.update_or_create(
                kiems_kit_id=kit_id,
                schedule_date=schedule_date,
                defaults={
                    "ward": ward,
                    "constituency": constituency,
                    "phase": phase,
                    "vra": vra,
                    "venue": venue[:200],
                    "notes": notes[:255],
                },
            )
            if was_created:
                created += 1
            else:
                obj.edit_count = (obj.edit_count or 0) + 1
                obj.save(update_fields=["edit_count", "updated_at"])
                updated += 1

    return _ok({"created": created, "updated": updated})


# ═══════════════════════════════════════════════════════════
# VOTER LOOKUP (IEBC proxy)
# ═══════════════════════════════════════════════════════════

@csrf_exempt
@require_POST
def verify_voter(request):
    """POST /api/verify-voter/ — server-side proxy to IEBC portal."""
    if not VOTER_LOOKUP_ENABLED:
        return JsonResponse(
            {"ok": False, "error": "Voter lookup is temporarily disabled."},
            status=503,
        )

    payload = _json_body(request)
    fp = _fingerprint(request, payload)
    id_number = (payload.get("id_number") or "").strip()
    year_of_birth = payload.get("year_of_birth")

    if not fp or not id_number or not year_of_birth:
        return JsonResponse(
            {"ok": False, "error": "fingerprint, id_number and year_of_birth are required."},
            status=400,
        )

    device = _resolve_device(fp)
    if not device:
        return JsonResponse({"ok": False, "error": "Device not registered."}, status=403)
    if not device.is_burned:
        return JsonResponse({"ok": False, "error": "Device not authorized."}, status=403)
    vra = _resolve_vra(fp)

    # Rate limit per device
    rl_key = f"voter_rl:{fp}"
    hits = cache.get(rl_key, [])
    now = time.time()
    hits = [t for t in hits if now - t < 60]
    if len(hits) >= VOTER_LOOKUP_RL_PER_MIN:
        return JsonResponse(
            {"ok": False, "error": "Slow down — too many lookups in the last minute."},
            status=429,
        )
    hits.append(now)
    cache.set(rl_key, hits, 60)

    if not id_number.isalnum() or not (5 <= len(id_number) <= 20):
        return JsonResponse(
            {"ok": False, "error": "ID number should be 5–20 alphanumeric characters."},
            status=400,
        )
    try:
        yob = int(year_of_birth)
    except (ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "Year of birth must be a number."}, status=400)
    if yob < 1900 or yob > timezone.now().year:
        return JsonResponse({"ok": False, "error": "Year of birth looks wrong."}, status=400)

    cache_key = f"voter:{id_number}:{yob}"
    cached = cache.get(cache_key)
    if cached:
        VoterVerification.objects.create(
            vra=vra, device=device,
            id_number=id_number, year_of_birth=yob,
            result="CACHED", payload=cached,
        )
        return JsonResponse({"ok": True, "cached": True, "voter": cached})

    started = time.time()
    try:
        resp = requests.post(
            IEBC_VERIFY_URL,
            data={"idno": id_number, "yob": str(yob)},
            headers={
                "User-Agent": "IEBC-KIEMS-Field/2.0 (+https://verify.iebc.or.ke/)",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-GB,en;q=0.9",
            },
            timeout=IEBC_TIMEOUT,
            allow_redirects=True,
        )
    except requests.RequestException as exc:
        VoterVerification.objects.create(
            vra=vra, device=device,
            id_number=id_number, year_of_birth=yob,
            result="NETWORK_ERROR", error=str(exc),
            duration_ms=int((time.time() - started) * 1000),
        )
        return JsonResponse(
            {"ok": False, "error": "IEBC portal unreachable. Try again shortly."},
            status=502,
        )

    duration = int((time.time() - started) * 1000)

    if resp.status_code != 200:
        VoterVerification.objects.create(
            vra=vra, device=device,
            id_number=id_number, year_of_birth=yob,
            result="HTTP_ERROR", error=f"HTTP {resp.status_code}",
            duration_ms=duration,
        )
        return JsonResponse(
            {"ok": False, "error": f"IEBC returned HTTP {resp.status_code}."},
            status=502,
        )

    try:
        voter = parse_iebc_response(resp.text)
    except ValueError as exc:
        VoterVerification.objects.create(
            vra=vra, device=device,
            id_number=id_number, year_of_birth=yob,
            result="PARSE_ERROR", error=str(exc),
            duration_ms=duration,
        )
        return JsonResponse(
            {"ok": False, "error": "Could not read IEBC response. Contact ICT."},
            status=502,
        )

    if voter is None:
        VoterVerification.objects.create(
            vra=vra, device=device,
            id_number=id_number, year_of_birth=yob,
            result="NOT_FOUND",
            duration_ms=duration,
        )
        return JsonResponse(
            {"ok": False, "error": "No voter found for those details."},
            status=404,
        )

    cache.set(cache_key, voter, IEBC_CACHE_SECONDS)
    VoterVerification.objects.create(
        vra=vra, device=device,
        id_number=id_number, year_of_birth=yob,
        result="OK", payload=voter,
        duration_ms=duration,
    )
    return JsonResponse({"ok": True, "voter": voter, "duration_ms": duration})


@require_GET
def voter_lookup_health(request):
    """GET /api/voter-lookup/health/ — cheap liveness check."""
    if not VOTER_LOOKUP_ENABLED:
        return JsonResponse({"ok": True, "enabled": False})

    if request.GET.get("ping_iebc") == "true":
        try:
            resp = requests.post(
                IEBC_VERIFY_URL,
                data={"idno": "00000000", "yob": "1990"},
                headers={"User-Agent": "IEBC-KIEMS-Health/1.0"},
                timeout=8,
            )
            if resp.status_code != 200:
                return JsonResponse(
                    {"ok": False, "error": f"IEBC HTTP {resp.status_code}"}, status=503,
                )
            try:
                parse_iebc_response(resp.text)
            except ValueError as exc:
                return JsonResponse(
                    {"ok": False, "error": f"parser mismatch: {exc}"}, status=503,
                )
        except requests.RequestException as exc:
            return JsonResponse(
                {"ok": False, "error": f"IEBC unreachable: {exc}"}, status=503,
            )

    return JsonResponse({"ok": True, "enabled": True, "url": IEBC_VERIFY_URL})


# ═══════════════════════════════════════════════════════════
# INCIDENTS
# ═══════════════════════════════════════════════════════════

@csrf_exempt
@require_POST
def submit_incident(request):
    """POST /api/incidents/ — multipart/form-data."""
    _fingerprint(request)

    def do():
        kind = request.POST.get("kind")
        severity = request.POST.get("severity", "MEDIUM")
        ward_id = request.POST.get("ward_id")

        if not kind or not ward_id:
            return _err("kind and ward_id are required.")

        ward = Ward.objects.filter(pk=ward_id).first()
        if not ward:
            return _err("Ward not found.", 404)

        kit = None
        if request.POST.get("kit_id"):
            kit = KIEMSKit.objects.filter(pk=request.POST["kit_id"]).first()

        device = _resolve_device(request.fingerprint)
        vra = _resolve_vra(request.fingerprint)

        def _float(name):
            v = request.POST.get(name)
            try:
                return float(v) if v not in (None, "") else None
            except (ValueError, TypeError):
                return None

        inc = Incident.objects.create(
            kind=kind,
            severity=severity,
            ward=ward,
            constituency=ward.constituency,
            kiems_kit=kit,
            vra=vra,
            device=device,
            description=(request.POST.get("description") or "").strip(),
            latitude=_float("lat"),
            longitude=_float("lng"),
            accuracy_m=_float("accuracy"),
            photo=request.FILES.get("photo"),
            client_op_id=request.POST.get("op_id", ""),
        )

        return _ok({"incident_id": str(inc.id), "has_photo": bool(inc.photo)})

    status, body, _ = with_idempotency(request, "incident.submit", do)
    return JsonResponse(body, status=status)


# ═══════════════════════════════════════════════════════════
# ANNOUNCEMENTS
# ═══════════════════════════════════════════════════════════

@require_GET
def list_announcements(request):
    """GET /api/announcements/?ward_id=&since=ISO&page=1&page_size=10"""
    ward_id = request.GET.get("ward_id")
    since = request.GET.get("since")

    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = int(request.GET.get("page_size", 10))
    except (TypeError, ValueError):
        page_size = 10
    page_size = max(1, min(page_size, 30))

    qs = Announcement.objects.all()

    if ward_id:
        ward = Ward.objects.filter(pk=ward_id).first()
        if ward:
            qs = qs.filter(
                models.Q(ward=ward)
                | models.Q(constituency=ward.constituency, ward__isnull=True)
                | models.Q(constituency__isnull=True, ward__isnull=True)
            )

    if since:
        try:
            dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            qs = qs.filter(created_at__gt=dt)
        except Exception:
            pass

    now = timezone.now()
    qs = qs.filter(models.Q(expires_at__isnull=True) | models.Q(expires_at__gt=now))

    total = qs.count()
    offset = (page - 1) * page_size
    items = []
    for a in qs[offset:offset + page_size]:
        items.append({
            "id": str(a.id),
            "title": a.title,
            "body_excerpt": (a.body or "")[:180],
            "body_html_present": bool(a.body_html),
            "image_url": a.image_url or "",
            "source": a.source,
            "is_iebc": a.source == "IEBC",
            "priority": a.priority,
            "published_at": (
                a.published_at.isoformat()
                if a.published_at else a.created_at.date().isoformat()
            ),
            "created_at": a.created_at.isoformat(),
        })

    has_more = offset + page_size < total
    return JsonResponse({
        "ok": True,
        "total": total,
        "page": page,
        "page_size": page_size,
        "has_more": has_more,
        "next_page": page + 1 if has_more else None,
        "announcements": items,
    })


@require_GET
def announcement_detail(request, announcement_id):
    """GET /api/announcements/<uuid>/"""
    a = Announcement.objects.filter(pk=announcement_id).first()
    if not a:
        return JsonResponse({"ok": False, "error": "Not found."}, status=404)

    return JsonResponse({
        "ok": True,
        "id": str(a.id),
        "title": a.title,
        "body_html": a.body_html or "",
        "body_excerpt": a.body or "",
        "image_url": a.image_url or "",
        "source": a.source,
        "is_iebc": a.source == "IEBC",
        "external_url": a.external_url or "",
        "priority": a.priority,
        "published_at": (a.published_at.isoformat()
                         if a.published_at else a.created_at.date().isoformat()),
        "created_at": a.created_at.isoformat(),
    })


# ═══════════════════════════════════════════════════════════
# PUSH SUBSCRIBE + SYNC EVENT
# ═══════════════════════════════════════════════════════════

@csrf_exempt
@require_POST
def push_subscribe(request):
    payload = _json_body(request)
    fp = _fingerprint(request, payload)
    if not fp or not payload.get("endpoint"):
        return JsonResponse(
            {"ok": False, "error": "fingerprint and endpoint required."},
            status=400,
        )

    device = _resolve_device(fp)
    if not device:
        return JsonResponse({"ok": False, "error": "Device not registered."}, status=403)

    keys = payload.get("keys") or {}
    sub, _created = PushSubscription.objects.update_or_create(
        endpoint=payload["endpoint"],
        defaults={
            "device": device,
            "fingerprint": fp,
            "p256dh": keys.get("p256dh", ""),
            "auth": keys.get("auth", ""),
            "user_agent": payload.get("user_agent", ""),
            "active": True,
        },
    )
    return JsonResponse({"ok": True, "subscription_id": str(sub.id)})


@csrf_exempt
@require_POST
def sync_event(request):
    payload = _json_body(request)
    fp = _fingerprint(request, payload)
    SyncEvent.objects.create(
        fingerprint=fp or "",
        sent=payload.get("sent", 0),
        failed=payload.get("failed", 0),
        still_queued=payload.get("still_queued", 0),
        duration_ms=payload.get("duration_ms", 0),
        app_version=payload.get("app_version", ""),
    )
    return JsonResponse({"ok": True})


# ═══════════════════════════════════════════════════════════
# CSRF
# ═══════════════════════════════════════════════════════════

@require_GET
@csrf_exempt
def csrf(request):
    from django.middleware.csrf import get_token
    token = get_token(request)
    return JsonResponse({"ok": True, "csrfToken": token})


# ═══════════════════════════════════════════════════════════
# STATS
# ═══════════════════════════════════════════════════════════

@require_GET
def stats_day(request):
    """GET /api/stats/day/?fingerprint=&date=YYYY-MM-DD — ward-scoped daily."""
    fp = request.GET.get("fingerprint")
    if not fp:
        return JsonResponse({"ok": False, "error": "fingerprint required"}, status=400)

    device = _resolve_device(fp)
    if not device:
        return JsonResponse({"ok": False, "error": "Device not registered."}, status=403)
    if not device.is_burned:
        return JsonResponse({"ok": False, "error": "Device not authorized."}, status=403)

    vra = _resolve_vra(fp)
    if not vra or not vra.ward:
        return JsonResponse({"ok": False, "error": "VRA not bound to a ward."}, status=404)

    ward = vra.ward

    date_str = request.GET.get("date")
    try:
        target_date = (
            datetime.strptime(date_str, "%Y-%m-%d").date()
            if date_str else timezone.localdate()
        )
    except (ValueError, TypeError):
        target_date = timezone.localdate()

    yesterday = target_date - timedelta(days=1)
    week_start = target_date - timedelta(days=6)

    today_qs = DailyKIEMSEntry.objects.filter(ward=ward, entry_date=target_date).aggregate(
        male=Sum("registered_male"),
        female=Sum("registered_female"),
        total=Sum("total_registered"),
        kits=Count("kiems_kit", distinct=True),
    )

    today_male = int(today_qs["male"] or 0)
    today_female = int(today_qs["female"] or 0)
    today_total = int(today_qs["total"] or 0)
    today_kits = int(today_qs["kits"] or 0)

    y_total = (
            DailyKIEMSEntry.objects
            .filter(ward=ward, entry_date=yesterday)
            .aggregate(total=Sum("total_registered"))["total"] or 0
    )
    y_total = int(y_total)

    delta_pct = 0.0
    if y_total > 0:
        delta_pct = round(((today_total - y_total) / y_total) * 100, 1)

    week_rows = (
        DailyKIEMSEntry.objects
        .filter(ward=ward, entry_date__gte=week_start, entry_date__lte=target_date)
        .values("entry_date")
        .annotate(total=Sum("total_registered"))
        .order_by("entry_date")
    )
    week_by_date = {row["entry_date"].isoformat(): int(row["total"] or 0) for row in week_rows}

    last_7_days = []
    for i in range(6, -1, -1):
        d = target_date - timedelta(days=i)
        iso = d.isoformat()
        last_7_days.append({"date": iso, "total": week_by_date.get(iso, 0)})

    kits_total = KIEMSKit.objects.filter(ward=ward, status=True).count()

    return JsonResponse({
        "ok": True,
        "date": target_date.isoformat(),
        "ward_id": ward.id,
        "ward_name": ward.name,
        "constituency_name": ward.constituency.name if ward.constituency else None,
        "today": {
            "male": today_male,
            "female": today_female,
            "total": today_total,
            "kits_recorded": today_kits,
            "kits_total": kits_total,
        },
        "yesterday": {"total": y_total, "delta_pct": delta_pct},
        "last_7_days": last_7_days,
    })


# ═══════════════════════════════════════════════════════════
# REFERENCE (training / help)
# ═══════════════════════════════════════════════════════════

from .reference import REFERENCE


@require_GET
def reference_manifest(request):
    category = request.GET.get("category")
    items = []
    for slug, doc in REFERENCE.items():
        if category and doc["category"] != category:
            continue
        items.append({
            "slug": doc["slug"],
            "title": doc["title"],
            "category": doc["category"],
            "summary": doc.get("summary", ""),
            "updated": doc.get("updated", ""),
        })
    items.sort(key=lambda x: x["title"])
    return JsonResponse({"ok": True, "docs": items})


@require_GET
def reference_doc(request, slug):
    doc = REFERENCE.get(slug)
    if not doc:
        return JsonResponse({"ok": False, "error": "Not found."}, status=404)
    return JsonResponse({
        "ok": True,
        "slug": doc["slug"],
        "title": doc["title"],
        "category": doc["category"],
        "summary": doc.get("summary", ""),
        "updated": doc.get("updated", ""),
        "body_markdown": doc["body_markdown"],
    })


# ═══════════════════════════════════════════════════════════
# STATIONS
# ═══════════════════════════════════════════════════════════

@require_GET
def stations_search(request):
    from django.db.models import Q

    q = (request.GET.get("q") or "").strip()
    ward_id = request.GET.get("ward_id")

    try:
        limit = max(1, min(int(request.GET.get("limit", 200)), 500))
    except (TypeError, ValueError):
        limit = 200
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except (TypeError, ValueError):
        page = 1

    qs = RegistrationCentre.objects.filter(active=True).select_related(
        "ward", "ward__constituency",
    )
    if ward_id:
        qs = qs.filter(ward_id=ward_id)

    if q:
        qs = qs.filter(Q(name__istartswith=q) | Q(name__icontains=q)).order_by(
            models.Case(
                models.When(name__istartswith=q, then=0),
                default=1,
                output_field=models.IntegerField(),
            ),
            "name",
        )
    else:
        qs = qs.order_by("name")

    total = qs.count()
    offset = (page - 1) * limit
    slice_qs = qs[offset:offset + limit]

    items = []
    for c in slice_qs:
        items.append({
            "id": c.id,
            "code": c.code,
            "full_code": c.full_code,
            "name": c.name,
            "ward_id": c.ward_id,
            "ward_name": c.ward.name,
            "constituency_name": (
                c.ward.constituency.name if c.ward.constituency else None
            ),
            "has_location": c.latitude is not None,
        })

    has_more = offset + limit < total
    return JsonResponse({
        "ok": True,
        "stations": items,
        "count": len(items),
        "total": total,
        "page": page,
        "page_size": limit,
        "has_more": has_more,
        "next_page": page + 1 if has_more else None,
    })


# ═══════════════════════════════════════════════════════════
# FORM A / ACKNOWLEDGMENT
# ═══════════════════════════════════════════════════════════

FORM_A_SPOIL_REASONS = [
    ("TORN", "Torn"),
    ("MISPRINT", "Misprinter"),
    ("WRONG_ENTRY", "Wrong entry"),
    ("WATER_DAMAGE", "Water damage"),
    ("OTHER", "Other"),
]


@csrf_exempt
@require_POST
def form_a_receive(request):
    """POST /api/form-a/receive/ — ICT records a receipt at the office."""
    payload = _json_body(request)
    fp = _fingerprint(request, payload)

    def do():
        form_type = payload.get("form_type")
        ward_id = payload.get("ward_id")
        phase_id = payload.get("phase_id")

        if form_type not in dict(FormAReceipt.TYPE_CHOICES):
            return _err("form_type must be FORM_A or ACKNOWLEDGMENT.")
        if not ward_id or not phase_id:
            return _err("ward_id and phase_id are required.")

        ward = Ward.objects.filter(pk=ward_id).first()
        if not ward:
            return _err("Ward not found.", 404)
        phase = Phase.objects.filter(pk=phase_id).first()
        if not phase:
            return _err("Phase not found.", 404)

        serial_start = (payload.get("serial_start") or "").strip()
        serial_end = (payload.get("serial_end") or "").strip()

        if form_type == FormAReceipt.ACK:
            try:
                booklet_count = int(payload.get("booklet_count"))
            except (TypeError, ValueError):
                return _err("booklet_count must be an integer.")
            if booklet_count < 1:
                return _err("booklet_count must be at least 1.")
            if not serial_start or not serial_end:
                return _err("Acknowledgment booklets require serial_start and serial_end.")
            form_count = booklet_count * FormAReceipt.ACK_FORMS_PER_BOOKLET
        else:
            booklet_count = None
            try:
                form_count = int(payload.get("form_count"))
            except (TypeError, ValueError):
                return _err("form_count must be an integer.")
            if form_count < 1:
                return _err("form_count must be at least 1.")
            if bool(serial_start) ^ bool(serial_end):
                return _err("Provide both serial_start and serial_end, or neither.")

        user = request.user if request.user.is_authenticated else None

        receipt = FormAReceipt.objects.create(
            form_type=form_type,
            phase=phase,
            ward=ward,
            form_count=form_count,
            booklet_count=booklet_count,
            serial_start=serial_start,
            serial_end=serial_end,
            received_by=user,
            note=(payload.get("note") or "").strip(),
            client_op_id=payload.get("op_id", ""),
        )
        return _ok({
            "receipt_id": str(receipt.id),
            "form_count": receipt.form_count,
            "booklet_count": receipt.booklet_count,
        })

    status, body, _ = with_idempotency(request, "form_a.receive", do)
    return JsonResponse(body, status=status)


@csrf_exempt
@require_POST
def form_a_usage(request):
    """POST /api/form-a/usage/ — record forms consumed on a date."""
    payload = _json_body(request)
    fp = _fingerprint(request, payload)

    def do():
        receipt_id = payload.get("receipt_id")
        entry_date = payload.get("date")
        forms_used = payload.get("forms_used")

        if not receipt_id or not entry_date or forms_used is None:
            return _err("receipt_id, date and forms_used are required.")

        receipt = FormAReceipt.objects.filter(pk=receipt_id).first()
        if not receipt:
            return _err("Receipt not found.", 404)

        try:
            d = datetime.fromisoformat(entry_date).date()
        except Exception:
            return _err("Invalid date.")

        try:
            n = int(forms_used)
        except (TypeError, ValueError):
            return _err("forms_used must be an integer.")
        if n < 1:
            return _err("forms_used must be at least 1.")
        if n > receipt.remaining:
            return _err(f"Only {receipt.remaining} forms remaining on this receipt.")

        device = _resolve_device(fp)
        vra = _resolve_vra(fp)

        kiems_kit = None
        if payload.get("kit_id"):
            kiems_kit = KIEMSKit.objects.filter(pk=payload["kit_id"]).first()

        usage = FormAUsage.objects.create(
            receipt=receipt,
            kind=FormAUsage.KIND_USED,
            phase=receipt.phase,
            ward=receipt.ward,
            kiems_kit=kiems_kit,
            vra=vra,
            device=device,
            entry_date=d,
            forms_used=n,
            note=(payload.get("note") or "").strip(),
            client_op_id=payload.get("op_id", ""),
        )
        return _ok({"usage_id": str(usage.id), "remaining": receipt.remaining})

    status, body, _ = with_idempotency(request, "form_a.usage", do)
    return JsonResponse(body, status=status)


@csrf_exempt
@require_POST
def form_a_spoil(request):
    """POST /api/form-a/spoil/ — ACK: per serial; FORM_A: count only."""
    payload = _json_body(request)
    fp = _fingerprint(request, payload)

    def do():
        receipt_id = payload.get("receipt_id")
        entry_date = payload.get("date")

        if not receipt_id or not entry_date:
            return _err("receipt_id and date are required.")

        receipt = FormAReceipt.objects.filter(pk=receipt_id).first()
        if not receipt:
            return _err("Receipt not found.", 404)

        try:
            d = datetime.fromisoformat(entry_date).date()
        except Exception:
            return _err("Invalid date.")

        device = _resolve_device(fp)
        vra = _resolve_vra(fp)
        reason = payload.get("reason", "OTHER")

        if receipt.form_type == FormAReceipt.ACK:
            serial = (payload.get("serial_number") or "").strip()
            if not serial:
                return _err("serial_number is required for acknowledgment booklets.")
            if FormASpoilage.objects.filter(receipt=receipt, serial_number=serial).exists():
                return _err(f"Serial {serial} is already marked spoiled on this receipt.", 409)

            sp = FormASpoilage.objects.create(
                receipt=receipt,
                phase=receipt.phase,
                ward=receipt.ward,
                vra=vra,
                device=device,
                serial_number=serial,
                entry_date=d,
                reason=reason,
                note=(payload.get("note") or "").strip(),
                client_op_id=payload.get("op_id", ""),
            )
            return _ok({"spoilage_id": str(sp.id), "remaining": receipt.remaining})

        else:
            try:
                n = int(payload.get("count"))
            except (TypeError, ValueError):
                return _err("count is required for Form A spoilage.")
            if n < 1:
                return _err("count must be at least 1.")
            if n > receipt.remaining:
                return _err(f"Only {receipt.remaining} forms remaining on this receipt.")

            usage = FormAUsage.objects.create(
                receipt=receipt,
                kind=FormAUsage.KIND_SPOILED,
                spoil_reason=reason,
                phase=receipt.phase,
                ward=receipt.ward,
                vra=vra,
                device=device,
                entry_date=d,
                forms_used=n,
                note=(payload.get("note") or "").strip(),
                client_op_id=payload.get("op_id", ""),
            )
            return _ok({"usage_id": str(usage.id), "remaining": receipt.remaining})

    status, body, _ = with_idempotency(request, "form_a.spoil", do)
    return JsonResponse(body, status=status)


@require_GET
def form_a_summary(request):
    """GET /api/form-a/summary/?ward_id=&phase_id="""
    ward_id = request.GET.get("ward_id")
    phase_id = request.GET.get("phase_id")

    qs = FormAReceipt.objects.all()
    if ward_id:
        qs = qs.filter(ward_id=ward_id)
    if phase_id:
        qs = qs.filter(phase_id=phase_id)

    items = []
    for r in qs.select_related("ward", "phase"):
        items.append({
            "receipt_id": str(r.id),
            "form_type": r.form_type,
            "form_type_label": r.get_form_type_display(),
            "ward_name": r.ward.name,
            "phase_name": r.phase.name,
            "form_count": r.form_count,
            "booklet_count": r.booklet_count,
            "serial_start": r.serial_start,
            "serial_end": r.serial_end,
            "used": int(r.used_count),
            "spoiled": int(r.spoiled_count),
            "remaining": int(r.remaining),
            "received_at": r.received_at.isoformat(),
        })

    return JsonResponse({"ok": True, "receipts": items})


@require_GET
def form_a_daily(request):
    """GET /api/form-a/daily/?ward_id=&date=YYYY-MM-DD"""
    ward_id = request.GET.get("ward_id")
    date_str = request.GET.get("date")
    try:
        target = (
            datetime.strptime(date_str, "%Y-%m-%d").date()
            if date_str else timezone.localdate()
        )
    except (TypeError, ValueError):
        target = timezone.localdate()

    usage_qs = FormAUsage.objects.filter(entry_date=target).select_related(
        "receipt", "kiems_kit", "vra",
    )
    spoil_qs = FormASpoilage.objects.filter(entry_date=target).select_related("receipt")
    if ward_id:
        usage_qs = usage_qs.filter(ward_id=ward_id)
        spoil_qs = spoil_qs.filter(ward_id=ward_id)

    usages = [{
        "id": str(u.id),
        "receipt_id": str(u.receipt_id),
        "form_type": u.receipt.form_type,
        "kind": u.kind,
        "forms_used": u.forms_used,
        "spoil_reason": u.spoil_reason,
        "kit_name": u.kiems_kit.kit_name if u.kiems_kit else None,
        "vra_name": str(u.vra) if u.vra else None,
        "note": u.note,
    } for u in usage_qs]

    spoilages = [{
        "id": str(s.id),
        "receipt_id": str(s.receipt_id),
        "serial_number": s.serial_number,
        "reason": s.get_reason_display(),
        "note": s.note,
    } for s in spoil_qs]

    return JsonResponse({
        "ok": True,
        "date": target.isoformat(),
        "usages": usages,
        "spoilages": spoilages,
    })


# ── VRA-facing Form A endpoints ──

@require_GET
def form_a_mine(request):
    """GET /api/form-a/mine/?fingerprint= — active serialed receipts in ward."""
    fp = request.GET.get("fingerprint")
    if not fp:
        return JsonResponse({"ok": False, "error": "fingerprint required"}, status=400)

    device = _resolve_device(fp)
    if not device:
        return JsonResponse({"ok": False, "error": "Device not registered."}, status=403)
    if not device.is_burned:
        return JsonResponse({"ok": False, "error": "Device not authorized."}, status=403)

    vra = _resolve_vra(fp)
    if not vra or not vra.ward_id:
        return JsonResponse({"ok": False, "error": "VRA not bound to a ward."}, status=404)

    qs = (
        FormAReceipt.objects
        .filter(ward_id=vra.ward_id, is_archived=False)
        .filter(
            models.Q(form_type=FormAReceipt.ACK)
            | models.Q(form_type=FormAReceipt.FORM_A, serial_start__gt="")
        )
        .select_related("phase")
        .order_by("-received_at")
    )

    items = []
    for r in qs:
        remaining = r.remaining
        if remaining <= 0:
            continue
        items.append({
            "receipt_id": str(r.id),
            "form_type": r.form_type,
            "form_type_label": r.get_form_type_display(),
            "phase_name": r.phase.name,
            "serial_start": r.serial_start,
            "serial_end": r.serial_end,
            "remaining": remaining,
            "received_at": r.received_at.isoformat(),
        })

    return JsonResponse({"ok": True, "receipts": items})


@require_GET
def form_a_kits_for_ward(request):
    """GET /api/form-a/kits/?fingerprint= — kits in VRA's ward."""
    fp = request.GET.get("fingerprint")
    if not fp:
        return JsonResponse({"ok": False, "error": "fingerprint required"}, status=400)

    device = _resolve_device(fp)
    if not device:
        return JsonResponse({"ok": False, "error": "Device not registered."}, status=403)
    if not device.is_burned:
        return JsonResponse({"ok": False, "error": "Device not authorized."}, status=403)

    vra = _resolve_vra(fp)
    if not vra or not vra.ward_id:
        return JsonResponse({"ok": False, "error": "VRA not bound to a ward."}, status=404)

    kits = KIEMSKit.objects.filter(ward_id=vra.ward_id, status=True).order_by("kit_name")
    return JsonResponse({
        "ok": True,
        "kits": [{
            "kit_id": k.id,
            "kit_name": k.kit_name,
            "serial_no": k.serial_no,
        } for k in kits],
    })


@require_GET
def form_a_kits_ledger(request):
    """GET /api/form-a/kits-ledger/?fingerprint=

    Per-kit Form A + Acknowledgment balance for the VRA's ward.

    For each active kit, returns:
      - allocated  : SUM(FormAAllocation.forms_allocated)
      - used       : SUM(FormAUsage where kind=USED)
      - spoiled    : COUNT(spoilages) + SUM(FormAUsage where kind=SPOILED)
      - left       : allocated - used - spoiled

    Also returns the ward's unallocated pool (forms received but not
    assigned to any kit), so the screen can show:
      "Pool: 150 forms unassigned"
    """
    fp = request.GET.get("fingerprint")
    if not fp:
        return JsonResponse({"ok": False, "error": "fingerprint required"}, status=400)

    device = _resolve_device(fp)
    if not device:
        return JsonResponse({"ok": False, "error": "Device not registered."}, status=403)
    if not device.is_burned:
        return JsonResponse({"ok": False, "error": "Device not authorized."}, status=403)

    vra = _resolve_vra(fp)
    if not vra or not vra.ward_id:
        return JsonResponse({"ok": False, "error": "VRA not bound to a ward."}, status=404)

    ward = vra.ward
    phase = _resolve_phase(timezone.localdate())
    if not phase:
        return JsonResponse({"ok": False, "error": "No active phase."}, status=409)

    kits = list(
        KIEMSKit.objects
        .filter(ward=ward, status=True)
        .order_by("kit_name")
    )

    # Bulk aggregate — three group-bys, one query each
    allocated_by_kit = dict(
        FormAAllocation.objects
        .filter(
            receipt__ward=ward, receipt__phase=phase,
            receipt__is_archived=False,
            kiems_kit__in=kits,
        )
        .values_list("kiems_kit_id")
        .annotate(total=Sum("forms_allocated"))
    )

    used_by_kit = dict(
        FormAUsage.objects
        .filter(
            ward=ward, phase=phase,
            kind=FormAUsage.KIND_USED,
            kiems_kit__in=kits,
        )
        .values_list("kiems_kit_id")
        .annotate(total=Sum("forms_used"))
    )

    spoiled_count_by_kit = dict(
        FormASpoilage.objects
        .filter(ward=ward, phase=phase, kiems_kit__in=kits)
        .values_list("kiems_kit_id")
        .annotate(total=Count("id"))
    )
    spoiled_usage_by_kit = dict(
        FormAUsage.objects
        .filter(
            ward=ward, phase=phase,
            kind=FormAUsage.KIND_SPOILED,
            kiems_kit__in=kits,
        )
        .values_list("kiems_kit_id")
        .annotate(total=Sum("forms_used"))
    )

    rows = []
    for kit in kits:
        allocated = allocated_by_kit.get(kit.id, 0) or 0
        used = used_by_kit.get(kit.id, 0) or 0
        spoiled = (spoiled_count_by_kit.get(kit.id, 0) or 0) \
                  + (spoiled_usage_by_kit.get(kit.id, 0) or 0)
        left = max(0, allocated - used - spoiled)
        rows.append({
            "kit_id": kit.id,
            "kit_name": kit.kit_name,
            "serial_no": kit.serial_no,
            "allocated": allocated,
            "used": used,
            "spoiled": spoiled,
            "left": left,
        })

    # Ward-level unallocated pool: forms received but not on any kit
    total_received = (
            FormAReceipt.objects
            .filter(ward=ward, phase=phase, is_archived=False)
            .aggregate(s=Sum("form_count"))["s"] or 0
    )
    total_allocated = sum(allocated_by_kit.values()) or 0
    total_spoiled_ward = (
        FormASpoilage.objects.filter(ward=ward, phase=phase).count()
    )
    pool = max(0, total_received - total_allocated - total_spoiled_ward)

    return JsonResponse({
        "ok": True,
        "ward_id": ward.id,
        "ward_name": ward.name,
        "phase_id": phase.id,
        "phase_name": phase.name,
        "pool": pool,
        "total_received": total_received,
        "total_allocated": total_allocated,
        "kits": rows,
    })


@require_GET
def form_a_spoilages_mine(request):
    """GET /api/form-a/spoilages-mine/?fingerprint=&date=YYYY-MM-DD"""
    fp = request.GET.get("fingerprint")
    if not fp:
        return JsonResponse({"ok": False, "error": "fingerprint required"}, status=400)

    vra = _resolve_vra(fp)
    if not vra:
        return JsonResponse({"ok": False, "error": "VRA not resolved."}, status=404)

    date_str = request.GET.get("date")
    try:
        target = datetime.fromisoformat(date_str).date() if date_str else timezone.localdate()
    except Exception:
        target = timezone.localdate()

    qs = (
        FormASpoilage.objects
        .filter(vra=vra, entry_date=target)
        .select_related("receipt", "kiems_kit")
        .order_by("-created_at")
    )

    return JsonResponse({
        "ok": True,
        "date": target.isoformat(),
        "spoilages": [{
            "id": str(s.id),
            "receipt_id": str(s.receipt_id),
            "kit_name": s.kiems_kit.kit_name if s.kiems_kit else None,
            "serial_number": s.serial_number,
            "reason": s.reason,
            "reason_label": s.get_reason_display(),
            "note": s.note,
            "created_at": s.created_at.isoformat(),
        } for s in qs],
    })


@csrf_exempt
@require_POST
def form_a_report_spoil(request):
    """POST /api/form-a/report-spoil/ — VRA reports a spoiled serial."""
    payload = _json_body(request)
    fp = _fingerprint(request, payload)

    def do():
        receipt_id = payload.get("receipt_id")
        serial = (payload.get("serial_number") or "").strip()
        date_str = payload.get("date")
        reason = payload.get("reason", "OTHER")
        note = (payload.get("note") or "").strip()
        op_id = payload.get("op_id", "")
        kit_id = payload.get("kit_id")

        if not receipt_id or not serial or not date_str:
            return _err("receipt_id, serial_number and date are required.")
        if reason not in dict(FORM_A_SPOIL_REASONS):
            return _err("Invalid reason.")

        receipt = FormAReceipt.objects.filter(pk=receipt_id, is_archived=False).first()
        if not receipt:
            return _err("Receipt not found.", 404)

        vra = _resolve_vra(fp)
        if not vra or not vra.ward_id:
            return _err("VRA not resolved for this device.", 403)
        if receipt.ward_id != vra.ward_id:
            return _err("This receipt belongs to another ward.", 403)

        device = _resolve_device(fp)
        if not device:
            return _err("Device not registered.", 403)
        if not device.is_burned:
            return _err("Device not authorized.", 403)

        kiems_kit = None
        if kit_id:
            kiems_kit = KIEMSKit.objects.filter(pk=kit_id, ward_id=vra.ward_id).first()

        try:
            entry_date = datetime.fromisoformat(date_str).date()
        except Exception:
            return _err("Invalid date (expected YYYY-MM-DD).")

        if receipt.serial_start and receipt.serial_end:
            try:
                sn, lo, hi = int(serial), int(receipt.serial_start), int(receipt.serial_end)
                if not (lo <= sn <= hi):
                    return _err(
                        f"Serial {serial} is not in the range "
                        f"{receipt.serial_start}–{receipt.serial_end}.",
                        409,
                    )
            except (TypeError, ValueError):
                if not (receipt.serial_start <= serial <= receipt.serial_end):
                    return _err(
                        f"Serial {serial} is not in the range "
                        f"{receipt.serial_start}–{receipt.serial_end}.",
                        409,
                    )

        if FormASpoilage.objects.filter(receipt=receipt, serial_number=serial).exists():
            return _err(f"Serial {serial} is already marked spoiled on this receipt.", 409)

        if receipt.remaining <= 0:
            return _err("This receipt has no remaining forms to spoil.", 409)

        sp = FormASpoilage.objects.create(
            receipt=receipt,
            kiems_kit=kiems_kit,
            phase=receipt.phase,
            ward=receipt.ward,
            vra=vra,
            device=device,
            serial_number=serial,
            entry_date=entry_date,
            reason=reason,
            note=note,
            client_op_id=op_id,
        )
        return _ok({"spoilage_id": str(sp.id), "remaining": receipt.remaining})

    status, body, _ = with_idempotency(request, "form_a.spoil", do)
    return JsonResponse(body, status=status)


@csrf_exempt
@require_POST
def form_a_allocate(request):
    """POST /api/form-a/allocate/ ? VRA distributes booklets to a kit."""
    payload = _json_body(request)
    fp = _fingerprint(request, payload)

    def do():
        receipt_id = payload.get("receipt_id")
        kit_id = payload.get("kit_id")
        forms = payload.get("forms_allocated")
        note = (payload.get("note") or "").strip()

        if not receipt_id or not kit_id or forms is None:
            return _err("receipt_id, kit_id and forms_allocated are required.")

        try:
            forms = int(forms)
        except (TypeError, ValueError):
            return _err("forms_allocated must be an integer.")

        if forms < 1:
            return _err("forms_allocated must be at least 1.")

        receipt = FormAReceipt.objects.filter(
            pk=receipt_id,
            is_archived=False
        ).first()

        if not receipt:
            return _err("Receipt not found.", 404)

        vra = _resolve_vra(fp)
        if not vra or not vra.ward_id:
            return _err("VRA not resolved.", 403)

        if receipt.ward_id != vra.ward_id:
            return _err("Receipt belongs to another ward.", 403)

        device = _resolve_device(fp)
        if not device:
            return _err("Device not registered.", 403)

        if not device.is_burned:
            return _err("Device not authorized.", 403)

        kit = KIEMSKit.objects.filter(
            pk=kit_id,
            ward_id=vra.ward_id,
            status=True
        ).first()

        if not kit:
            return _err("Kit not found in your ward.", 404)

        if (
                receipt.form_type == FormAReceipt.ACK
                and forms % 50 != 0
        ):
            return _err(
                f"Acknowledgment allocations must be multiples of 50 (got {forms}).",
                409,
            )

        with transaction.atomic():

            # Lock the receipt to prevent concurrent allocations
            receipt = (
                FormAReceipt.objects
                .select_for_update()
                .get(pk=receipt.pk)
            )

            # Lock and retrieve an existing allocation, if present
            allocation = (
                FormAAllocation.objects
                .select_for_update()
                .filter(
                    receipt=receipt,
                    kiems_kit=kit,
                )
                .first()
            )

            previous = (
                allocation.forms_allocated
                if allocation else 0
            )

            other_allocated = (
                    receipt.allocations
                    .exclude(kiems_kit=kit)
                    .aggregate(s=Sum("forms_allocated"))["s"] or 0
            )

            already_used = (
                    other_allocated
                    + previous
                    + receipt.spoiled_count
            )

            available = (
                    receipt.form_count
                    - other_allocated
                    - previous
                    - receipt.spoiled_count
            )

            if already_used + forms > receipt.form_count:
                return _err(
                    f"Only {available} forms available on this receipt "
                    f"(already {already_used} allocated/spoiled "
                    f"of {receipt.form_count}).",
                    409,
                )

            if allocation:
                # ADD to existing allocation instead of replacing it
                allocation.forms_allocated += forms
                allocation.vra = vra
                allocation.device = device
                allocation.note = note or allocation.note
                allocation.client_op_id = payload.get("op_id", "")

                allocation.save(update_fields=[
                    "forms_allocated",
                    "vra",
                    "device",
                    "note",
                    "client_op_id",
                    "updated_at",
                ])

                created = False

            else:
                allocation = FormAAllocation.objects.create(
                    receipt=receipt,
                    kiems_kit=kit,
                    vra=vra,
                    device=device,
                    forms_allocated=forms,
                    note=note,
                    client_op_id=payload.get("op_id", ""),
                )

                created = True

        return _ok({
            "allocation_id": str(allocation.id),
            "created": created,
            "kit_name": kit.kit_name,
            "forms_allocated": allocation.forms_allocated,
            "unallocated": receipt.unallocated,
        })

    status, body, _ = with_idempotency(
        request,
        "form_a.allocate",
        do
    )

    return JsonResponse(body, status=status)


@csrf_exempt
@require_POST
def form_a_deallocate(request):
    """POST /api/form-a/deallocate/

    Supports:
      { fingerprint, allocation_id }
      { fingerprint, receipt_id, kit_id }
      { fingerprint, receipt_id, kit_id, allocation_amount }

    If allocation_amount is omitted, the entire allocation is removed.
    If allocation_amount is supplied, only that quantity is removed.
    """
    payload = _json_body(request)
    fp = _fingerprint(request, payload)

    def do():
        allocation_id = payload.get("allocation_id")
        receipt_id = payload.get("receipt_id")
        kit_id = payload.get("kit_id")
        amount = payload.get("allocation_amount")

        # Find allocation using either identification method
        if allocation_id:
            allocation = (
                FormAAllocation.objects
                .select_related("receipt", "kiems_kit")
                .filter(pk=allocation_id)
                .first()
            )

        elif receipt_id and kit_id:
            allocation = (
                FormAAllocation.objects
                .select_related("receipt", "kiems_kit")
                .filter(
                    receipt_id=receipt_id,
                    kiems_kit_id=kit_id,
                )
                .first()
            )

        else:
            return _err(
                "Either allocation_id, or receipt_id + kit_id, is required."
            )

        if not allocation:
            return _err("Allocation not found.", 404)

        # Verify VRA belongs to the allocation's ward
        vra = _resolve_vra(fp)

        if not vra or allocation.receipt.ward_id != vra.ward_id:
            return _err("Not your ward.", 403)

        # Verify device authorization
        device = _resolve_device(fp)

        if not device or not device.is_burned:
            return _err("Device not authorized.", 403)

        kit_name = allocation.kiems_kit.kit_name

        # No amount supplied: preserve full-deallocation behaviour
        if amount is None:
            with transaction.atomic():
                allocation = (
                    FormAAllocation.objects
                    .select_for_update()
                    .filter(pk=allocation.pk)
                    .first()
                )

                if not allocation:
                    return _err("Allocation not found.", 404)

                allocation.delete()

            return _ok({
                "deleted": True,
                "kit_name": kit_name,
            })

        # Validate partial deallocation amount
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            return _err("allocation_amount must be an integer.")

        if amount < 1:
            return _err("allocation_amount must be at least 1.")

        # ACK booklets must always move in multiples of 50
        if (
                allocation.receipt.form_type == FormAReceipt.ACK
                and amount % 50 != 0
        ):
            return _err(
                "Acknowledgment deallocations must be multiples of 50.",
                409,
            )

        with transaction.atomic():

            allocation = (
                FormAAllocation.objects
                .select_for_update()
                .filter(pk=allocation.pk)
                .first()
            )

            if not allocation:
                return _err("Allocation not found.", 404)

            current_amount = allocation.forms_allocated

            if amount > current_amount:
                return _err(
                    f"Cannot move {amount}; only "
                    f"{current_amount} allocated.",
                    409,
                )

            remaining = current_amount - amount

            if remaining == 0:
                allocation.delete()
            else:
                allocation.forms_allocated = remaining

                allocation.save(update_fields=[
                    "forms_allocated",
                    "updated_at",
                ])

        return _ok({
            "deleted": remaining == 0,
            "kit_name": kit_name,
            "moved": amount,
            "remaining_allocated": remaining,
        })

    status, body, _ = with_idempotency(
        request,
        "form_a.deallocate",
        do
    )

    return JsonResponse(body, status=status)


@require_GET
def form_a_receipt_allocations(request, receipt_id):
    """GET /api/form-a/receipts/<receipt_id>/allocations/?fingerprint="""
    fp = request.GET.get("fingerprint")
    if not fp:
        return JsonResponse({"ok": False, "error": "fingerprint required"}, status=400)

    vra = _resolve_vra(fp)
    if not vra:
        return JsonResponse({"ok": False, "error": "VRA not resolved."}, status=404)

    receipt = FormAReceipt.objects.filter(
        pk=receipt_id, is_archived=False,
    ).select_related("ward", "phase").first()
    if not receipt:
        return JsonResponse({"ok": False, "error": "Receipt not found."}, status=404)
    if receipt.ward_id != vra.ward_id:
        return JsonResponse({"ok": False, "error": "Not your ward."}, status=403)

    allocations = receipt.allocations.select_related("kiems_kit", "vra").order_by("-created_at")

    return JsonResponse({
        "ok": True,
        "receipt_id": str(receipt.id),
        "form_type": receipt.form_type,
        "form_count": receipt.form_count,
        "spoiled": receipt.spoiled_count,
        "allocated": receipt.allocated_count,
        "unallocated": receipt.unallocated,
        "allocations": [{
            "id": str(a.id),
            "kit_id": a.kiems_kit_id,
            "kit_name": a.kiems_kit.kit_name,
            "serial_no": a.kiems_kit.serial_no,
            "forms_allocated": a.forms_allocated,
            "note": a.note,
            "created_at": a.created_at.isoformat(),
        } for a in allocations],
    })


logger = logging.getLogger(__name__)


@require_http_methods(["GET", "POST"])
def cron_crawl_iebc_news(request):
    expected = os.environ.get("CRON_SECRET", "").strip()
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()

    if not expected or not hmac.compare_digest(token, expected):
        return JsonResponse({"ok": False, "error": "unauthorized"}, status=401)

    out, err = io.StringIO(), io.StringIO()
    try:
        call_command("crawl_iebc_news", max=5, stdout=out, stderr=err)
    except CommandError as e:
        logger.exception("crawl_iebc_news failed")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)
    except Exception as e:
        logger.exception("crawl_iebc_news crashed")
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

    return JsonResponse({"ok": True, "output": out.getvalue()[-500:], "errors": err.getvalue()[-500:]})
