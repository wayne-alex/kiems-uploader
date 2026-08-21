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
        return JsonResponse({
            "bound": True,
            "vra_id": vra.id,
            "vra_name": vra.name,
            "ward_id": vra.ward_id,
            "ward_name": vra.ward.name
        })
    return JsonResponse({"bound": False})


@require_POST
def bind_ward(request):
    """One VRA per ward – selecting the ward identifies the VRA directly."""
    token = request.POST.get("token")
    ward_id = request.POST.get("ward_id")

    vra = VRA.objects.filter(ward_id=ward_id, active=True).first()
    if not vra:
        return JsonResponse({
            "ok": False,
            "error": "No VRA is registered for this ward. Contact your ICT officer."
        }, status=404)

    if vra.device_token and vra.device_token != token:
        return JsonResponse({
            "ok": False,
            "error": "This ward is already registered on another device. Contact your ICT officer."
        }, status=409)

    if not vra.device_token:
        vra.device_token = token
        vra.save(update_fields=["device_token"])

    return JsonResponse({
        "ok": True,
        "vra_id": vra.id,
        "vra_name": vra.name,
        "ward_id": vra.ward_id,
        "ward_name": vra.ward.name
    })


@require_GET
def kits_with_entries(request):
    """All kits for the VRA's ward, each with today's existing entry (if any) pre-filled."""
    token = request.GET.get("token")

    if not token:
        return JsonResponse({"error": "No token provided", "kits": []}, status=400)

    vra = VRA.objects.filter(device_token=token, active=True).select_related("ward").first()
    if not vra:
        return JsonResponse({"error": "VRA not found or inactive", "kits": []}, status=404)

    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        return JsonResponse({"error": "No active phase found", "kits": []}, status=404)

    today = timezone.localdate()

    # Get all kits for this ward
    kits = KIEMSKit.objects.filter(ward=vra.ward, status=True).order_by('kit_name')

    # Get existing entries for today
    existing = {
        e.kiems_kit_id: e for e in DailyKIEMSEntry.objects.filter(
            kiems_kit__in=kits,
            phase=active_phase,
            entry_date=today,
            vra=vra
        )
    }

    data = []
    for kit in kits:
        entry = existing.get(kit.id)
        # Calculate total from male + female
        total = 0
        if entry:
            total = entry.registered_male + entry.registered_female

        kit_data = {
            "kit_id": kit.id,
            "kit_name": kit.kit_name,
            "serial_no": kit.serial_no,
            "venue": entry.venue if entry else "",
            "registered_male": entry.registered_male if entry else 0,
            "registered_female": entry.registered_female if entry else 0,
            "total_registered": total,
            "total_transferred": entry.total_transferred if entry else 0,
            "total_deleted": entry.total_deleted if entry else 0,
            "has_entry": bool(entry),
        }
        data.append(kit_data)

    return JsonResponse({
        "kits": data,
        "ward_name": vra.ward.name,
        "vra_name": vra.name,
        "kit_count": len(data)
    })


def kiems_entry_view(request):
    wards = Ward.objects.all()
    active_phase = Phase.objects.filter(active=True).first()
    return render(request, "home.html", {"wards": wards, "active_phase": active_phase})


@require_POST
def submit_daily_entries(request):
    """Bulk submit – one row per kit with male/female breakdown."""
    token = request.POST.get("token")

    if not token:
        return JsonResponse({"ok": False, "error": "No token provided"}, status=400)

    vra = get_object_or_404(VRA, device_token=token, active=True)
    active_phase = Phase.objects.filter(active=True).first()

    if not active_phase:
        return JsonResponse({"ok": False, "error": "No active phase found"}, status=404)

    today = timezone.localdate()

    kit_ids = request.POST.getlist("kit_id[]")
    venues = request.POST.getlist("venue[]")
    male_vals = request.POST.getlist("registered_male[]")
    female_vals = request.POST.getlist("registered_female[]")

    errors = {}
    saved = 0

    for i, kit_id in enumerate(kit_ids):
        venue = venues[i].strip() if i < len(venues) else ""

        # Parse values
        male_count = int(male_vals[i]) if i < len(male_vals) and male_vals[i] and male_vals[i].isdigit() else 0
        female_count = int(female_vals[i]) if i < len(female_vals) and female_vals[i] and female_vals[
            i].isdigit() else 0

        # Validate
        if not venue:
            errors[kit_id] = "Venue is required."
            continue

        # Check if at least one person was registered
        if male_count == 0 and female_count == 0:
            errors[kit_id] = "Enter at least one registered voter (Male/Female)."
            continue

        kit = get_object_or_404(KIEMSKit, id=kit_id, ward=vra.ward)

        entry, created = DailyKIEMSEntry.objects.get_or_create(
            kiems_kit=kit,
            phase=active_phase,
            entry_date=today,
            vra=vra,
            defaults={
                "ward": vra.ward,
                "venue": venue,
                "registered_male": male_count,
                "registered_female": female_count,
            },
        )
        if not created:
            entry.venue = venue
            entry.registered_male = male_count
            entry.registered_female = female_count
            entry.edit_count += 1
            entry.save(update_fields=["venue", "registered_male", "registered_female",
                                      "edit_count", "updated_at"])
        saved += 1

    if errors:
        return JsonResponse({"ok": False, "errors": errors, "saved": saved}, status=400)
    return JsonResponse({"ok": True, "saved": saved})