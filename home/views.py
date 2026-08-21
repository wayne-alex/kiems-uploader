from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import Ward, VRA, Phase, KIEMSKit, DailyKIEMSEntry


@require_GET
def resolve_vra(request):
    token = request.GET.get("token")
    vra = VRA.objects.filter(device_token=token, active=True).select_related("ward").first()
    if vra:
        return JsonResponse({"bound": True, "vra_id": vra.id, "vra_name": vra.name,
                              "ward_id": vra.ward_id, "ward_name": vra.ward.name})
    return JsonResponse({"bound": False})


@require_POST
def bind_ward(request):
    """One VRA per ward — selecting the ward identifies the VRA directly."""
    token = request.POST.get("token")
    ward_id = request.POST.get("ward_id")

    vra = VRA.objects.filter(ward_id=ward_id, active=True).first()
    if not vra:
        return JsonResponse({"ok": False, "error": "No VRA is registered for this ward. Contact your ICT officer."}, status=404)

    if vra.device_token and vra.device_token != token:
        return JsonResponse(
            {"ok": False, "error": "This ward is already registered on another device. Contact your ICT officer."},
            status=409,
        )

    if not vra.device_token:
        vra.device_token = token
        vra.save(update_fields=["device_token"])

    return JsonResponse({"ok": True, "vra_id": vra.id, "vra_name": vra.name,
                          "ward_id": vra.ward_id, "ward_name": vra.ward.name})


@require_GET
def kits_with_entries(request):
    """All kits for the VRA's ward, each with today's existing entry (if any) pre-filled."""
    token = request.GET.get("token")
    vra = get_object_or_404(VRA, device_token=token, active=True)
    active_phase = Phase.objects.filter(active=True).first()
    today = timezone.localdate()

    kits = KIEMSKit.objects.filter(ward=vra.ward, status=True)
    existing = {
        e.kiems_kit_id: e for e in DailyKIEMSEntry.objects.filter(
            kiems_kit__in=kits, phase=active_phase, entry_date=today, vra=vra
        )
    }

    data = []
    for kit in kits:
        entry = existing.get(kit.id)
        data.append({
            "kit_id": kit.id,
            "kit_name": kit.kit_name,
            "serial_no": kit.serial_no,
            "venue": entry.venue if entry else "",
            "total_registered": entry.total_registered if entry else "",
            "total_transferred": entry.total_transferred if entry else 0,
            "total_deleted": entry.total_deleted if entry else 0,
            "has_entry": bool(entry),
        })
    return JsonResponse({"kits": data})


def kiems_entry_view(request):
    wards = Ward.objects.all()
    active_phase = Phase.objects.filter(active=True).first()
    return render(request, "home.html", {"wards": wards, "active_phase": active_phase})


@require_POST
def submit_daily_entries(request):
    """Bulk submit — one row per kit, submitted together."""
    token = request.POST.get("token")
    vra = get_object_or_404(VRA, device_token=token, active=True)
    active_phase = Phase.objects.filter(active=True).first()
    today = timezone.localdate()

    kit_ids = request.POST.getlist("kit_id[]")
    venues = request.POST.getlist("venue[]")
    registered_vals = request.POST.getlist("total_registered[]")

    errors = {}
    saved = 0
    for kit_id, venue, registered in zip(kit_ids, venues, registered_vals):
        venue = venue.strip()
        registered = registered.strip()
        if not venue or not registered.isdigit():
            errors[kit_id] = "Enter a venue and a valid number."
            continue

        kit = get_object_or_404(KIEMSKit, id=kit_id, ward=vra.ward)
        entry, created = DailyKIEMSEntry.objects.get_or_create(
            kiems_kit=kit, phase=active_phase, entry_date=today, vra=vra,
            defaults={"ward": vra.ward, "venue": venue, "total_registered": int(registered)},
        )
        if not created:
            entry.venue = venue
            entry.total_registered = int(registered)
            entry.edit_count += 1
            entry.save(update_fields=["venue", "total_registered", "edit_count", "updated_at"])
        saved += 1

    if errors:
        return JsonResponse({"ok": False, "errors": errors, "saved": saved}, status=400)
    return JsonResponse({"ok": True, "saved": saved})