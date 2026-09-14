import csv
import hmac
import io
import json
import logging
import os
import time
from datetime import datetime, timedelta

import openpyxl
import requests
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout, login as auth_login
from django.db import IntegrityError
from django.db.models import Q, Sum, Count
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

from home.models import (
    VRA, Clerk, Device, DeviceBurnLog, AuditLog, WhatsAppSetting, Phase,
    Ward, KIEMSKit, DailyKIEMSEntry, Constituency, WhatsAppGroup,
    CronHeartbeat, DailyReportState,
)
from home.services.daily_report import (
    reap_stuck_sending_states,
    send_ready_reports,
    reevaluate_constituency_report,
    run_daily_report_tick,
)
from .decorators import ict_required
from .forms import (
    WardForm, VRAForm, ClerkForm, KIEMSKitForm,
    DailyEntryOfficeForm, WhatsAppSettingForm, ICTOfficerLoginForm, DailyEntryCreateForm,
)

logger = logging.getLogger(__name__)


# ---------- Helpers ----------

def _log(request, action, model_name, instance, description=""):
    AuditLog.objects.create(
        actor=request.user,
        constituency=request.constituency,
        action=action,
        model_name=model_name,
        object_id=str(getattr(instance, "pk", "")),
        object_repr=str(instance)[:255],
        description=description,
        ip_address=request.META.get("REMOTE_ADDR"),
    )


def _coerce_date(value):
    """Accept a date object, a 'YYYY-MM-DD' string, or None -> today."""
    if value is None:
        return timezone.localdate()
    if hasattr(value, "year") and hasattr(value, "month"):
        return value
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return timezone.localdate()


def _bot_url():
    return getattr(settings, "WHATSAPP_BOT_URL", "http://localhost:3000")


_BOT_HEALTH_CACHE = {"ts": 0.0, "ok": False}
_BOT_HEALTH_TTL_SECONDS = 5


def _check_bot_health():
    """Ping the WhatsApp bot, cached for 5s to survive rapid page refreshes."""
    now = time.time()
    if now - _BOT_HEALTH_CACHE["ts"] < _BOT_HEALTH_TTL_SECONDS:
        return _BOT_HEALTH_CACHE["ok"]

    ok = False
    try:
        r = requests.get(f"{_bot_url()}/status", timeout=3)
        if r.status_code == 200:
            ok = bool(r.json().get("isReady", False))
    except Exception:
        ok = False

    _BOT_HEALTH_CACHE["ts"] = now
    _BOT_HEALTH_CACHE["ok"] = ok
    return ok


# ---------- Dashboard ----------

@ict_required
def dashboard(request):
    c = request.constituency
    today = timezone.now().date()

    ward_qs = Ward.objects.filter(constituency=c)
    vra_qs = VRA.objects.filter(ward__constituency=c)
    clerk_qs = Clerk.objects.filter(ward__constituency=c)
    kit_qs = KIEMSKit.objects.filter(ward__constituency=c)
    device_qs = Device.objects.filter(
        Q(vra__ward__constituency=c) | Q(clerk__ward__constituency=c)
    ).distinct()
    entry_qs = DailyKIEMSEntry.objects.filter(ward__constituency=c)

    todays = entry_qs.filter(entry_date=today)
    totals = entry_qs.aggregate(
        male=Sum("registered_male"),
        female=Sum("registered_female"),
        total=Sum("total_registered"),
    )

    by_ward = (
        entry_qs.values("ward__name")
        .annotate(total=Sum("total_registered"))
        .order_by("-total")[:10]
    )

    context = {
        "constituency": c,
        "ward_count": ward_qs.count(),
        "vra_count": vra_qs.filter(active=True).count(),
        "clerk_count": clerk_qs.filter(active=True).count(),
        "kit_count": kit_qs.count(),
        "device_count": device_qs.count(),
        "burned_device_count": device_qs.filter(is_burned=True).count(),
        "today_entry_count": todays.count(),
        "totals": totals,
        "by_ward": list(by_ward),
        "recent_audit": AuditLog.objects.filter(constituency=c)[:8],
        "recent_entries": todays.select_related("ward", "vra", "kiems_kit")[:10],
        "unread_notification_count": 0,
    }
    return render(request, "ict/dashboard.html", context)


# ---------- Wards ----------

@ict_required
def ward_list(request):
    c = request.constituency
    wards = (
        Ward.objects.filter(constituency=c)
        .annotate(
            vra_count=Count("vras", distinct=True),
            clerk_count=Count("clerks", distinct=True),
            kit_count=Count("kits", distinct=True),
        )
        .order_by("name")
    )
    return render(request, "ict/wards/list.html", {"wards": wards, "constituency": c})


@ict_required
def ward_create(request):
    c = request.constituency
    if request.method == "POST":
        form = WardForm(request.POST)
        if form.is_valid():
            ward = form.save(commit=False)
            ward.constituency = c
            ward.save()
            _log(request, "CREATE", "Ward", ward, f"Created ward {ward.name}")
            messages.success(request, f"Ward '{ward.name}' created.")
            return redirect("ict:ward_list")
    else:
        form = WardForm()
    return render(request, "ict/wards/form.html", {"form": form, "constituency": c, "mode": "create"})


@ict_required
def ward_edit(request, pk):
    c = request.constituency
    ward = get_object_or_404(Ward, pk=pk, constituency=c)
    if request.method == "POST":
        form = WardForm(request.POST, instance=ward)
        if form.is_valid():
            ward = form.save()
            _log(request, "UPDATE", "Ward", ward, f"Updated ward {ward.name}")
            messages.success(request, "Ward updated.")
            return redirect("ict:ward_list")
    else:
        form = WardForm(instance=ward)
    return render(request, "ict/wards/form.html", {"form": form, "constituency": c, "mode": "edit", "ward": ward})


@ict_required
def ward_detail(request, pk):
    c = request.constituency
    ward = get_object_or_404(Ward, pk=pk, constituency=c)
    vras = ward.vras.all()
    clerks = ward.clerks.all()
    kits = ward.kits.all()
    entries = ward.daily_entries.select_related("vra", "kiems_kit")[:20]
    return render(request, "ict/wards/detail.html", {
        "ward": ward, "vras": vras, "clerks": clerks,
        "kits": kits, "entries": entries, "constituency": c,
    })


# ---------- Staff (VRA + Clerk) ----------

@ict_required
def staff_list(request):
    c = request.constituency
    vras = VRA.objects.filter(ward__constituency=c).select_related("ward")
    clerks = Clerk.objects.filter(ward__constituency=c).select_related("ward")
    return render(request, "ict/staff/list.html", {
        "vras": vras, "clerks": clerks, "constituency": c,
    })


@ict_required
def vra_create(request):
    c = request.constituency
    if request.method == "POST":
        form = VRAForm(request.POST, constituency=c)
        if form.is_valid():
            vra = form.save()
            _log(request, "CREATE", "VRA", vra, f"Created VRA {vra.name}")
            messages.success(request, f"VRA '{vra.name}' created.")
            return redirect("ict:staff_list")
    else:
        form = VRAForm(constituency=c)
    return render(request, "ict/staff/vra_form.html", {"form": form, "constituency": c, "mode": "create"})


@ict_required
def vra_edit(request, pk):
    c = request.constituency
    vra = get_object_or_404(VRA, pk=pk, ward__constituency=c)
    if request.method == "POST":
        form = VRAForm(request.POST, instance=vra, constituency=c)
        if form.is_valid():
            vra = form.save()
            _log(request, "UPDATE", "VRA", vra, f"Updated VRA {vra.name}")
            messages.success(request, "VRA updated.")
            return redirect("ict:staff_list")
    else:
        form = VRAForm(instance=vra, constituency=c)
    return render(request, "ict/staff/vra_form.html", {"form": form, "constituency": c, "mode": "edit", "vra": vra})


@ict_required
def vra_toggle(request, pk):
    c = request.constituency
    vra = get_object_or_404(VRA, pk=pk, ward__constituency=c)
    if request.method == "POST":
        vra.active = not vra.active
        vra.save(update_fields=["active"])
        _log(request, "UPDATE", "VRA", vra,
             f"{'Activated' if vra.active else 'Deactivated'} VRA {vra.name}")
        messages.success(request, f"VRA {'activated' if vra.active else 'deactivated'}.")
    return redirect("ict:staff_list")


@ict_required
def clerk_create(request):
    c = request.constituency
    if request.method == "POST":
        form = ClerkForm(request.POST, constituency=c)
        if form.is_valid():
            clerk = form.save()
            _log(request, "CREATE", "Clerk", clerk, f"Created Clerk {clerk.name}")
            messages.success(request, f"Clerk '{clerk.name}' created.")
            return redirect("ict:staff_list")
    else:
        form = ClerkForm(constituency=c)
    return render(request, "ict/staff/clerk_form.html", {"form": form, "constituency": c, "mode": "create"})


@ict_required
def clerk_edit(request, pk):
    c = request.constituency
    clerk = get_object_or_404(Clerk, pk=pk, ward__constituency=c)
    if request.method == "POST":
        form = ClerkForm(request.POST, instance=clerk, constituency=c)
        if form.is_valid():
            clerk = form.save()
            _log(request, "UPDATE", "Clerk", clerk, f"Updated Clerk {clerk.name}")
            messages.success(request, "Clerk updated.")
            return redirect("ict:staff_list")
    else:
        form = ClerkForm(instance=clerk, constituency=c)
    return render(request, "ict/staff/clerk_form.html",
                  {"form": form, "constituency": c, "mode": "edit", "clerk": clerk})


@ict_required
def clerk_toggle(request, pk):
    c = request.constituency
    clerk = get_object_or_404(Clerk, pk=pk, ward__constituency=c)
    if request.method == "POST":
        clerk.active = not clerk.active
        clerk.save(update_fields=["active"])
        _log(request, "UPDATE", "Clerk", clerk,
             f"{'Activated' if clerk.active else 'Deactivated'} Clerk {clerk.name}")
        messages.success(request, f"Clerk {'activated' if clerk.active else 'deactivated'}.")
    return redirect("ict:staff_list")


# ---------- KIEMS Kits ----------

@ict_required
def kit_list(request):
    c = request.constituency
    kits = (
        KIEMSKit.objects.filter(ward__constituency=c)
        .select_related("ward")
        .prefetch_related("assigned_clerks")
    )
    return render(request, "ict/kits/list.html", {"kits": kits, "constituency": c})


@ict_required
def kit_create(request):
    c = request.constituency
    if request.method == "POST":
        form = KIEMSKitForm(request.POST, constituency=c)
        if form.is_valid():
            kit = form.save()
            _log(request, "CREATE", "KIEMSKit", kit, f"Created kit {kit.serial_no}")
            messages.success(request, "Kit created.")
            return redirect("ict:kit_list")
    else:
        form = KIEMSKitForm(constituency=c)
    return render(request, "ict/kits/form.html", {"form": form, "constituency": c, "mode": "create"})


@ict_required
def kit_edit(request, pk):
    c = request.constituency
    kit = get_object_or_404(KIEMSKit, pk=pk, ward__constituency=c)
    if request.method == "POST":
        form = KIEMSKitForm(request.POST, instance=kit, constituency=c)
        if form.is_valid():
            kit = form.save()
            _log(request, "UPDATE", "KIEMSKit", kit, f"Updated kit {kit.serial_no}")
            messages.success(request, "Kit updated.")
            return redirect("ict:kit_list")
    else:
        form = KIEMSKitForm(instance=kit, constituency=c)
    return render(request, "ict/kits/form.html", {"form": form, "constituency": c, "mode": "edit", "kit": kit})


@ict_required
def kit_detail(request, pk):
    c = request.constituency
    kit = get_object_or_404(KIEMSKit, pk=pk, ward__constituency=c)
    entries = kit.daily_entries.select_related("vra", "clerk")[:30]
    return render(request, "ict/kits/detail.html", {
        "kit": kit, "entries": entries, "constituency": c,
    })


# ---------- Devices ----------

@ict_required
def device_list(request):
    c = request.constituency
    qs = Device.objects.filter(
        Q(vra__ward__constituency=c) | Q(clerk__ward__constituency=c)
    ).distinct().select_related("vra", "clerk")

    status = request.GET.get("status")
    if status == "authorized":
        qs = qs.filter(is_burned=True)
    elif status == "unauthorized":
        qs = qs.filter(is_burned=False)

    return render(request, "ict/devices/list.html", {
        "devices": qs,
        "constituency": c,
        "status": status,
        "authorized_count": Device.objects.filter(
            Q(vra__ward__constituency=c) | Q(clerk__ward__constituency=c),
            is_burned=True,
        ).distinct().count(),
        "unauthorized_count": Device.objects.filter(
            Q(vra__ward__constituency=c) | Q(clerk__ward__constituency=c),
            is_burned=False,
        ).distinct().count(),
    })


@ict_required
def device_detail(request, pk):
    c = request.constituency
    device = get_object_or_404(
        Device.objects.filter(
            Q(vra__ward__constituency=c) | Q(clerk__ward__constituency=c)
        ).distinct(),
        pk=pk,
    )
    logs = device.burn_logs.select_related("performed_by")[:30]
    return render(request, "ict/devices/detail.html", {
        "device": device, "logs": logs, "constituency": c,
    })


@ict_required
def device_authorize(request, pk):
    """Authorize a device: sets is_burned=True."""
    c = request.constituency
    device = get_object_or_404(
        Device.objects.filter(
            Q(vra__ward__constituency=c) | Q(clerk__ward__constituency=c)
        ).distinct(),
        pk=pk,
    )
    if request.method == "POST":
        if device.is_burned:
            messages.info(request, "Device is already authorized.")
        else:
            device.is_burned = True
            device.burn_date = timezone.now()
            device.burn_notes = request.POST.get("notes", "")
            device.save(update_fields=["is_burned", "burn_date", "burn_notes"])
            DeviceBurnLog.objects.create(
                device=device,
                action="AUTHORIZE",
                performed_by=request.user,
                notes=device.burn_notes,
            )
            _log(request, "BURN", "Device", device,
                 f"Authorized device {device.fingerprint[:12]}")
            messages.success(request, "Device authorized successfully.")
    return redirect("ict:device_detail", pk=device.pk)


@ict_required
def device_revoke(request, pk):
    """Revoke a device's authorization: sets is_burned=False."""
    c = request.constituency
    device = get_object_or_404(
        Device.objects.filter(
            Q(vra__ward__constituency=c) | Q(clerk__ward__constituency=c)
        ).distinct(),
        pk=pk,
    )
    if request.method == "POST":
        if not device.is_burned:
            messages.info(request, "Device is not currently authorized.")
        else:
            device.is_burned = False
            device.burn_date = None
            device.burn_notes = ""
            device.save(update_fields=["is_burned", "burn_date", "burn_notes"])
            DeviceBurnLog.objects.create(
                device=device,
                action="REVOKE",
                performed_by=request.user,
                notes=request.POST.get("notes", ""),
            )
            _log(request, "UNBURN", "Device", device,
                 f"Revoked authorization for device {device.fingerprint[:12]}")
            messages.success(request, "Device authorization revoked.")
    return redirect("ict:device_detail", pk=device.pk)


@ict_required
def device_delete(request, pk):
    """Permanently delete a device and its burn logs."""
    c = request.constituency
    device = get_object_or_404(
        Device.objects.filter(
            Q(vra__ward__constituency=c) | Q(clerk__ward__constituency=c)
        ).distinct(),
        pk=pk,
    )

    if request.method == "POST":
        fingerprint = device.fingerprint
        device_id = device.pk

        # Break links so VRA/Clerk don't hold a stale token
        if device.vra and device.vra.device_token == fingerprint:
            device.vra.device_token = None
            device.vra.save(update_fields=["device_token"])
        if device.clerk and device.clerk.device_token == fingerprint:
            device.clerk.device_token = None
            device.clerk.save(update_fields=["device_token"])

        DeviceBurnLog.objects.filter(device=device).delete()
        device.delete()

        AuditLog.objects.create(
            actor=request.user,
            constituency=c,
            action="DELETE",
            model_name="Device",
            object_id=str(device_id),
            object_repr=fingerprint[:30],
            description=f"Deleted device {fingerprint[:12]}",
            ip_address=request.META.get("REMOTE_ADDR"),
        )

        messages.success(request, f"Device {fingerprint[:12]} deleted.")
        return redirect("ict:device_list")

    return redirect("ict:device_detail", pk=device.pk)


# ---------- Daily Entries ----------

@ict_required
def entry_list(request):
    """
    Daily entries for the ICT officer's constituency.

    This view is REGISTRATION-only. Venue pre-maps are excluded because
    they carry no numbers and belong to the planning workflow, not the
    daily reporting workflow.
    """
    c = request.constituency

    qs = (
        DailyKIEMSEntry.objects
        .filter(ward__constituency=c, entry_type="REGISTRATION")
        .select_related("ward", "vra", "kiems_kit", "phase")
    )

    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    ward_id = request.GET.get("ward")
    kit_id = request.GET.get("kit")
    vra_id = request.GET.get("vra")

    if date_from:
        qs = qs.filter(entry_date__gte=date_from)
    if date_to:
        qs = qs.filter(entry_date__lte=date_to)
    if ward_id:
        qs = qs.filter(ward_id=ward_id)
    if kit_id:
        qs = qs.filter(kiems_kit_id=kit_id)
    if vra_id:
        qs = qs.filter(vra_id=vra_id)

    totals = qs.aggregate(
        male=Sum("registered_male"),
        female=Sum("registered_female"),
        total=Sum("total_registered"),
        transferred=Sum("total_transferred"),
        updated=Sum("total_updated"),
    )

    today = timezone.localdate()
    today_entries = DailyKIEMSEntry.objects.filter(
        ward__constituency=c,
        entry_date=today,
        entry_type="REGISTRATION",
    )
    today_stats = {
        "total_entries": today_entries.count(),
        "unique_kits": today_entries.values("kiems_kit").distinct().count(),
        "total_registered": today_entries.aggregate(Sum("total_registered"))["total_registered__sum"] or 0,
        "registered_male": today_entries.aggregate(Sum("registered_male"))["registered_male__sum"] or 0,
        "registered_female": today_entries.aggregate(Sum("registered_female"))["registered_female__sum"] or 0,
        "total_transferred": today_entries.aggregate(Sum("total_transferred"))["total_transferred__sum"] or 0,
        "total_updated": today_entries.aggregate(Sum("total_updated"))["total_updated__sum"] or 0,
    }

    wards = Ward.objects.filter(constituency=c).order_by("name")
    kits = KIEMSKit.objects.filter(ward__constituency=c, status=True).order_by("kit_name")
    vras = VRA.objects.filter(ward__constituency=c, active=True).order_by("name")

    return render(request, "ict/entries/list.html", {
        "entries": qs.order_by("-entry_date", "ward__name")[:500],
        "totals": totals,
        "today_stats": today_stats,
        "constituency": c,
        "wards": wards,
        "kits": kits,
        "vras": vras,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "ward_id": ward_id or "",
        "kit_id": kit_id or "",
        "vra_id": vra_id or "",
        "is_filtered": any([date_from, date_to, ward_id, kit_id, vra_id]),
    })


@ict_required
def entry_create(request):
    """Office: create a manual REGISTRATION entry from the daily-entries page."""
    c = request.constituency

    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        messages.error(
            request,
            "No active phase. Ask the SuperAdmin to activate one before creating entries."
        )
        return redirect("ict:entry_list")

    if request.method == "POST":
        form = DailyEntryCreateForm(request.POST, constituency=c)
        if form.is_valid():
            entry = form.save(commit=False)

            entry.phase = active_phase
            entry.entry_type = "REGISTRATION"

            if entry.kiems_kit and entry.kiems_kit.ward_id:
                entry.ward = entry.kiems_kit.ward

            entry.office_updated_by = request.user.username
            entry.office_updated_at = timezone.now()

            try:
                entry.save()
            except IntegrityError:
                messages.error(
                    request,
                    "An entry for this kit, VRA, and date already exists. "
                    "Edit the existing entry instead."
                )
                return render(request, "ict/entries/form_new.html", {
                    "form": form,
                    "constituency": c,
                    "wards": Ward.objects.filter(constituency=c).order_by("name"),
                    "kits": KIEMSKit.objects.filter(ward__constituency=c, status=True).order_by("kit_name"),
                    "vras": VRA.objects.filter(ward__constituency=c, active=True).order_by("name"),
                    "today": timezone.localdate(),
                    "active_phase": active_phase,
                })

            _log(request, "CREATE", "DailyKIEMSEntry", entry,
                 f"Manual entry {entry.entry_date} - {entry.ward.name} - {entry.kiems_kit.kit_name}")

            try:
                reevaluate_constituency_report(entry.ward.constituency, entry.entry_date)
            except Exception as e:
                logger.warning("State reevaluation failed after manual entry: %s", e)

            messages.success(request, "Entry created.")
            return redirect("ict:entry_list")
    else:
        form = DailyEntryCreateForm(constituency=c)

    return render(request, "ict/entries/form_new.html", {
        "form": form,
        "constituency": c,
        "wards": Ward.objects.filter(constituency=c).order_by("name"),
        "kits": KIEMSKit.objects.filter(ward__constituency=c, status=True).order_by("kit_name"),
        "vras": VRA.objects.filter(ward__constituency=c, active=True).order_by("name"),
        "today": timezone.localdate(),
        "active_phase": active_phase,
    })


@ict_required
def entry_edit(request, pk):
    """Office edit of an existing REGISTRATION entry."""
    c = request.constituency
    entry = get_object_or_404(
        DailyKIEMSEntry,
        pk=pk,
        ward__constituency=c,
        entry_type="REGISTRATION",
    )
    if request.method == "POST":
        form = DailyEntryOfficeForm(request.POST, instance=entry)
        if form.is_valid():
            entry = form.save(commit=False)
            entry.office_updated_by = request.user.username
            entry.office_updated_at = timezone.now()
            entry.edit_count += 1
            entry.save()
            _log(request, "EDIT", "DailyKIEMSEntry", entry,
                 f"Office update on {entry.entry_date}")
            messages.success(request, "Entry updated.")
            return redirect("ict:entry_list")
    else:
        form = DailyEntryOfficeForm(instance=entry)
    return render(request, "ict/entries/form.html", {
        "form": form, "entry": entry, "constituency": c,
    })

@ict_required
@require_POST
def entry_delete(request, pk):
    """Permanently delete a single REGISTRATION entry."""
    c = request.constituency
    entry = get_object_or_404(
        DailyKIEMSEntry, pk=pk, ward__constituency=c, entry_type="REGISTRATION"
    )

    description = f"Deleted entry {entry.entry_date} - {entry.ward.name} - {entry.kiems_kit.kit_name}"
    entry_date, ward = entry.entry_date, entry.ward
    entry.delete()

    _log(request, "DELETE", "DailyKIEMSEntry", f"deleted-{pk}", description)

    try:
        reevaluate_constituency_report(ward.constituency, entry_date)
    except Exception as e:
        logger.warning("State reevaluation failed after entry delete: %s", e)

    messages.success(request, "Entry deleted.")
    return redirect("ict:entry_list")




# ==================== EXPORTS ====================

def _export_filtered_entries(request, constituency):
    """Shared filter logic for both export formats."""
    qs = (
        DailyKIEMSEntry.objects
        .filter(ward__constituency=constituency, entry_type="REGISTRATION")
        .select_related("ward", "vra", "kiems_kit")
    )
    for param, field in [
        ("date_from", "entry_date__gte"),
        ("date_to", "entry_date__lte"),
        ("ward", "ward_id"),
        ("kit", "kiems_kit_id"),
        ("vra", "vra_id"),
    ]:
        val = request.GET.get(param)
        if val:
            qs = qs.filter(**{field: val})
    return qs


@ict_required
@require_GET
def entry_export_excel(request):
    """
    Export filtered REGISTRATION entries to Excel in the same
    ward -> kit -> M/F -> date-columns layout as the manual tracking sheet.
    """
    c = request.constituency
    entries = list(_export_filtered_entries(request, c))
    if not entries:
        messages.warning(request, "No entries match this filter.")
        return redirect("ict:entry_list")

    all_dates = sorted({e.entry_date for e in entries})
    by_cell = {(e.ward_id, e.kiems_kit_id, e.entry_date): e for e in entries}

    ward_ids = {e.ward_id for e in entries}
    wards = Ward.objects.filter(constituency=c, id__in=ward_ids).order_by("name")

    kits_by_ward = {}
    for w in wards:
        kit_ids = {e.kiems_kit_id for e in entries if e.ward_id == w.id}
        kits_by_ward[w.id] = list(
            KIEMSKit.objects.filter(ward=w, id__in=kit_ids).order_by("kit_name")
        )

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Daily Entries"

    header_fill = PatternFill("solid", fgColor="16A34A")
    header_font = Font(bold=True, color="FFFFFF")
    total_fill = PatternFill("solid", fgColor="E5E7EB")
    total_font = Font(bold=True)
    center = Alignment(horizontal="center", vertical="center")
    thin = Side(style="thin", color="DDDDDD")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    FIXED_COLS = 3  # Ward | Kit | M/F
    DATE_START_COL = FIXED_COLS + 1
    last_col = DATE_START_COL + len(all_dates) - 1

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    title_cell = ws.cell(row=1, column=1, value=f"{c.name} Constituency \u2014 Daily Registration Report")
    title_cell.font = Font(bold=True, size=13, color="16A34A")
    title_cell.alignment = center

    ws.cell(row=2, column=1, value="WARD")
    ws.cell(row=2, column=2, value="KIT")
    ws.cell(row=2, column=3, value="M/F")
    for i, d in enumerate(all_dates):
        ws.cell(row=2, column=DATE_START_COL + i, value=d.strftime("%d-%b")).alignment = center
    for col_idx in range(1, last_col + 1):
        cell = ws.cell(row=2, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center
        cell.border = border

    row = 3
    for w in wards:
        ward_start_row = row
        for kit in kits_by_ward.get(w.id, []):
            for label, attr in (("M", "registered_male"), ("F", "registered_female")):
                ws.cell(row=row, column=2, value=f"{kit.kit_name} ({kit.serial_no})")
                ws.cell(row=row, column=3, value=label).alignment = center
                for i, d in enumerate(all_dates):
                    entry = by_cell.get((w.id, kit.id, d))
                    val = getattr(entry, attr) if entry else None
                    cell = ws.cell(row=row, column=DATE_START_COL + i, value=val)
                    cell.alignment = center
                    cell.border = border
                row += 1

        if row - 1 >= ward_start_row:
            ws.merge_cells(start_row=ward_start_row, start_column=1, end_row=row - 1, end_column=1)
            ward_cell = ws.cell(row=ward_start_row, column=1, value=w.name)
            ward_cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            ward_cell.font = Font(bold=True)

        ws.cell(row=row, column=2, value="TOTAL/DAY")
        for i, d in enumerate(all_dates):
            total = sum(
                (getattr(by_cell.get((w.id, kit.id, d)), "total_registered", 0) or 0)
                for kit in kits_by_ward.get(w.id, [])
            )
            cell = ws.cell(row=row, column=DATE_START_COL + i, value=total or None)
            cell.font = total_font
            cell.fill = total_fill
            cell.alignment = center
        row += 1

    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 6
    for i in range(len(all_dates)):
        ws.column_dimensions[openpyxl.utils.get_column_letter(DATE_START_COL + i)].width = 10

    # Freeze the identifying columns (Ward/Kit/M-F) + the two header rows
    ws.freeze_panes = ws.cell(row=3, column=DATE_START_COL).coordinate

    timestamp = timezone.localtime().strftime("%Y%m%d_%H%M%S")
    filename = f"{c.name.replace(' ', '_')}_Entries_{timestamp}.xlsx"

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


@ict_required
@require_GET
def entry_export_csv(request):
    """
    Export filtered REGISTRATION entries to CSV using the exact same
    column layout entry_import_csv expects, so a file exported here can
    be re-imported without any manual editing.
    """
    c = request.constituency
    qs = _export_filtered_entries(request, c).order_by(
        "entry_date", "ward__name", "kiems_kit__kit_name"
    )

    timestamp = timezone.localtime().strftime("%Y%m%d_%H%M%S")
    filename = f"{c.name.replace(' ', '_')}_Entries_{timestamp}.csv"

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        "date", "ward_id", "kit_id", "vra_id",
        "male", "female", "transferred", "updated", "venue",
    ])
    for e in qs:
        writer.writerow([
            e.entry_date.strftime("%Y-%m-%d"),
            e.ward_id,
            e.kiems_kit_id,
            e.vra_id,
            e.registered_male,
            e.registered_female,
            e.total_transferred,
            e.total_updated,
            e.venue or "",
        ])
    return response


# ==================== PDF REPORT ====================

def _entry_report_context(request):
    """Context for `ict/entries/report_pdf.html` (and the ReportLab fallback)."""
    c = request.constituency

    qs = (
        DailyKIEMSEntry.objects
        .filter(ward__constituency=c, entry_type="REGISTRATION")
        .select_related("ward", "phase", "kiems_kit")
    )

    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    ward_id = request.GET.get("ward")
    kit_id = request.GET.get("kit")
    vra_id = request.GET.get("vra")

    for param, field, val in [
        ("date_from", "entry_date__gte", date_from),
        ("date_to", "entry_date__lte", date_to),
        ("ward", "ward_id", ward_id),
        ("kit", "kiems_kit_id", kit_id),
        ("vra", "vra_id", vra_id),
    ]:
        if val:
            qs = qs.filter(**{field: val})

    totals = qs.aggregate(
        registered=Sum("total_registered"),
        male=Sum("registered_male"),
        female=Sum("registered_female"),
        transferred=Sum("total_transferred"),
    )

    ward_summary = list(
        qs.values("ward__name").annotate(
            count=Count("id"),
            male=Sum("registered_male"),
            female=Sum("registered_female"),
            registered=Sum("total_registered"),
            transferred=Sum("total_transferred"),
        ).order_by("-registered")
    )

    phase_summary = list(
        qs.values("phase__name").annotate(
            count=Count("id"),
            male=Sum("registered_male"),
            female=Sum("registered_female"),
            registered=Sum("total_registered"),
            transferred=Sum("total_transferred"),
        ).order_by("-registered")
    )

    kit_summary = list(
        qs.values("kiems_kit__kit_name").annotate(
            count=Count("id"),
            male=Sum("registered_male"),
            female=Sum("registered_female"),
            registered=Sum("total_registered"),
        ).order_by("-registered")[:20]
    )

    parts = []
    if date_from and date_to:
        parts.append(f"{date_from} to {date_to}")
    elif date_from:
        parts.append(f"From {date_from}")
    elif date_to:
        parts.append(f"Up to {date_to}")
    else:
        parts.append("All dates")

    if ward_id:
        w = Ward.objects.filter(pk=ward_id, constituency=c).first()
        if w:
            parts.append(f"Ward: {w.name}")
    if kit_id:
        k = KIEMSKit.objects.filter(pk=kit_id, ward__constituency=c).first()
        if k:
            parts.append(f"Kit: {k.kit_name}")
    if vra_id:
        v = VRA.objects.filter(pk=vra_id, ward__constituency=c).first()
        if v:
            parts.append(f"VRA: {v.name}")
    parts.append("Type: Registration")

    return {
        "constituency": c,
        "total_entries": qs.count(),
        "total_registered": totals["registered"] or 0,
        "total_male": totals["male"] or 0,
        "total_female": totals["female"] or 0,
        "total_transferred": totals["transferred"] or 0,
        "ward_summary": ward_summary,
        "phase_summary": phase_summary,
        "kit_summary": kit_summary,
        "scope": "  |  ".join(parts),
        "generated_at": timezone.localtime().strftime("%d %b %Y, %H:%M"),
        "brand_logo_url": "https://verify.iebc.or.ke/images/1.png",
    }


@ict_required
def entry_download_report(request):
    """Generate PDF via PDF.co with ReportLab fallback."""
    ctx = _entry_report_context(request)

    try:
        api_key = getattr(settings, "PDF_CO_API_KEY", None)
        if not api_key:
            raise RuntimeError("PDF_CO_API_KEY not configured")

        html_string = render_to_string("ict/entries/report_pdf.html", ctx)

        api_url = f"{getattr(settings, 'PDF_CO_API_URL', 'https://api.pdf.co/v1')}/pdf/convert/from/html"
        payload = json.dumps({
            "name": f"{ctx['constituency'].name}_Daily_Report.pdf",
            "html": html_string,
            "margin": "0px",
            "paperSize": "Letter",
            "orientation": "Portrait",
            "printBackground": "true",
            "async": False,
        })
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}

        r = requests.post(api_url, headers=headers, data=payload, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"PDF.co returned {r.status_code}")

        result = r.json()
        if result.get("error"):
            raise RuntimeError(result["error"])

        pdf_url = result.get("url")
        if not pdf_url:
            raise RuntimeError("No PDF URL returned")

        pdf = requests.get(pdf_url, timeout=30)
        if pdf.status_code != 200:
            raise RuntimeError("Failed to download PDF")

        response = HttpResponse(pdf.content, content_type="application/pdf")
        filename = (
            f"{ctx['constituency'].name.replace(' ', '_')}"
            f"_Daily_Report_{timezone.localtime().strftime('%Y%m%d')}.pdf"
        )
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    except Exception as e:
        messages.warning(request, f"Falling back to local PDF: {e}")
        return _entry_pdf_reportlab(request, ctx)


def _entry_pdf_reportlab(request, ctx):
    """Local PDF fallback using ReportLab. Reads from the same ctx as `_entry_report_context`."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.enums import TA_CENTER
    except ImportError:
        messages.error(request, "ReportLab not installed and PDF.co unavailable.")
        return redirect("ict:entry_list")

    response = HttpResponse(content_type="application/pdf")
    filename = f"{ctx['constituency'].name.replace(' ', '_')}_Daily_Report.pdf"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    doc = SimpleDocTemplate(
        response, pagesize=letter,
        rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36,
    )
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(
        f"{ctx['constituency'].name} Constituency - Daily Report",
        ParagraphStyle("T", parent=styles["Title"], fontSize=16,
                       textColor=colors.HexColor("#16a34a"),
                       alignment=TA_CENTER, spaceAfter=4)
    ))
    story.append(Paragraph(
        f"Generated: {ctx['generated_at']}",
        ParagraphStyle("S", parent=styles["Normal"], fontSize=9,
                       textColor=colors.HexColor("#6b7280"),
                       alignment=TA_CENTER, spaceAfter=14)
    ))

    totals_data = [
        ["Total Entries", "Registered", "Male", "Female", "Transferred"],
        [
            str(ctx["total_entries"]),
            str(ctx["total_registered"]),
            str(ctx["total_male"]),
            str(ctx["total_female"]),
            str(ctx["total_transferred"]),
        ],
    ]
    tt = Table(totals_data, colWidths=[100, 100, 80, 80, 100])
    tt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16a34a")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 10),
        ("FONTSIZE", (0, 1), (-1, -1), 12),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
        ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#f5f5f5")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(tt)
    story.append(Spacer(1, 16))

    if ctx["ward_summary"]:
        story.append(Paragraph("Summary by Ward", styles["Heading4"]))
        ward_data = [["Ward", "Entries", "Male", "Female", "Total", "Transferred"]]
        for w in ctx["ward_summary"]:
            ward_data.append([
                w["ward__name"] or "Unknown",
                str(w["count"]),
                str(w["male"] or 0),
                str(w["female"] or 0),
                str(w["registered"] or 0),
                str(w["transferred"] or 0),
            ])
        wt = Table(ward_data, colWidths=[140, 60, 60, 60, 70, 80])
        wt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16a34a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9f9f9")]),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(wt)
        story.append(Spacer(1, 16))

    if ctx["kit_summary"]:
        story.append(Paragraph("Top Kits by Registration", styles["Heading4"]))
        kit_data = [["Kit", "Entries", "Male", "Female", "Total"]]
        for k in ctx["kit_summary"]:
            kit_data.append([
                k["kiems_kit__kit_name"] or "-",
                str(k["count"]),
                str(k["male"] or 0),
                str(k["female"] or 0),
                str(k["registered"] or 0),
            ])
        kt = Table(kit_data, colWidths=[160, 70, 70, 70, 80])
        kt.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#16a34a")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (0, 1), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9f9f9")]),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(kt)

    doc.build(story)
    return response


@ict_required
def entry_download_report_preview(request):
    """HTML preview of the PDF daily report."""
    ctx = _entry_report_context(request)
    return render(request, "ict/entries/report_pdf.html", ctx)


# ==================== CSV IMPORT ====================

@ict_required
@require_POST
def entry_import_csv(request):
    """
    Import daily entries from CSV.
    Required columns: date, ward_id, kit_id, vra_id, male, female
    Optional: transferred, updated, venue
    """
    c = request.constituency

    csv_file = request.FILES.get("csv_file")
    if not csv_file:
        messages.error(request, "No CSV file uploaded.")
        return redirect("ict:entry_list")

    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        messages.error(request, "No active phase - import aborted.")
        return redirect("ict:entry_list")

    try:
        decoded = csv_file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded))

        success = 0
        errors = []

        for i, row in enumerate(reader, start=2):
            try:
                date_str = (row.get("date") or "").strip()
                ward_id = (row.get("ward_id") or "").strip()
                kit_id = (row.get("kit_id") or "").strip()
                vra_id = (row.get("vra_id") or "").strip()
                male = int(row.get("male") or 0)
                female = int(row.get("female") or 0)
                transferred = int(row.get("transferred") or 0)
                updated = int(row.get("updated") or 0)
                venue = (row.get("venue") or "").strip()

                if not (date_str and ward_id and kit_id and vra_id):
                    errors.append(f"Row {i}: missing required fields")
                    continue

                try:
                    entry_date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
                except ValueError:
                    errors.append(f"Row {i}: invalid date '{date_str}'")
                    continue

                ward = Ward.objects.filter(pk=ward_id, constituency=c).first()
                kit = KIEMSKit.objects.filter(pk=kit_id, ward__constituency=c).first()
                vra = VRA.objects.filter(pk=vra_id, ward__constituency=c).first()

                if not (ward and kit and vra):
                    errors.append(f"Row {i}: ward/kit/vra not in your constituency")
                    continue

                DailyKIEMSEntry.objects.update_or_create(
                    kiems_kit=kit,
                    phase=active_phase,
                    entry_date=entry_date_obj,
                    vra=vra,
                    defaults={
                        "ward": ward,
                        "venue": venue,
                        "registered_male": male,
                        "registered_female": female,
                        "total_registered": male + female,
                        "total_transferred": transferred,
                        "total_updated": updated,
                        "entry_type": "REGISTRATION",
                    },
                )
                success += 1

            except Exception as e:
                errors.append(f"Row {i}: {e}")

        if success:
            _log(request, "CREATE", "DailyKIEMSEntry", f"bulk-{success}",
                 f"Imported {success} entries from CSV")
            messages.success(request, f"Imported {success} entries successfully.")
        if errors:
            preview = "\n".join(errors[:10])
            messages.warning(request, f"{len(errors)} rows failed:\n{preview}")

    except Exception as e:
        messages.error(request, f"Import failed: {e}")

    return redirect("ict:entry_list")


# ---------- Notifications ----------

@ict_required
def notification_list(request):
    c = request.constituency

    constituency_group = (
        WhatsAppGroup.objects
        .filter(constituency=c, is_active=True)
        .order_by("name")
        .first()
    )
    setting = WhatsAppSetting.objects.filter(user=request.user).first()

    return render(request, "ict/notifications/list.html", {
        "constituency": c,
        "constituency_group": constituency_group,
        "selected_group": constituency_group,
        "personal_setting": setting,
        "wards": Ward.objects.filter(constituency=c).order_by("name"),
        "today": timezone.now().date(),
    })


@ict_required
def notification_settings(request):
    c = request.constituency
    setting, _ = WhatsAppSetting.objects.get_or_create(user=request.user)

    constituency_group = (
        WhatsAppGroup.objects
        .filter(constituency=c, is_active=True)
        .order_by("name")
        .first()
    )

    if request.method == "POST":
        form = WhatsAppSettingForm(request.POST, instance=setting, constituency=c)
        if form.is_valid():
            form.save()
            messages.success(request, "Notification settings saved.")
            return redirect("ict:notification_settings")
    else:
        form = WhatsAppSettingForm(instance=setting, constituency=c)

    return render(request, "ict/notifications/settings.html", {
        "form": form,
        "constituency": c,
        "constituency_group": constituency_group,
        "selected_group": constituency_group,
    })


@ict_required
@require_GET
def whatsapp_bot_status(request):
    """Live bot status."""
    try:
        r = requests.get(f"{_bot_url()}/status", timeout=5,
                          headers={"Content-Type": "application/json"})
        if r.status_code == 200:
            data = r.json()
            qr = data.get("qr") if data.get("hasQr") else None
            if qr and not qr.startswith("data:image"):
                qr = None
            return JsonResponse({
                "success": True,
                "isReady": data.get("isReady", False),
                "status": data.get("status", "unknown"),
                "hasQr": data.get("hasQr", False),
                "qr": qr,
                "phoneNumber": data.get("phoneNumber") or data.get("me") or "",
                "uptime": data.get("uptime", 0),
            })
        return JsonResponse({
            "success": False, "isReady": False, "status": "error",
            "error": f"Bot returned {r.status_code}",
        }, status=503)
    except requests.exceptions.ConnectionError:
        return JsonResponse({
            "success": False, "isReady": False, "status": "offline",
            "error": "WhatsApp bot is not running.",
        }, status=503)
    except requests.exceptions.Timeout:
        return JsonResponse({
            "success": False, "isReady": False, "status": "timeout",
            "error": "Connection timeout.",
        }, status=503)
    except Exception as e:
        return JsonResponse({"success": False, "isReady": False,
                              "status": "error", "error": str(e)}, status=500)


@ict_required
@require_GET
def whatsapp_groups_live(request):
    """
    Fetch groups from the bot. Filters to groups the officer can target
    (their own constituency's groups, unclaimed groups, and global groups).
    """
    c = request.constituency

    try:
        s = requests.get(f"{_bot_url()}/status", timeout=3,
                          headers={"Content-Type": "application/json"})
        if s.status_code != 200 or not s.json().get("isReady"):
            return JsonResponse({
                "success": False, "data": [],
                "error": "Bot is not ready. Scan the QR code from the SuperAdmin portal first."
            }, status=503)

        r = requests.get(f"{_bot_url()}/groups", timeout=10,
                          headers={"Content-Type": "application/json"})
        if r.status_code != 200:
            return JsonResponse({
                "success": False, "data": [],
                "error": f"Bot returned {r.status_code}",
            }, status=503)

        raw_groups = r.json().get("data", [])
        counts = {g["id"]: g.get("participants", 0) for g in raw_groups if g.get("id")}

        # Persist any groups the bot knows that we haven't seen yet.
        # Do NOT auto-scope them to a constituency - leave that to the
        # explicit picker, so we don't accidentally claim a shared group.
        for g in raw_groups:
            gid = g.get("id")
            gname = g.get("name", f"Group {gid[:10]}")
            if not gid:
                continue
            try:
                obj, created = WhatsAppGroup.objects.get_or_create(
                    group_id=gid,
                    defaults={"name": gname, "is_active": True, "constituency": None},
                )
                if not created and obj.name != gname:
                    obj.name = gname
                    obj.save(update_fields=["name"])
            except Exception:
                continue

        # Show: this constituency's group, unclaimed groups, global groups.
        # Hide other constituencies' groups (they aren't pickable).
        visible = (
            WhatsAppGroup.objects
            .filter(is_active=True)
            .filter(Q(constituency=c) | Q(constituency__isnull=True))
            .select_related("constituency")
            .order_by("name")
        )

        data = [{
            "id": g.group_id,
            "name": g.name,
            "constituency": g.constituency.name if g.constituency else None,
            "is_mine": bool(g.constituency_id == c.id),
            "is_unclaimed": g.constituency_id is None,
            "participants": counts.get(g.group_id, 0),
        } for g in visible]

        return JsonResponse({"success": True, "data": data})

    except requests.exceptions.ConnectionError:
        return JsonResponse({"success": False, "data": [], "error": "Bot is offline"}, status=503)
    except Exception as e:
        return JsonResponse({"success": False, "data": [], "error": str(e)}, status=500)


@ict_required
@require_POST
def whatsapp_select_group(request):
    """
    Set the WhatsApp group that will receive this constituency's messages.

    - Assigns the chosen WhatsAppGroup to the officer's constituency.
    - Any other group previously scoped to this constituency is un-scoped
      (constituency set to NULL) so a constituency never has more than
      one active target.
    - Also records the choice in the officer's WhatsAppSetting for UI
      display, but routing uses WhatsAppGroup.constituency, not this.

    Accepts JSON {group_id: "..."} or form POST.
    """
    try:
        if request.content_type == "application/json":
            data = json.loads(request.body)
        else:
            data = request.POST.dict()

        group_id = (data.get("group_id") or "").strip()
        c = request.constituency

        setting, _ = WhatsAppSetting.objects.get_or_create(user=request.user)

        if not group_id:
            WhatsAppGroup.objects.filter(constituency=c).update(constituency=None)
            setting.default_group = None
            setting.save(update_fields=["default_group"])
            return JsonResponse({
                "success": True,
                "message": "Constituency group cleared.",
                "group_id": "",
                "group_name": "",
            })

        # Match by group_id regardless of current constituency, because the
        # officer may be re-picking a group currently assigned elsewhere.
        group = WhatsAppGroup.objects.filter(group_id=group_id).first()

        if group is None:
            group = WhatsAppGroup.objects.create(
                group_id=group_id,
                name=data.get("group_name") or f"Group {group_id[:12]}",
                is_active=True,
                constituency=c,
            )
        else:
            if (
                    group.constituency_id
                    and group.constituency_id != c.id
                    and not request.user.is_superuser
            ):
                return JsonResponse({
                    "success": False,
                    "error": (
                        f"Group '{group.name}' is already assigned to "
                        f"{group.constituency.name}. Ask the SuperAdmin to "
                        f"reassign it if this is intentional."
                    ),
                }, status=409)

            WhatsAppGroup.objects.filter(constituency=c).exclude(pk=group.pk).update(constituency=None)

            group.constituency = c
            if data.get("group_name"):
                group.name = data["group_name"]
            group.is_active = True
            group.save(update_fields=["constituency", "name", "is_active"])

        setting.default_group = group
        setting.save(update_fields=["default_group"])

        return JsonResponse({
            "success": True,
            "message": f"'{group.name}' is now the group for {c.name}.",
            "group_id": group.group_id,
            "group_name": group.name,
            "constituency_id": c.id,
            "constituency_name": c.name,
        })

    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


# ============================================================
# MANUAL REPORT SEND
# ============================================================

def _format_ward_report(ward, date):
    """Ward-level daily report message."""
    qs = DailyKIEMSEntry.objects.filter(
        ward=ward, entry_date=date, entry_type="REGISTRATION",
    ).select_related("kiems_kit", "vra")

    if not qs.exists():
        return f"*{ward.name.upper()}* - {date.strftime('%d %b %Y')}\n_No submissions yet._"

    totals = qs.aggregate(
        male=Sum("registered_male"),
        female=Sum("registered_female"),
        total=Sum("total_registered"),
        transferred=Sum("total_transferred"),
    )
    msg = f"*{ward.name.upper()}* - {date.strftime('%d %b %Y')}\n"
    msg += "--------------------------\n"
    for e in qs.order_by("kiems_kit__kit_name"):
        msg += f"{e.kiems_kit.kit_name}: M:{e.registered_male} F:{e.registered_female} = {e.total_registered}\n"
    msg += "--------------------------\n"
    msg += f"*TOTAL:* M:{totals['male'] or 0} F:{totals['female'] or 0} = *{totals['total'] or 0}*"
    if totals["transferred"]:
        msg += f"\nTransferred: {totals['transferred']}"
    return msg


def _format_constituency_report(constituency, date):
    """Constituency-wide daily report message."""
    qs = DailyKIEMSEntry.objects.filter(
        ward__constituency=constituency, entry_date=date, entry_type="REGISTRATION",
    ).select_related("ward")

    if not qs.exists():
        return f"*{constituency.name.upper()}* - {date.strftime('%d %b %Y')}\n_No submissions for this date._"

    totals = qs.aggregate(
        male=Sum("registered_male"),
        female=Sum("registered_female"),
        total=Sum("total_registered"),
        transferred=Sum("total_transferred"),
    )
    wards = qs.values("ward__name").annotate(
        male=Sum("registered_male"),
        female=Sum("registered_female"),
        total=Sum("total_registered"),
    ).order_by("ward__name")

    msg = f"*{constituency.name.upper()} CONSTITUENCY*\n"
    msg += f"_{date.strftime('%d %b %Y')} Daily Report_\n"
    msg += "--------------------------\n"
    for w in wards:
        msg += f"{w['ward__name']}: M:{w['male']} F:{w['female']} = {w['total']}\n"
    msg += "--------------------------\n"
    msg += f"*TOTAL:* M:{totals['male'] or 0} F:{totals['female'] or 0} = *{totals['total'] or 0}*"
    if totals["transferred"]:
        msg += f"\nTransferred: {totals['transferred']}"
    return msg


@ict_required
@require_POST
def whatsapp_send_report(request):
    """
    Manually send a Ward or Constituency report to the officer's target group.
    POST JSON: {report_type: 'ward'|'constituency', ward_id?: int, date?: 'YYYY-MM-DD'}
    """
    c = request.constituency
    try:
        data = json.loads(request.body) if request.content_type == "application/json" else request.POST.dict()
    except json.JSONDecodeError:
        return JsonResponse({"success": False, "error": "Invalid JSON"}, status=400)

    report_type = data.get("report_type", "constituency")
    date_str = data.get("date")
    ward_id = data.get("ward_id")

    if date_str:
        try:
            report_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            report_date = timezone.now().date()
    else:
        report_date = timezone.now().date()

    setting = WhatsAppSetting.objects.filter(user=request.user).first()
    if not setting or not setting.default_group:
        return JsonResponse({
            "success": False,
            "error": "No target group selected. Pick one above first."
        }, status=400)

    if report_type == "ward":
        if not ward_id:
            return JsonResponse({"success": False, "error": "ward_id required"}, status=400)
        ward = Ward.objects.filter(pk=ward_id, constituency=c).first()
        if not ward:
            return JsonResponse({"success": False, "error": "Ward not found in your constituency"}, status=404)
        message = _format_ward_report(ward, report_date)
    else:
        message = _format_constituency_report(c, report_date)

    try:
        r = requests.post(
            f"{_bot_url()}/send",
            json={"groupId": setting.default_group.group_id, "message": message},
            timeout=15,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code == 200:
            return JsonResponse({
                "success": True,
                "message": f"{report_type.title()} report sent to {setting.default_group.name}.",
                "preview": message,
            })
        return JsonResponse({
            "success": False,
            "error": f"Bot returned {r.status_code}: {r.text[:200]}",
        }, status=503)
    except requests.exceptions.ConnectionError:
        return JsonResponse({"success": False, "error": "Bot is offline"}, status=503)
    except requests.exceptions.Timeout:
        return JsonResponse({"success": False, "error": "Bot timed out"}, status=503)
    except Exception as e:
        return JsonResponse({"success": False, "error": str(e)}, status=500)


@ict_required
@require_GET
def whatsapp_report_preview(request):
    """Return the exact message text so the UI can preview before sending."""
    c = request.constituency
    report_type = request.GET.get("report_type", "constituency")
    date_str = request.GET.get("date")
    ward_id = request.GET.get("ward_id")

    try:
        report_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else timezone.now().date()
    except ValueError:
        report_date = timezone.now().date()

    if report_type == "ward" and ward_id:
        ward = Ward.objects.filter(pk=ward_id, constituency=c).first()
        text = _format_ward_report(ward, report_date) if ward else "Ward not found."
    else:
        text = _format_constituency_report(c, report_date)

    return JsonResponse({"success": True, "preview": text})


# ---------- Audit Logs ----------

@ict_required
def audit_log_list(request):
    c = request.constituency
    qs = AuditLog.objects.filter(constituency=c).select_related("actor")

    action = request.GET.get("action")
    model_name = request.GET.get("model")
    if action:
        qs = qs.filter(action=action)
    if model_name:
        qs = qs.filter(model_name=model_name)

    return render(request, "ict/audit/list.html", {
        "logs": qs[:500],
        "actions": AuditLog.ACTIONS,
        "model_names": AuditLog.objects.filter(constituency=c)
        .values_list("model_name", flat=True).distinct(),
        "constituency": c,
        "selected_action": action,
        "selected_model": model_name,
    })


# ---------- Auth ----------

@ict_required
def ict_logout(request):
    if request.method == "POST":
        logout(request)
    return redirect("ict:login_ict")


def ict_login(request):
    if request.user.is_authenticated:
        profile = getattr(request.user, "ict_profile", None)
        if profile and profile.active:
            return redirect("ict:dashboard")
        if request.user.is_superuser:
            return redirect("superadmin:dashboard")

    next_url = request.GET.get("next") or request.POST.get("next") or ""

    if request.method == "POST":
        form = ICTOfficerLoginForm(request=request, data=request.POST)
        if form.is_valid():
            auth_login(request, form.user)

            if not form.cleaned_data.get("remember"):
                request.session.set_expiry(0)

            if next_url and next_url.startswith("/"):
                return redirect(next_url)
            return redirect("ict:dashboard")
    else:
        form = ICTOfficerLoginForm(request=request)

    return render(request, "ict/login.html", {
        "form": form,
        "next": next_url,
    })


# ============================================================
# SYSTEM STATUS
# ============================================================

@ict_required
def system_status(request):
    """
    System status dashboard:
      - Today's per-constituency report state
      - WhatsApp group status per constituency
      - Cron heartbeat freshness
      - Any FAILED / stuck-SENDING states (last 14 days)
      - Recent send history
    """
    today = timezone.localdate()
    fourteen_days_ago = today - timedelta(days=14)

    if request.user.is_superuser:
        constituencies = Constituency.objects.filter(active=True).order_by("name")
    else:
        constituencies = Constituency.objects.filter(
            id=request.constituency.id, active=True
        ).order_by("name")

    visible_ids = list(constituencies.values_list("id", flat=True))

    todays_states = {
        s.constituency_id: s
        for s in DailyReportState.objects.filter(
            report_date=today, constituency_id__in=visible_ids
        )
    }

    # First active group per constituency (alphabetically); missing if none.
    groups_by_constituency = {}
    for g in (
            WhatsAppGroup.objects
            .filter(constituency_id__in=visible_ids, is_active=True)
            .order_by("name")
    ):
        if g.constituency_id not in groups_by_constituency:
            groups_by_constituency[g.constituency_id] = g

    active_phase = Phase.objects.filter(active=True).first()

    ward_totals = dict(
        Ward.objects
        .filter(constituency_id__in=visible_ids, active=True)
        .values("constituency_id")
        .annotate(n=Count("id"))
        .values_list("constituency_id", "n")
    )

    kit_totals = dict(
        KIEMSKit.objects
        .filter(ward__constituency_id__in=visible_ids, ward__active=True, status=True)
        .values("ward__constituency_id")
        .annotate(n=Count("id"))
        .values_list("ward__constituency_id", "n")
    )

    submitted_kits_today = {}
    if active_phase:
        submitted_kits_today = dict(
            DailyKIEMSEntry.objects
            .filter(
                ward__constituency_id__in=visible_ids,
                phase=active_phase,
                entry_date=today,
                entry_type="REGISTRATION",
            )
            .values("ward__constituency_id")
            .annotate(n=Count("kiems_kit_id", distinct=True))
            .values_list("ward__constituency_id", "n")
        )

    today_rows = []
    for c in constituencies:
        state = todays_states.get(c.id)
        group = groups_by_constituency.get(c.id)

        total_wards = state.total_wards if state else ward_totals.get(c.id, 0)
        submitted_wards = state.submitted_wards if state else 0

        total_kits = kit_totals.get(c.id, 0)
        submitted_kits = min(submitted_kits_today.get(c.id, 0), total_kits) if total_kits else 0

        ward_pct = int((submitted_wards / total_wards) * 100) if total_wards else 0
        kit_pct = int((submitted_kits / total_kits) * 100) if total_kits else 0

        today_rows.append({
            "constituency": c,
            "state": state,
            "total_wards": total_wards,
            "submitted_wards": submitted_wards,
            "ward_progress_pct": ward_pct,
            "total_kits": total_kits,
            "submitted_kits": submitted_kits,
            "kit_progress_pct": kit_pct,
            "has_group": group is not None,
            "group_name": group.name if group else "",
            "group_id": group.group_id if group else "",
        })

    problem_states = list(
        DailyReportState.objects
        .filter(
            report_date__gte=fourteen_days_ago,
            constituency_id__in=visible_ids,
            status__in=["FAILED", "SENDING"],
        )
        .select_related("constituency")
        .order_by("-report_date")[:50]
    )

    history = list(
        DailyReportState.objects
        .filter(
            report_date__gte=fourteen_days_ago,
            constituency_id__in=visible_ids,
            status__in=["SENT", "FAILED"],
        )
        .select_related("constituency")
        .order_by("-sent_at", "-updated_at")[:100]
    )

    counts = {
        "pending": sum(1 for r in today_rows if not r["state"] or r["state"].status == "PENDING"),
        "ready": sum(1 for r in today_rows if r["state"] and r["state"].status == "READY"),
        "sending": sum(1 for r in today_rows if r["state"] and r["state"].status == "SENDING"),
        "sent": sum(1 for r in today_rows if r["state"] and r["state"].status == "SENT"),
        "failed": sum(1 for r in today_rows if r["state"] and r["state"].status == "FAILED"),
    }

    heartbeat = CronHeartbeat.objects.filter(name="send_daily_reports").first()
    heartbeat_stale = (
        True if not heartbeat
        else (timezone.now() - heartbeat.last_run_at).total_seconds() > 600
    )

    context = {
        "constituency": request.constituency,
        "today": today,
        "today_rows": today_rows,
        "problem_states": problem_states,
        "history": history,
        "bot_online": _check_bot_health(),
        "heartbeat": heartbeat,
        "heartbeat_stale": heartbeat_stale,
        "counts": counts,
    }
    return render(request, "ict/system_status.html", context)


@ict_required
@require_POST
def system_status_run_tick(request):
    """Manually fire the reconciler tick (for ops use)."""
    try:
        reaped = reap_stuck_sending_states(stale_after_minutes=10)
        summary = send_ready_reports(limit=20)
        messages.success(
            request,
            f"Tick ran. Reaped {reaped} stuck. "
            f"Sent {summary.get('sent', 0)}, "
            f"failed {summary.get('failed', 0)}, "
            f"skipped {summary.get('skipped', 0)}."
        )
    except Exception as e:
        logger.exception("Manual tick failed")
        messages.error(request, f"Tick failed: {e}")
    return redirect("ict:system_status")


@ict_required
@require_POST
def system_status_retry(request, state_id):
    """
    Force a single FAILED / stuck-SENDING state back to READY.
    Scoped to the officer's own constituency unless they're a superuser.
    """
    qs = DailyReportState.objects.all()
    if not request.user.is_superuser:
        qs = qs.filter(constituency=request.constituency)

    state = get_object_or_404(qs, pk=state_id)

    if state.status not in ("FAILED", "SENDING"):
        messages.info(request, f"State is already {state.status} - nothing to retry.")
        return redirect("ict:system_status")

    state.status = "READY"
    state.attempts = 0
    state.last_error = ""
    state.ready_at = timezone.now()
    state.locked_at = None
    state.locked_by = ""
    state.save(update_fields=[
        "status", "attempts", "last_error", "ready_at", "locked_at", "locked_by",
    ])

    messages.success(request, "State re-armed. It will be sent on the next tick.")
    return redirect("ict:system_status")


@ict_required
@require_POST
def system_status_reevaluate(request, constituency_id, report_date):
    """
    Force a re-evaluation for a specific constituency/date.
    Officers may only re-evaluate their own constituency.
    """
    if request.user.is_superuser:
        c = get_object_or_404(Constituency, pk=constituency_id)
    else:
        c = get_object_or_404(Constituency, pk=constituency_id, id=request.constituency.id)

    parsed_date = _coerce_date(report_date)
    state = reevaluate_constituency_report(c, parsed_date)

    if state:
        messages.success(
            request,
            f"Re-evaluated {c.name}: {state.submitted_wards}/{state.total_wards} wards - {state.status}"
        )
    else:
        messages.warning(request, "Nothing to evaluate (no active phase or no wards).")

    return redirect("ict:system_status")


def _collect_health_snapshot(request):
    """
    Build the full data snapshot that both the HTML preview and the PDF
    report consume. Shared so they never drift.
    """
    today = timezone.localdate()
    since = today - timedelta(days=14)

    if request.user.is_superuser:
        constituencies = Constituency.objects.filter(active=True).order_by("name")
    else:
        constituencies = Constituency.objects.filter(id=request.constituency.id, active=True)

    visible_ids = list(constituencies.values_list("id", flat=True))
    active_phase = Phase.objects.filter(active=True).first()

    today_states = {
        s.constituency_id: s
        for s in DailyReportState.objects.filter(report_date=today)
    }

    today_rows = []
    for c in constituencies:
        state = today_states.get(c.id)

        total_wards = Ward.objects.filter(constituency=c, active=True).count()
        submitted_wards = state.submitted_wards if state else 0

        total_kits = KIEMSKit.objects.filter(
            ward__constituency=c, ward__active=True, status=True
        ).count()
        submitted_kits = (
            DailyKIEMSEntry.objects
            .filter(
                ward__constituency=c,
                phase=active_phase,
                entry_date=today,
                entry_type="REGISTRATION",
            )
            .values("kiems_kit_id")
            .distinct()
            .count()
        ) if active_phase else 0

        ward_pct = int((submitted_wards / total_wards * 100)) if total_wards else 0
        kit_pct = int((submitted_kits / total_kits * 100)) if total_kits else 0

        today_rows.append({
            "constituency": c,
            "state": state,
            "status": state.status if state else "NO_ACTIVITY",
            "status_label": state.get_status_display() if state else "No activity",
            "total_wards": state.total_wards if state else total_wards,
            "submitted_wards": submitted_wards,
            "ward_pct": ward_pct,
            "total_kits": total_kits,
            "submitted_kits": min(submitted_kits, total_kits),
            "kit_pct": kit_pct,
            "ready_at": state.ready_at if state else None,
            "sent_at": state.sent_at if state else None,
        })

    problem_states = list(
        DailyReportState.objects
        .filter(
            report_date__gte=since,
            constituency_id__in=visible_ids,
            status__in=["FAILED", "SENDING"],
        )
        .select_related("constituency")
        .order_by("-report_date")[:100]
    )

    sent_recent = list(
        DailyReportState.objects
        .filter(report_date__gte=since, constituency_id__in=visible_ids, status="SENT")
        .select_related("constituency")
        .order_by("-sent_at")[:100]
    )

    counts = {
        "pending": sum(1 for r in today_rows if r["status"] in ("PENDING", "NO_ACTIVITY")),
        "ready": sum(1 for r in today_rows if r["status"] == "READY"),
        "sending": sum(1 for r in today_rows if r["status"] == "SENDING"),
        "sent": sum(1 for r in today_rows if r["status"] == "SENT"),
        "failed": sum(1 for r in today_rows if r["status"] == "FAILED"),
    }

    active_wards = Ward.objects.filter(constituency_id__in=visible_ids, active=True).count()
    active_kits = KIEMSKit.objects.filter(
        ward__constituency_id__in=visible_ids, ward__active=True, status=True
    ).count()
    active_devices = Device.objects.filter(
        is_burned=True, is_active=True
    ).filter(
        Q(vra__ward__constituency_id__in=visible_ids) | Q(clerk__ward__constituency_id__in=visible_ids)
    ).distinct().count()

    return {
        "generated_at": timezone.localtime().strftime("%d %b %Y, %H:%M:%S"),
        "report_date": today,
        "report_date_iso": today.isoformat(),
        "window_days": 14,
        "active_phase": active_phase,
        "is_superadmin_view": request.user.is_superuser,
        "scope_label": "All Constituencies" if request.user.is_superuser else request.constituency.name,
        "today_rows": today_rows,
        "counts": counts,
        "problem_states": problem_states,
        "sent_recent": sent_recent,
        "totals": {
            "constituencies": constituencies.count(),
            "wards": active_wards,
            "kits": active_kits,
            "devices_authorized": active_devices,
        },
        "bot_online": _check_bot_health(),
    }


@ict_required
def system_health_report_preview(request):
    """HTML preview of the system health report."""
    ctx = _collect_health_snapshot(request)
    return render(request, "ict/system_health_report.html", ctx)


@ict_required
def system_health_report_download(request):
    """Generate the system health report as PDF (via PDF.co), or raw HTML."""
    ctx = _collect_health_snapshot(request)
    fmt = request.GET.get("format", "pdf")

    html_string = render_to_string("ict/system_health_report.html", ctx, request=request)

    if fmt == "html":
        resp = HttpResponse(html_string, content_type="text/html")
        filename = f"system_health_{ctx['report_date_iso']}.html"
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

    try:
        api_key = getattr(settings, "PDF_CO_API_KEY", None)
        if not api_key:
            raise RuntimeError("PDF_CO_API_KEY not configured")

        api_url = f"{getattr(settings, 'PDF_CO_API_URL', 'https://api.pdf.co/v1')}/pdf/convert/from/html"
        payload = json.dumps({
            "name": f"System_Health_{ctx['report_date_iso']}.pdf",
            "html": html_string,
            "margin": "0px",
            "paperSize": "Letter",
            "orientation": "Portrait",
            "printBackground": "true",
            "async": False,
        })
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}

        r = requests.post(api_url, headers=headers, data=payload, timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"PDF.co returned {r.status_code}")

        result = r.json()
        if result.get("error"):
            raise RuntimeError(result["error"])

        pdf_url = result.get("url")
        if not pdf_url:
            raise RuntimeError("No PDF URL returned")

        pdf = requests.get(pdf_url, timeout=30)
        if pdf.status_code != 200:
            raise RuntimeError("Failed to download generated PDF")

        response = HttpResponse(pdf.content, content_type="application/pdf")
        filename = f"System_Health_{ctx['report_date_iso']}.pdf"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    except Exception as e:
        messages.warning(request, f"PDF service unavailable, falling back: {e}")
        resp = HttpResponse(html_string, content_type="text/html")
        filename = f"system_health_{ctx['report_date_iso']}.html"
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp


@csrf_exempt
@require_GET
def cron_send_reports(request):
    """
    HTTP endpoint for external cron (cron-job.org) to trigger the
    daily-report tick. Protected by a shared bearer secret.

    Set CRON_SECRET in Vercel environment variables.
    Request must include: Authorization: Bearer <CRON_SECRET>
    """
    secret = os.environ.get("CRON_SECRET", "").strip()
    if not secret:
        logger.error("CRON_SECRET not configured")
        return JsonResponse({"ok": False, "error": "Server misconfigured"}, status=500)

    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {secret}"
    if not hmac.compare_digest(auth, expected):
        logger.warning("Unauthorized cron attempt from %s", request.META.get("REMOTE_ADDR"))
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=401)

    try:
        summary = run_daily_report_tick()
    except Exception as e:
        logger.exception("Cron tick failed")
        CronHeartbeat.objects.update_or_create(
            name="send_daily_reports",
            defaults={"last_summary": {"error": str(e)}},
        )
        hb = CronHeartbeat.objects.filter(name="send_daily_reports").first()
        if hb:
            hb.consecutive_failures += 1
            hb.save(update_fields=["consecutive_failures"])
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

    hb, _ = CronHeartbeat.objects.get_or_create(name="send_daily_reports")
    hb.total_runs += 1
    hb.consecutive_failures = 0
    hb.last_summary = summary
    hb.save()

    return JsonResponse({"ok": True, "summary": summary})