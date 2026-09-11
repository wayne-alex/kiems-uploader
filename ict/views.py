import csv
import io
import json
import os
from datetime import timedelta

import openpyxl
import requests
from django.contrib import messages
from django.contrib.auth import logout, login as auth_login
from django.db.models import Count, Q, Sum
from django.http import JsonResponse, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_GET

from home.models import (
    Ward, VRA, Clerk, KIEMSKit, Device, DeviceBurnLog,
    DailyKIEMSEntry, AuditLog, WhatsAppGroup, WhatsAppSetting, DailyReportState, Constituency, Phase, CronHeartbeat, )
from .decorators import ict_required
from .forms import (
    WardForm, VRAForm, ClerkForm, KIEMSKitForm,
    DailyEntryOfficeForm, WhatsAppSettingForm, ICTOfficerLoginForm,
)


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
    """Authorize a device → is_burned=True."""
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
    """Revoke a device's authorization → is_burned=False."""
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

        # Delete logs first (FK cascade would do it anyway, but explicit is clear)
        DeviceBurnLog.objects.filter(device=device).delete()
        device.delete()

        # Audit trail (device row is gone, so we log against the pk string)
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

        messages.success(request, f"Device {fingerprint[:12]}… deleted.")
        return redirect("ict:device_list")

    # GET fallback (shouldn't normally happen; templates use POST forms)
    return redirect("ict:device_detail", pk=device.pk)


# ---------- Daily Entries ----------

@ict_required
def entry_list(request):
    """
    Daily entries for the logged-in ICT officer's constituency,
    with SuperAdmin-style filters + today's stats + entry-type stats.
    """
    c = request.constituency

    qs = (
        DailyKIEMSEntry.objects.filter(ward__constituency=c)
        .select_related("ward", "vra", "clerk", "kiems_kit", "phase")
    )

    # ---- Filters ----
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    ward_id = request.GET.get("ward")
    kit_id = request.GET.get("kit")
    vra_id = request.GET.get("vra")
    entry_type = request.GET.get("entry_type")  # 'REGISTRATION' | 'VENUE' | ''

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
    if entry_type in ("REGISTRATION", "VENUE"):
        qs = qs.filter(entry_type=entry_type)

    # ---- Totals for filtered set ----
    totals = qs.aggregate(
        male=Sum("registered_male"),
        female=Sum("registered_female"),
        total=Sum("total_registered"),
        transferred=Sum("total_transferred"),
        updated=Sum("total_updated"),
    )

    # Entry type stats
    entry_type_stats = {
        "venue_count": qs.filter(entry_type="VENUE").count(),
        "registration_count": qs.filter(entry_type="REGISTRATION").count(),
        "venue_registered": qs.filter(entry_type="VENUE").aggregate(Sum("total_registered"))[
                                "total_registered__sum"] or 0,
        "registration_registered": qs.filter(entry_type="REGISTRATION").aggregate(Sum("total_registered"))[
                                       "total_registered__sum"] or 0,
    }

    # ---- Today's stats (constituency-wide, unfiltered) ----
    today = timezone.now().date()
    today_entries = DailyKIEMSEntry.objects.filter(
        ward__constituency=c, entry_date=today
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

    # ---- Dropdown options ----
    wards = Ward.objects.filter(constituency=c).order_by("name")
    kits = KIEMSKit.objects.filter(ward__constituency=c).order_by("kit_name")
    vras = VRA.objects.filter(ward__constituency=c, active=True).order_by("name")

    return render(request, "ict/entries/list.html", {
        "entries": qs.order_by("-entry_date", "ward__name")[:500],
        "totals": totals,
        "today_stats": today_stats,
        "entry_type_stats": entry_type_stats,
        "constituency": c,
        "wards": wards,
        "kits": kits,
        "vras": vras,
        "date_from": date_from or "",
        "date_to": date_to or "",
        "ward_id": ward_id or "",
        "kit_id": kit_id or "",
        "vra_id": vra_id or "",
        "entry_type": entry_type or "",
        "is_filtered": any([date_from, date_to, ward_id, kit_id, vra_id, entry_type]),
    })


@ict_required
def entry_edit(request, pk):
    c = request.constituency
    entry = get_object_or_404(DailyKIEMSEntry, pk=pk, ward__constituency=c)
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
@require_GET
def entry_export_excel(request):
    """Export filtered entries to Excel (.xlsx)."""
    c = request.constituency

    qs = DailyKIEMSEntry.objects.filter(
        ward__constituency=c
    ).select_related("ward", "vra", "clerk", "kiems_kit", "phase")

    # Apply the same filters as the list view
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    ward_id = request.GET.get("ward")
    kit_id = request.GET.get("kit")
    vra_id = request.GET.get("vra")
    entry_type = request.GET.get("entry_type")

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
    if entry_type in ("REGISTRATION", "VENUE"):
        qs = qs.filter(entry_type=entry_type)

    qs = qs.order_by("entry_date", "ward__name", "kiems_kit__kit_name")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Daily Entries"

    headers = [
        "Date", "Ward", "Kit", "Kit Serial", "VRA", "Clerk", "Venue", "Type",
        "Male", "Female", "Total", "Transferred", "Updated",
        "Uploaded", "Edit Count",
    ]
    ws.append(headers)

    # Style header row
    from openpyxl.styles import Font, PatternFill
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="16A34A")

    for e in qs:
        ws.append([
            e.entry_date.strftime("%Y-%m-%d") if e.entry_date else "",
            e.ward.name if e.ward else "",
            e.kiems_kit.kit_name if e.kiems_kit else "",
            e.kiems_kit.serial_no if e.kiems_kit else "",
            e.vra.name if e.vra else "",
            e.clerk.name if e.clerk else "",
            e.venue or "",
            "Registration" if e.entry_type == "REGISTRATION" else "Venue Mapping",
            e.registered_male,
            e.registered_female,
            e.total_registered,
            e.total_transferred,
            e.total_updated,
            "Yes" if e.uploaded else "No",
            e.edit_count,
        ])

    # Auto column widths
    for i, h in enumerate(headers, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = max(12, len(h) + 4)

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
    """Export filtered entries to CSV."""
    c = request.constituency
    qs = DailyKIEMSEntry.objects.filter(
        ward__constituency=c
    ).select_related("ward", "vra", "clerk", "kiems_kit")

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

    et = request.GET.get("entry_type")
    if et in ("REGISTRATION", "VENUE"):
        qs = qs.filter(entry_type=et)

    timestamp = timezone.localtime().strftime("%Y%m%d_%H%M%S")
    filename = f"{c.name.replace(' ', '_')}_Entries_{timestamp}.csv"

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    writer = csv.writer(response)
    writer.writerow([
        "Date", "Ward", "Kit", "Serial", "VRA", "Clerk", "Venue", "Type",
        "Male", "Female", "Total", "Transferred", "Updated",
    ])
    for e in qs.order_by("-entry_date"):
        writer.writerow([
            e.entry_date, e.ward.name, e.kiems_kit.kit_name, e.kiems_kit.serial_no,
            e.vra.name, e.clerk.name if e.clerk else "",
            e.venue, e.entry_type,
            e.registered_male, e.registered_female, e.total_registered,
            e.total_transferred, e.total_updated,
        ])
    return response


import json as _json
from django.conf import settings


def _entry_report_context(request):
    """
    Build the context your `ict/entries/report_pdf.html` template expects.
    Applies the same filters as the entry_list view.
    """
    c = request.constituency

    qs = (
        DailyKIEMSEntry.objects.filter(ward__constituency=c)
        .select_related("ward", "phase", "kiems_kit")
    )

    # ---- Filters (mirror entry_list) ----
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")
    ward_id = request.GET.get("ward")
    kit_id = request.GET.get("kit")
    vra_id = request.GET.get("vra")
    entry_type = request.GET.get("entry_type")

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
    if entry_type in ("REGISTRATION", "VENUE"):
        qs = qs.filter(entry_type=entry_type)

    # ---- Grand totals ----
    totals = qs.aggregate(
        registered=Sum("total_registered"),
        male=Sum("registered_male"),
        female=Sum("registered_female"),
        transferred=Sum("total_transferred"),
    )

    # ---- Ward summary ----
    ward_summary = (
        qs.values("ward__name")
        .annotate(
            count=Count("id"),
            male=Sum("registered_male"),
            female=Sum("registered_female"),
            registered=Sum("total_registered"),
            transferred=Sum("total_transferred"),
        )
        .order_by("-registered")
    )

    # ---- Phase summary ----
    phase_summary = (
        qs.values("phase__name")
        .annotate(
            count=Count("id"),
            male=Sum("registered_male"),
            female=Sum("registered_female"),
            registered=Sum("total_registered"),
            transferred=Sum("total_transferred"),
        )
        .order_by("-registered")
    )

    # ---- Kit summary (top 20) ----
    kit_summary = (
        qs.values("kiems_kit__kit_name")
        .annotate(
            count=Count("id"),
            male=Sum("registered_male"),
            female=Sum("registered_female"),
            registered=Sum("total_registered"),
        )
        .order_by("-registered")[:20]
    )

    # ---- Scope string for the subtitle under the title ----
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
    if entry_type == "REGISTRATION":
        parts.append("Type: Registration")
    elif entry_type == "VENUE":
        parts.append("Type: Venue Mapping")

    scope = "  •  ".join(parts)

    return {
        "constituency": c,
        "total_entries": qs.count(),
        "total_registered": totals["registered"] or 0,
        "total_male": totals["male"] or 0,
        "total_female": totals["female"] or 0,
        "total_transferred": totals["transferred"] or 0,
        "ward_summary": list(ward_summary),
        "phase_summary": list(phase_summary),
        "kit_summary": list(kit_summary),
        "scope": scope,
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
        payload = _json.dumps({
            "name": f"{ctx['constituency'].name}_Daily_Report.pdf",
            "html": html_string,
            "margin": "20px",
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
        filename = f"{ctx['constituency'].name.replace(' ', '_')}_Daily_Report_{timezone.localtime().strftime('%Y%m%d')}.pdf"
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    except Exception as e:
        # Fallback to ReportLab
        messages.warning(request, f"Falling back to local PDF: {e}")
        return _entry_pdf_reportlab(request, ctx)


def _entry_pdf_reportlab(request, ctx):
    """Local PDF fallback using ReportLab."""
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

    doc = SimpleDocTemplate(response, pagesize=letter,
                            rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    story = []

    # Title
    story.append(Paragraph(
        f"{ctx['constituency'].name} Constituency — Daily Report",
        ParagraphStyle("T", parent=styles["Title"], fontSize=16,
                       textColor=colors.HexColor("#16a34a"), alignment=TA_CENTER, spaceAfter=4)
    ))
    story.append(Paragraph(
        f"Generated: {ctx['generated_at']}",
        ParagraphStyle("S", parent=styles["Normal"], fontSize=9,
                       textColor=colors.HexColor("#6b7280"), alignment=TA_CENTER, spaceAfter=14)
    ))

    # Totals table
    t = ctx["totals"]
    totals_data = [
        ["Male", "Female", "Total", "Transferred", "Updated"],
        [str(t["male"] or 0), str(t["female"] or 0), str(t["total"] or 0),
         str(t["transferred"] or 0), str(t["updated"] or 0)],
    ]
    tt = Table(totals_data, colWidths=[100, 100, 100, 110, 100])
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

    # Ward summary
    if ctx["by_ward"]:
        story.append(Paragraph("Summary by Ward", styles["Heading4"]))
        ward_data = [["Ward", "Entries", "Male", "Female", "Total", "Transferred"]]
        for w in ctx["by_ward"]:
            ward_data.append([
                w["ward__name"] or "Unknown", str(w["entries"]),
                str(w["male"] or 0), str(w["female"] or 0),
                str(w["total"] or 0), str(w["transferred"] or 0),
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

    # Kit summary
    if ctx["by_kit"]:
        story.append(Paragraph("Top Kits by Registration", styles["Heading4"]))
        kit_data = [["Kit", "Ward", "Entries", "Male", "Female", "Total"]]
        for k in ctx["by_kit"]:
            kit_data.append([
                k["kiems_kit__kit_name"] or "—", k["ward__name"] or "—",
                str(k["entries"]), str(k["male"] or 0),
                str(k["female"] or 0), str(k["total"] or 0),
            ])
        kt = Table(kit_data, colWidths=[120, 100, 60, 60, 60, 70])
        kt.setStyle(TableStyle([
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
        story.append(kt)

    doc.build(story)
    return response


@ict_required
def entry_download_report_preview(request):
    """HTML preview of the PDF daily report."""
    ctx = _entry_report_context(request)
    return render(request, "ict/entries/report_pdf.html", ctx)


@ict_required
@require_POST
def entry_import_csv(request):
    """
    Import daily entries from CSV.
    Required columns: date, ward_id, kit_id, vra_id, male, female
    Optional: transferred, updated, venue, entry_type
    """
    c = request.constituency
    csv_file = request.FILES.get("csv_file")
    if not csv_file:
        messages.error(request, "No CSV file uploaded.")
        return redirect("ict:entry_list")

    try:
        decoded = csv_file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(decoded))

        success = 0
        errors = []
        today = timezone.now().date()

        for i, row in enumerate(reader, start=2):  # row 1 = header
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
                etype = (row.get("entry_type") or "REGISTRATION").strip().upper()

                if not (date_str and ward_id and kit_id and vra_id):
                    errors.append(f"Row {i}: missing required fields")
                    continue

                ward = Ward.objects.filter(pk=ward_id, constituency=c).first()
                kit = KIEMSKit.objects.filter(pk=kit_id, ward__constituency=c).first()
                vra = VRA.objects.filter(pk=vra_id, ward__constituency=c).first()

                if not (ward and kit and vra):
                    errors.append(f"Row {i}: ward/kit/vra not in your constituency")
                    continue

                entry, created = DailyKIEMSEntry.objects.update_or_create(
                    kiems_kit=kit,
                    entry_date=date_str,
                    vra=vra,
                    defaults={
                        "ward": ward,
                        "venue": venue,
                        "registered_male": male,
                        "registered_female": female,
                        "total_registered": male + female,
                        "total_transferred": transferred,
                        "total_updated": updated,
                        "entry_type": etype if etype in ("REGISTRATION", "VENUE") else "REGISTRATION",
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


@ict_required
def notification_list(request):
    c = request.constituency

    # The group actually used for routing today
    constituency_group = (
        WhatsAppGroup.objects
        .filter(constituency=c, is_active=True)
        .order_by("name")
        .first()
    )

    # The officer's private preference (may be None or stale)
    setting = WhatsAppSetting.objects.filter(user=request.user).first()

    return render(request, "ict/notifications/list.html", {
        "constituency": c,
        "constituency_group": constituency_group,
        "selected_group": constituency_group,   # keep name for template compat
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


def _bot_url():
    return getattr(settings, "WHATSAPP_BOT_URL", "http://localhost:3000")


@ict_required
@require_GET
def whatsapp_bot_status(request):
    """Live bot status (mirrors superadmin:whatsapp_bot_status)."""
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
        # Do NOT auto-scope them to a constituency — leave that to the
        # explicit picker, so we don't accidentally claim a shared group.
        for g in raw_groups:
            gid = g.get("id")
            gname = g.get("name", f"Group {gid[:10]}")
            if not gid:
                continue
            try:
                obj, created = WhatsAppGroup.objects.get_or_create(
                    group_id=gid,
                    defaults={
                        "name": gname,
                        "is_active": True,
                        "constituency": None,   # unclaimed until picked
                    },
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
            .filter(
                Q(constituency=c)                 # mine
                | Q(constituency__isnull=True)    # unclaimed or global
            )
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
        return JsonResponse({"success": False, "data": [],
                             "error": "Bot is offline"}, status=503)
    except Exception as e:
        return JsonResponse({"success": False, "data": [], "error": str(e)}, status=500)


@ict_required
@require_POST
def whatsapp_select_group(request):
    """
    Set the WhatsApp group that will receive this constituency's messages.

    Behavior:
      - Assigns the chosen WhatsAppGroup to the officer's constituency.
      - Any other group that was previously scoped to this constituency
        is un-scoped (constituency set to NULL) so a constituency never
        has more than one active target.
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

        # --- Clear selection ---
        if not group_id:
            # Un-scope all of this constituency's groups so nothing routes to them
            WhatsAppGroup.objects.filter(constituency=c).update(constituency=None)
            setting.default_group = None
            setting.save(update_fields=["default_group"])
            return JsonResponse({
                "success": True,
                "message": "Constituency group cleared.",
                "group_id": "",
                "group_name": "",
            })

        # --- Find or create the group ---
        # Match by group_id regardless of current constituency, because the
        # officer may be re-picking a group that's currently assigned to
        # another constituency (e.g. reassigning it) or to NULL.
        group = WhatsAppGroup.objects.filter(group_id=group_id).first()

        if group is None:
            # Brand new — the bot gave us a group we've never seen
            group = WhatsAppGroup.objects.create(
                group_id=group_id,
                name=data.get("group_name") or f"Group {group_id[:12]}",
                is_active=True,
                constituency=c,
            )
        else:
            # If it's already scoped to a *different* constituency,
            # refuse unless the user is a superuser — this prevents
            # accidentally stealing another constituency's group.
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

            # Un-scope any OTHER group currently claiming this constituency
            WhatsAppGroup.objects.filter(
                constituency=c
            ).exclude(pk=group.pk).update(constituency=None)

            # Assign to this constituency
            group.constituency = c
            if data.get("group_name"):
                group.name = data["group_name"]
            group.is_active = True
            group.save(update_fields=["constituency", "name", "is_active"])

        # --- Reflect in the officer's UI preference ---
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

    # Resolve date
    if date_str:
        try:
            from datetime import datetime as _dt
            report_date = _dt.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            report_date = timezone.now().date()
    else:
        report_date = timezone.now().date()

    # Resolve target group
    setting = WhatsAppSetting.objects.filter(user=request.user).first()
    if not setting or not setting.default_group:
        return JsonResponse({
            "success": False,
            "error": "No target group selected. Pick one above first."
        }, status=400)

    # Build message
    if report_type == "ward":
        if not ward_id:
            return JsonResponse({"success": False, "error": "ward_id required"}, status=400)
        ward = Ward.objects.filter(pk=ward_id, constituency=c).first()
        if not ward:
            return JsonResponse({"success": False, "error": "Ward not found in your constituency"}, status=404)
        message = _format_ward_report(ward, report_date)
    else:
        message = _format_constituency_report(c, report_date)

    # Send via bot
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
        from datetime import datetime as _dt
        report_date = _dt.strptime(date_str, "%Y-%m-%d").date() if date_str else timezone.now().date()
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


# ---------- Logout ----------

@ict_required
def ict_logout(request):
    if request.method == "POST":
        logout(request)
    return redirect("ict:login_ict")


def ict_login(request):
    # Already logged in as an ICT officer? Go straight to dashboard.
    if request.user.is_authenticated:
        profile = getattr(request.user, "ict_profile", None)
        if profile and profile.active:
            return redirect("ict:dashboard")
        # Superadmin or other user hitting this URL: send them to their portal
        if request.user.is_superuser:
            return redirect("superadmin:dashboard")

    next_url = request.GET.get("next") or request.POST.get("next") or ""

    if request.method == "POST":
        form = ICTOfficerLoginForm(request=request, data=request.POST)
        if form.is_valid():
            auth_login(request, form.user)

            # "Remember me" → 30 days, otherwise browser-session only
            if not form.cleaned_data.get("remember"):
                request.session.set_expiry(0)

            # Safe redirect: only allow internal paths
            if next_url and next_url.startswith("/"):
                return redirect(next_url)
            return redirect("ict:dashboard")
    else:
        form = ICTOfficerLoginForm(request=request)

    return render(request, "ict/login.html", {
        "form": form,
        "next": next_url,
    })

# ==================== SYSTEM STATUS ====================

import time
import requests
from django.conf import settings

from home.services.daily_report import (
    reap_stuck_sending_states,
    send_ready_reports,
    reevaluate_constituency_report,
    constituency_kit_progress, run_daily_report_tick,  # <- add this helper in daily_report.py (see below)
)


# --- Simple in-process cache for the bot health check ---
_BOT_HEALTH_CACHE = {"ts": 0, "ok": False}
_BOT_HEALTH_TTL_SECONDS = 5


def _check_bot_health():
    """Ping the WhatsApp bot, cached for 5s to survive rapid refreshes."""
    now = time.time()
    if now - _BOT_HEALTH_CACHE["ts"] < _BOT_HEALTH_TTL_SECONDS:
        return _BOT_HEALTH_CACHE["ok"]

    ok = False
    try:
        url = getattr(settings, "WHATSAPP_BOT_URL", "http://localhost:3000")
        r = requests.get(f"{url}/status", timeout=3)
        if r.status_code == 200:
            ok = bool(r.json().get("isReady", False))
    except Exception:
        ok = False

    _BOT_HEALTH_CACHE["ts"] = now
    _BOT_HEALTH_CACHE["ok"] = ok
    return ok


def _coerce_date(value):
    """Accept a date, a YYYY-MM-DD string, or None → today."""
    from datetime import datetime as _dt
    if value is None:
        return timezone.localdate()
    if hasattr(value, "year") and hasattr(value, "month"):
        return value  # already a date
    try:
        return _dt.strptime(str(value), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return timezone.localdate()


@ict_required
def system_status(request):
    """
    System status dashboard:
      - Today's per-constituency report state
      - Any FAILED / stuck-SENDING states (last 14 days)
      - Recent send history
    """
    from home.models import Constituency, Ward

    today = timezone.localdate()
    fourteen_days_ago = today - timedelta(days=14)

    # --- Constituencies the officer can see.
    # ICT officers see their own constituency; superusers see all.
    if request.user.is_superuser:
        constituencies = Constituency.objects.filter(active=True).order_by("name")
    else:
        constituencies = Constituency.objects.filter(
            id=request.constituency.id, active=True
        )

    todays_states = {
        s.constituency_id: s
        for s in DailyReportState.objects.filter(report_date=today)
    }

    today_rows = []
    for c in constituencies:
        state = todays_states.get(c.id)

        total_wards = Ward.objects.filter(constituency=c, active=True).count()
        submitted_wards = state.submitted_wards if state else 0

        # Kit-level progress — the number ops actually cares about
        kit_prog = constituency_kit_progress(c, today)
        total_kits = kit_prog["total"]
        submitted_kits = kit_prog["submitted"]

        today_rows.append({
            "constituency": c,
            "state": state,
            "total_wards": state.total_wards if state else total_wards,
            "submitted_wards": submitted_wards,
            "ward_progress_pct": int(
                (submitted_wards / (state.total_wards if state else total_wards) * 100)
                if (state.total_wards if state else total_wards) else 0
            ),
            "total_kits": total_kits,
            "submitted_kits": submitted_kits,
            "kit_progress_pct": int(
                (submitted_kits / total_kits * 100) if total_kits else 0
            ),
        })

    # --- Problem states (last 14 days, restricted to visible constituencies) ---
    visible_ids = [c.id for c in constituencies]
    problem_states = (
        DailyReportState.objects
        .filter(
            report_date__gte=fourteen_days_ago,
            constituency_id__in=visible_ids,
            status__in=["FAILED", "SENDING"],
        )
        .select_related("constituency")
        .order_by("-report_date")[:50]
    )

    # --- Recent send history ---
    history = (
        DailyReportState.objects
        .filter(
            report_date__gte=fourteen_days_ago,
            constituency_id__in=visible_ids,
            status__in=["SENT", "FAILED"],
        )
        .select_related("constituency")
        .order_by("-sent_at", "-updated_at")[:100]
    )

    context = {
        "constituency": request.constituency,
        "today": today,
        "today_rows": today_rows,
        "problem_states": problem_states,
        "history": history,
        "bot_online": _check_bot_health(),
        "counts": {
            "pending": sum(
                1 for r in today_rows
                if not r["state"] or r["state"].status == "PENDING"
            ),
            "ready": sum(
                1 for r in today_rows
                if r["state"] and r["state"].status == "READY"
            ),
            "sending": sum(
                1 for r in today_rows
                if r["state"] and r["state"].status == "SENDING"
            ),
            "sent": sum(
                1 for r in today_rows
                if r["state"] and r["state"].status == "SENT"
            ),
            "failed": sum(
                1 for r in today_rows
                if r["state"] and r["state"].status == "FAILED"
            ),
        },
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
        messages.error(request, f"Tick failed: {e}")
    return redirect("ict:system_status")


@ict_required
@require_POST
def system_status_retry(request, state_id):
    """
    Force a single FAILED / stuck-SENDING state back to READY.
    Scoped to the officer's own constituency (superusers may retry any).
    """
    qs = DailyReportState.objects.all()
    if not request.user.is_superuser:
        qs = qs.filter(constituency=request.constituency)

    state = get_object_or_404(qs, pk=state_id)

    if state.status not in ("FAILED", "SENDING"):
        messages.info(
            request, f"State is already {state.status} — nothing to retry."
        )
        return redirect("ict:system_status")

    state.status = "READY"
    state.attempts = 0
    state.last_error = ""
    state.ready_at = timezone.now()       # <- re-arm ready_at too
    state.locked_at = None
    state.locked_by = ""
    state.save(update_fields=[
        "status", "attempts", "last_error",
        "ready_at", "locked_at", "locked_by",
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
    from home.models import Constituency

    # Scope the lookup
    if request.user.is_superuser:
        c = get_object_or_404(Constituency, pk=constituency_id)
    else:
        c = get_object_or_404(
            Constituency, pk=constituency_id, id=request.constituency.id
        )

    parsed_date = _coerce_date(report_date)
    state = reevaluate_constituency_report(c, parsed_date)

    if state:
        messages.success(
            request,
            f"Re-evaluated: {state.submitted_wards}/{state.total_wards} "
            f"wards — {state.status}"
        )
    else:
        messages.warning(
            request, "Nothing to evaluate (no active phase or no wards)."
        )

    return redirect("ict:system_status")


def _collect_health_snapshot(request):
    """
    Build the full data snapshot that both the HTML preview and the PDF
    report consume. Shared so they never drift.
    """
    today = timezone.localdate()
    since = today - timedelta(days=14)

    # Scope
    if request.user.is_superuser:
        constituencies = Constituency.objects.filter(active=True).order_by("name")
    else:
        constituencies = Constituency.objects.filter(
            id=request.constituency.id, active=True
        )

    visible_ids = list(constituencies.values_list("id", flat=True))
    active_phase = Phase.objects.filter(active=True).first()

    # --- Today per constituency ---
    today_states = {
        s.constituency_id: s
        for s in DailyReportState.objects.filter(report_date=today)
    }

    today_rows = []
    for c in constituencies:
        state = today_states.get(c.id)

        total_wards = Ward.objects.filter(constituency=c, active=True).count()
        submitted_wards = state.submitted_wards if state else 0

        # Kit-level progress
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

    # --- Problem states (last 14 days) ---
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

    # --- Recently sent (last 14 days) ---
    sent_recent = list(
        DailyReportState.objects
        .filter(
            report_date__gte=since,
            constituency_id__in=visible_ids,
            status="SENT",
        )
        .select_related("constituency")
        .order_by("-sent_at")[:100]
    )

    # --- Summary counts ---
    counts = {
        "pending":  sum(1 for r in today_rows if r["status"] in ("PENDING", "NO_ACTIVITY")),
        "ready":    sum(1 for r in today_rows if r["status"] == "READY"),
        "sending":  sum(1 for r in today_rows if r["status"] == "SENDING"),
        "sent":     sum(1 for r in today_rows if r["status"] == "SENT"),
        "failed":   sum(1 for r in today_rows if r["status"] == "FAILED"),
    }

    # --- Aggregate enrolment totals for context ---
    active_wards = Ward.objects.filter(constituency_id__in=visible_ids, active=True).count()
    active_kits  = KIEMSKit.objects.filter(
        ward__constituency_id__in=visible_ids, ward__active=True, status=True
    ).count()
    active_devices = Device.objects.filter(
        is_burned=True, is_active=True
    ).filter(
        Q(vra__ward__constituency_id__in=visible_ids)
        | Q(clerk__ward__constituency_id__in=visible_ids)
    ).distinct().count()

    # --- Bot health ---
    bot_online = _check_bot_health()

    return {
        "generated_at": timezone.localtime().strftime("%d %b %Y, %H:%M:%S"),
        "report_date": today,
        "report_date_iso": today.isoformat(),
        "window_days": 14,
        "active_phase": active_phase,
        "is_superadmin_view": request.user.is_superuser,
        "scope_label": (
            "All Constituencies" if request.user.is_superuser
            else request.constituency.name
        ),
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
        "bot_online": bot_online,
    }


@ict_required
def system_health_report_preview(request):
    """HTML preview of the system health report."""
    ctx = _collect_health_snapshot(request)
    return render(request, "ict/system_health_report.html", ctx)


@ict_required
def system_health_report_download(request):
    """
    Generate the system health report as PDF.
    Uses PDF.co, falls back to ReportLab if configured, then HTML download.
    """
    ctx = _collect_health_snapshot(request)
    fmt = request.GET.get("format", "pdf")

    # --- Raw HTML download ---
    if fmt == "html":
        html = render_to_string("ict/system_health_report.html", ctx, request=request)
        resp = HttpResponse(html, content_type="text/html")
        filename = f"system_health_{ctx['report_date_iso']}.html"
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp

    # --- PDF via PDF.co ---
    html_string = render_to_string("ict/system_health_report.html", ctx, request=request)

    try:
        api_key = getattr(settings, "PDF_CO_API_KEY", None)
        if not api_key:
            raise RuntimeError("PDF_CO_API_KEY not configured")

        api_url = (
            f"{getattr(settings, 'PDF_CO_API_URL', 'https://api.pdf.co/v1')}"
            "/pdf/convert/from/html"
        )
        payload = json.dumps({
            "name": f"System_Health_{ctx['report_date_iso']}.pdf",
            "html": html_string,
            "margin": "0px",
            "paperSize": "Letter",
            "orientation": "Portrait",
            "printBackground": "true",
            "async": False,
        })
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
        }

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
        # Fall back to raw HTML download so the user still gets something
        resp = HttpResponse(html_string, content_type="text/html")
        filename = f"system_health_{ctx['report_date_iso']}.html"
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp
import logging
logger = logging.getLogger(__name__)


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
        return JsonResponse(
            {"ok": False, "error": "Server misconfigured"}, status=500
        )

    auth = request.headers.get("Authorization", "")
    expected = f"Bearer {secret}"
    # Constant-time compare
    import hmac
    if not hmac.compare_digest(auth, expected):
        logger.warning("Unauthorized cron attempt from %s", request.META.get("REMOTE_ADDR"))
        return JsonResponse({"ok": False, "error": "Unauthorized"}, status=401)

    try:
        summary = run_daily_report_tick()
    except Exception as e:
        logger.exception("Cron tick failed")
        CronHeartbeat.objects.update_or_create(
            name="send_daily_reports",
            defaults={
                "last_summary": {"error": str(e)},
            },
        )
        # Increment consecutive failures
        hb = CronHeartbeat.objects.filter(name="send_daily_reports").first()
        if hb:
            hb.consecutive_failures += 1
            hb.save(update_fields=["consecutive_failures"])
        return JsonResponse({"ok": False, "error": str(e)}, status=500)

    # Record successful run
    hb, _ = CronHeartbeat.objects.get_or_create(name="send_daily_reports")
    hb.total_runs += 1
    hb.consecutive_failures = 0
    hb.last_summary = summary
    hb.save()

    return JsonResponse({"ok": True, "summary": summary})



# ============================================================
# MAIN VIEW
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

    # ---------- Scope: superuser sees all, ICT sees their own ----------
    if request.user.is_superuser:
        constituencies = Constituency.objects.filter(active=True).order_by("name")
    else:
        constituencies = Constituency.objects.filter(
            id=request.constituency.id, active=True
        ).order_by("name")

    visible_ids = list(constituencies.values_list("id", flat=True))

    # ---------- Today's report states for visible constituencies ----------
    todays_states = {
        s.constituency_id: s
        for s in DailyReportState.objects.filter(
            report_date=today, constituency_id__in=visible_ids
        )
    }

    # ---------- Today's WhatsApp group per constituency ----------
    # Pick the first active group for each constituency (alphabetically).
    # If none, mark as missing.
    groups_by_constituency = {}
    for g in (
        WhatsAppGroup.objects
        .filter(constituency_id__in=visible_ids, is_active=True)
        .order_by("name")
    ):
        # First one wins
        if g.constituency_id not in groups_by_constituency:
            groups_by_constituency[g.constituency_id] = g

    # ---------- Build the today grid ----------
    active_phase = Phase.objects.filter(active=True).first()

    # Precompute ward + kit totals in bulk to avoid N+1 on a big list
    ward_totals = dict(
        Ward.objects
        .filter(constituency_id__in=visible_ids, active=True)
        .values("constituency_id")
        .annotate(n=Count("id"))
        .values_list("constituency_id", "n")
    )

    # Kits per constituency
    kit_totals = dict(
        KIEMSKit.objects
        .filter(
            ward__constituency_id__in=visible_ids,
            ward__active=True,
            status=True,
        )
        .values("ward__constituency_id")
        .annotate(n=Count("id"))
        .values_list("ward__constituency_id", "n")
    )

    # Kits submitted today per constituency
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

        total_wards = (
            state.total_wards if state
            else ward_totals.get(c.id, 0)
        )
        submitted_wards = state.submitted_wards if state else 0

        total_kits = kit_totals.get(c.id, 0)
        submitted_kits = submitted_kits_today.get(c.id, 0)
        if submitted_kits > total_kits:
            submitted_kits = total_kits

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
            # WhatsApp group status
            "has_group": group is not None,
            "group_name": group.name if group else "",
            "group_id": group.group_id if group else "",
        })

    # ---------- Problem states (FAILED / stuck SENDING, last 14 days) ----------
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

    # ---------- Recent send history ----------
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

    # ---------- Summary counts ----------
    counts = {
        "pending": sum(
            1 for r in today_rows
            if not r["state"] or r["state"].status == "PENDING"
        ),
        "ready": sum(
            1 for r in today_rows
            if r["state"] and r["state"].status == "READY"
        ),
        "sending": sum(
            1 for r in today_rows
            if r["state"] and r["state"].status == "SENDING"
        ),
        "sent": sum(
            1 for r in today_rows
            if r["state"] and r["state"].status == "SENT"
        ),
        "failed": sum(
            1 for r in today_rows
            if r["state"] and r["state"].status == "FAILED"
        ),
    }

    # ---------- Cron heartbeat ----------
    heartbeat = CronHeartbeat.objects.filter(name="send_daily_reports").first()
    heartbeat_stale = (
        True if not heartbeat
        else (timezone.now() - heartbeat.last_run_at).total_seconds() > 600  # 10 min
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


# ============================================================
# POST ACTIONS
# ============================================================

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
        messages.info(
            request, f"State is already {state.status} — nothing to retry."
        )
        return redirect("ict:system_status")

    state.status = "READY"
    state.attempts = 0
    state.last_error = ""
    state.ready_at = timezone.now()   # re-arm ready_at so the tick picks it up
    state.locked_at = None
    state.locked_by = ""
    state.save(update_fields=[
        "status", "attempts", "last_error",
        "ready_at", "locked_at", "locked_by",
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
        c = get_object_or_404(
            Constituency, pk=constituency_id, id=request.constituency.id
        )

    parsed_date = _coerce_date(report_date)
    state = reevaluate_constituency_report(c, parsed_date)

    if state:
        messages.success(
            request,
            f"Re-evaluated {c.name}: "
            f"{state.submitted_wards}/{state.total_wards} wards — {state.status}"
        )
    else:
        messages.warning(
            request, "Nothing to evaluate (no active phase or no wards)."
        )

    return redirect("ict:system_status")


# ==================== SYSTEM STATUS ====================

import time
import requests
import logging

from django.conf import settings
from django.db.models import Count
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET

from datetime import timedelta

from home.models import (
    Ward, KIEMSKit, DailyKIEMSEntry, DailyReportState,
    Constituency, WhatsAppGroup, CronHeartbeat,
)
from home.services.daily_report import (
    reap_stuck_sending_states,
    send_ready_reports,
    reevaluate_constituency_report,
    constituency_kit_progress,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# Bot health with a short in-process cache
# ------------------------------------------------------------
_BOT_HEALTH_CACHE = {"ts": 0.0, "ok": False}
_BOT_HEALTH_TTL_SECONDS = 5


def _check_bot_health():
    """Ping the WhatsApp bot, cached for 5s to survive rapid page refreshes."""
    now = time.time()
    if now - _BOT_HEALTH_CACHE["ts"] < _BOT_HEALTH_TTL_SECONDS:
        return _BOT_HEALTH_CACHE["ok"]

    ok = False
    try:
        url = getattr(settings, "WHATSAPP_BOT_URL", "http://localhost:3000")
        r = requests.get(f"{url}/status", timeout=3)
        if r.status_code == 200:
            ok = bool(r.json().get("isReady", False))
    except Exception:
        ok = False

    _BOT_HEALTH_CACHE["ts"] = now
    _BOT_HEALTH_CACHE["ok"] = ok
    return ok


# ------------------------------------------------------------
# Utility: accept date, string, or None
# ------------------------------------------------------------
def _coerce_date(value):
    """Accept a date object, a 'YYYY-MM-DD' string, or None → today."""
    from datetime import datetime as _dt
    if value is None:
        return timezone.localdate()
    if hasattr(value, "year") and hasattr(value, "month"):
        return value  # already a date
    try:
        return _dt.strptime(str(value), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return timezone.localdate()


# ============================================================
# MAIN VIEW
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

    # ---------- Scope: superuser sees all, ICT sees their own ----------
    if request.user.is_superuser:
        constituencies = Constituency.objects.filter(active=True).order_by("name")
    else:
        constituencies = Constituency.objects.filter(
            id=request.constituency.id, active=True
        ).order_by("name")

    visible_ids = list(constituencies.values_list("id", flat=True))

    # ---------- Today's report states for visible constituencies ----------
    todays_states = {
        s.constituency_id: s
        for s in DailyReportState.objects.filter(
            report_date=today, constituency_id__in=visible_ids
        )
    }

    # ---------- Today's WhatsApp group per constituency ----------
    # Pick the first active group for each constituency (alphabetically).
    # If none, mark as missing.
    groups_by_constituency = {}
    for g in (
        WhatsAppGroup.objects
        .filter(constituency_id__in=visible_ids, is_active=True)
        .order_by("name")
    ):
        # First one wins
        if g.constituency_id not in groups_by_constituency:
            groups_by_constituency[g.constituency_id] = g

    # ---------- Build the today grid ----------
    active_phase = Phase.objects.filter(active=True).first()

    # Precompute ward + kit totals in bulk to avoid N+1 on a big list
    ward_totals = dict(
        Ward.objects
        .filter(constituency_id__in=visible_ids, active=True)
        .values("constituency_id")
        .annotate(n=Count("id"))
        .values_list("constituency_id", "n")
    )

    # Kits per constituency
    kit_totals = dict(
        KIEMSKit.objects
        .filter(
            ward__constituency_id__in=visible_ids,
            ward__active=True,
            status=True,
        )
        .values("ward__constituency_id")
        .annotate(n=Count("id"))
        .values_list("ward__constituency_id", "n")
    )

    # Kits submitted today per constituency
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

        total_wards = (
            state.total_wards if state
            else ward_totals.get(c.id, 0)
        )
        submitted_wards = state.submitted_wards if state else 0

        total_kits = kit_totals.get(c.id, 0)
        submitted_kits = submitted_kits_today.get(c.id, 0)
        if submitted_kits > total_kits:
            submitted_kits = total_kits

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
            # WhatsApp group status
            "has_group": group is not None,
            "group_name": group.name if group else "",
            "group_id": group.group_id if group else "",
        })

    # ---------- Problem states (FAILED / stuck SENDING, last 14 days) ----------
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

    # ---------- Recent send history ----------
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

    # ---------- Summary counts ----------
    counts = {
        "pending": sum(
            1 for r in today_rows
            if not r["state"] or r["state"].status == "PENDING"
        ),
        "ready": sum(
            1 for r in today_rows
            if r["state"] and r["state"].status == "READY"
        ),
        "sending": sum(
            1 for r in today_rows
            if r["state"] and r["state"].status == "SENDING"
        ),
        "sent": sum(
            1 for r in today_rows
            if r["state"] and r["state"].status == "SENT"
        ),
        "failed": sum(
            1 for r in today_rows
            if r["state"] and r["state"].status == "FAILED"
        ),
    }

    # ---------- Cron heartbeat ----------
    heartbeat = CronHeartbeat.objects.filter(name="send_daily_reports").first()
    heartbeat_stale = (
        True if not heartbeat
        else (timezone.now() - heartbeat.last_run_at).total_seconds() > 600  # 10 min
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


# ============================================================
# POST ACTIONS
# ============================================================

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
        messages.info(
            request, f"State is already {state.status} — nothing to retry."
        )
        return redirect("ict:system_status")

    state.status = "READY"
    state.attempts = 0
    state.last_error = ""
    state.ready_at = timezone.now()   # re-arm ready_at so the tick picks it up
    state.locked_at = None
    state.locked_by = ""
    state.save(update_fields=[
        "status", "attempts", "last_error",
        "ready_at", "locked_at", "locked_by",
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
        c = get_object_or_404(
            Constituency, pk=constituency_id, id=request.constituency.id
        )

    parsed_date = _coerce_date(report_date)
    state = reevaluate_constituency_report(c, parsed_date)

    if state:
        messages.success(
            request,
            f"Re-evaluated {c.name}: "
            f"{state.submitted_wards}/{state.total_wards} wards — {state.status}"
        )
    else:
        messages.warning(
            request, "Nothing to evaluate (no active phase or no wards)."
        )

    return redirect("ict:system_status")
