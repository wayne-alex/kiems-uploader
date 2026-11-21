import json
from datetime import date, datetime, timedelta

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import (
    require_http_methods, require_GET, require_POST,
)

from home.models import (
    MovementSchedule,
    Ward, VRA, Clerk, KIEMSKit, Phase, DailyKIEMSEntry,
    Device, Constituency,
)
from home.services.whatsapp_queue import enqueue_whatsapp, drain_async

MOVEMENT_REQUIRE_BOUND_DEVICE = getattr(settings, "MOVEMENT_REQUIRE_BOUND_DEVICE", True)


# ═══════════════════════════════════════════════════════════
# WhatsApp message formatters
# ═══════════════════════════════════════════════════════════

def format_vra_submission_message(entry, is_update=False):
    """WhatsApp text for a single registration submission."""
    total = (entry.registered_male or 0) + (entry.registered_female or 0)
    if total == 0:
        return None

    header = "UPDATED" if is_update else "CONFIRMED ✓"
    msg = f"{entry.ward.name.upper()} {header}\n"
    msg += (
        f"{entry.kiems_kit.kit_name}:"
        f"Male:{entry.registered_male} Female:{entry.registered_female} ={total}"
    )
    if entry.total_transferred and entry.total_transferred > 0:
        msg += f"\nTransferred: {entry.total_transferred}"
    return msg


def format_grand_total_message(entries, total_wards):
    """WhatsApp text for the constituency-wide daily report."""
    today = timezone.now().date()
    totals = entries.aggregate(
        male=Sum("registered_male"),
        female=Sum("registered_female"),
        registered=Sum("total_registered"),
        transferred=Sum("total_transferred"),
    )
    ward_data = entries.values("ward__name").annotate(
        male=Sum("registered_male"),
        female=Sum("registered_female"),
        total=Sum("total_registered"),
    ).order_by("ward__name")

    msg = f"DAILY REPORT - {today.strftime('%d %b %Y')}\n"
    msg += f"All {total_wards} Wards Submitted\n\n"
    for w in ward_data:
        msg += f"{w['ward__name']}: Male: {w['male']} Female: {w['female']} = {w['total']}\n"
    msg += (
        f"\nTOTAL: Male: {totals['male'] or 0} "
        f"Female: {totals['female'] or 0} = {totals['registered'] or 0}"
    )
    if totals["transferred"]:
        msg += f" | Transferred: {totals['transferred']}"
    return msg


def _enqueue_whatsapp_for_entries(entries, kind):
    """Queue WhatsApp confirmations for entries that have voter counts."""
    for entry in entries:
        total = (entry.registered_male or 0) + (entry.registered_female or 0)
        if total <= 0:
            continue
        msg = format_vra_submission_message(entry, is_update=False)
        if msg and entry.ward.constituency_id:
            enqueue_whatsapp(
                constituency=entry.ward.constituency,
                message=msg,
                kind=kind,
            )


# ═══════════════════════════════════════════════════════════
# Device / auth helpers
# ═══════════════════════════════════════════════════════════

def get_vra_from_request(request):
    """Resolve VRA from fingerprint (preferred) or legacy token."""
    fingerprint = request.GET.get("fingerprint") or request.POST.get("fingerprint")
    if fingerprint:
        try:
            device = Device.objects.select_related("vra", "vra__ward").get(
                fingerprint=fingerprint, is_burned=True, is_active=True,
            )
            if device.vra:
                return device.vra
        except Device.DoesNotExist:
            pass

    token = request.GET.get("token") or request.POST.get("token")
    if token:
        return VRA.objects.filter(device_token=token, active=True).select_related("ward").first()
    return None


def get_clerk_from_request(request):
    """Resolve Clerk from fingerprint (preferred) or legacy token."""
    fingerprint = request.GET.get("fingerprint") or request.POST.get("fingerprint")
    if fingerprint:
        try:
            device = Device.objects.select_related("clerk", "clerk__ward").get(
                fingerprint=fingerprint, is_burned=True, is_active=True,
            )
            if device.clerk:
                return device.clerk
        except Device.DoesNotExist:
            pass

    token = request.GET.get("token") or request.POST.get("token")
    if token:
        return Clerk.objects.filter(device_token=token, active=True).select_related("ward").first()
    return None


def get_device_from_fingerprint(fingerprint):
    if not fingerprint:
        return None
    try:
        return Device.objects.select_related(
            "vra", "vra__ward", "clerk", "clerk__ward",
        ).get(fingerprint=fingerprint, is_burned=True, is_active=True)
    except Device.DoesNotExist:
        return None


def _resolve_submitter(fingerprint, token):
    """Return (vra, clerk) for a device or token. Either may be None."""
    vra = clerk = None

    if fingerprint:
        device = (
            Device.objects
            .select_related("vra", "vra__ward", "clerk", "clerk__ward")
            .filter(fingerprint=fingerprint, is_burned=True, is_active=True)
            .first()
        )
        if device:
            vra, clerk = device.vra, device.clerk

    if not (vra or clerk) and token:
        vra = VRA.objects.select_related("ward").filter(
            device_token=token, active=True,
        ).first()
        if not vra:
            clerk = Clerk.objects.select_related("ward").filter(
                device_token=token, active=True,
            ).first()

    return vra, clerk


# ═══════════════════════════════════════════════════════════
# Page renders
# ═══════════════════════════════════════════════════════════

def kiems_entry_view(request):
    """Main entry page for VRA."""
    constituencies = Constituency.objects.filter(active=True).order_by("name")

    bound_constituency = None
    vra = get_vra_from_request(request)
    if vra and vra.ward and getattr(vra.ward, "constituency_id", None):
        bound_constituency = vra.ward.constituency

    wards = Ward.objects.select_related("constituency").order_by(
        "constituency__name", "name",
    )
    active_phase = Phase.objects.filter(active=True).first()

    return render(request, "home.html", {
        "constituencies": constituencies,
        "wards": wards,
        "bound_constituency": bound_constituency,
        "active_phase": active_phase,
    })


def clerk_venue_mapping_view(request):
    return render(request, "clerk_mapping.html", {
        "active_phase": Phase.objects.filter(active=True).first(),
    })


def movement_schedule_view(request):
    """Client page for movement schedule entry."""
    bound_constituency = None
    bound_ward = None
    bound_clerk = None
    bound_vra = None

    vra = get_vra_from_request(request)
    if vra and vra.ward:
        bound_ward = vra.ward
        bound_constituency = vra.ward.constituency
        bound_vra = vra
    else:
        clerk = get_clerk_from_request(request)
        if clerk and clerk.ward:
            bound_ward = clerk.ward
            bound_constituency = clerk.ward.constituency
            bound_clerk = clerk

    tomorrow = timezone.localdate() + timedelta(days=1)

    return render(request, "movement_schedule.html", {
        "constituencies": Constituency.objects.filter(active=True).order_by("name"),
        "wards": Ward.objects.select_related("constituency").order_by(
            "constituency__name", "name",
        ),
        "bound_constituency": bound_constituency,
        "bound_ward": bound_ward,
        "bound_vra": bound_vra,
        "bound_clerk": bound_clerk,
        "active_phase": Phase.objects.filter(active=True).first(),
        "schedule_date": tomorrow.isoformat(),
    })


def download_app_view(request):
    return render(request, "download_app.html", {"pwa_url": settings.PWA_URL})


# ═══════════════════════════════════════════════════════════
# Device registration / binding
# ═══════════════════════════════════════════════════════════

@require_GET
def wards_by_constituency(request):
    constituency_id = request.GET.get("constituency_id")
    if not constituency_id:
        return JsonResponse({"ok": False, "wards": []}, status=400)
    wards = (
        Ward.objects.filter(constituency_id=constituency_id)
        .order_by("name")
        .values("id", "name", "code")
    )
    return JsonResponse({"ok": True, "wards": list(wards)})


@require_GET
def constituencies_list(request):
    qs = Constituency.objects.filter(active=True).order_by("name").values("id", "name")
    return JsonResponse({"ok": True, "constituencies": list(qs)})


@csrf_exempt
@require_POST
def register_device(request):
    """Register / refresh a device by fingerprint; auto-authorize by default."""
    try:
        data = json.loads(request.body)
        fingerprint = data.get("fingerprint")
        device_info = data.get("device_info", {})

        if not fingerprint:
            return JsonResponse({"ok": False, "error": "Device fingerprint required"}, status=400)

        device, created = Device.objects.get_or_create(
            fingerprint=fingerprint,
            defaults={
                "user_agent": request.META.get("HTTP_USER_AGENT", ""),
                "ip_address": request.META.get("REMOTE_ADDR"),
                "screen_resolution": device_info.get("screenResolution", ""),
                "language": device_info.get("language", ""),
                "platform": device_info.get("platform", ""),
                "timezone": device_info.get("timezone", ""),
                "is_burned": True,
                "is_active": True,
            },
        )

        if not created:
            device.user_agent = request.META.get("HTTP_USER_AGENT", device.user_agent)
            device.ip_address = request.META.get("REMOTE_ADDR", device.ip_address)
            device.screen_resolution = device_info.get("screenResolution", device.screen_resolution)
            device.language = device_info.get("language", device.language)
            device.platform = device_info.get("platform", device.platform)
            device.timezone = device_info.get("timezone", device.timezone)
            device.save(update_fields=[
                "user_agent", "ip_address", "screen_resolution",
                "language", "platform", "timezone",
            ])

        vra = VRA.objects.filter(
            Q(device_token=fingerprint) | Q(device_fingerprint=fingerprint),
            active=True,
        ).first()
        if vra and not device.vra:
            device.vra = vra
            device.save(update_fields=["vra"])
            if not vra.device_fingerprint:
                vra.device_fingerprint = fingerprint
                vra.save(update_fields=["device_fingerprint"])

        clerk = Clerk.objects.filter(
            Q(device_token=fingerprint) | Q(device_fingerprint=fingerprint),
            active=True,
        ).first()
        if clerk and not device.clerk:
            device.clerk = clerk
            device.save(update_fields=["clerk"])
            if not clerk.device_fingerprint:
                clerk.device_fingerprint = fingerprint
                clerk.save(update_fields=["device_fingerprint"])

        return JsonResponse({
            "ok": True,
            "device_id": device.id,
            "is_burned": device.is_burned,
            "is_active": device.is_active,
            "vra_id": device.vra_id,
            "vra_name": device.vra.name if device.vra else None,
            "ward_id": device.vra.ward_id if device.vra else None,
            "ward_name": device.vra.ward.name if device.vra else None,
            "clerk_id": device.clerk_id if device.clerk else None,
            "clerk_name": device.clerk.name if device.clerk else None,
            "created": created,
            "auto_authorized": True,
        })
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


@require_GET
def check_device_status(request):
    fingerprint = request.GET.get("fingerprint")
    if not fingerprint:
        return JsonResponse({"ok": False, "error": "Fingerprint required"}, status=400)

    try:
        device = Device.objects.select_related(
            "vra", "vra__ward", "clerk", "clerk__ward",
        ).get(fingerprint=fingerprint)
        return JsonResponse({
            "ok": True,
            "is_authorized": device.is_burned,
            "is_active": device.is_active,
            "vra_id": device.vra_id,
            "vra_name": device.vra.name if device.vra else None,
            "ward_id": device.vra.ward_id if device.vra else None,
            "ward_name": device.vra.ward.name if device.vra else None,
            "clerk_id": device.clerk_id if device.clerk else None,
            "clerk_name": device.clerk.name if device.clerk else None,
            "authorized_date": device.burn_date.isoformat() if device.burn_date else None,
            "device_id": device.id,
        })
    except Device.DoesNotExist:
        return JsonResponse({
            "ok": True,
            "is_authorized": False,
            "is_active": False,
            "vra_id": None, "vra_name": None,
            "ward_id": None, "ward_name": None,
            "clerk_id": None, "clerk_name": None,
        })


@require_GET
def resolve_vra(request):
    fingerprint = request.GET.get("fingerprint")
    if fingerprint:
        try:
            device = Device.objects.select_related("vra", "vra__ward").get(
                fingerprint=fingerprint, is_burned=True, is_active=True,
            )
            if device.vra:
                return JsonResponse({
                    "bound": True,
                    "vra_id": device.vra.id,
                    "vra_name": device.vra.name,
                    "ward_id": device.vra.ward_id,
                    "ward_name": device.vra.ward.name,
                    "device_id": device.id,
                    "is_authorized": True,
                })
        except Device.DoesNotExist:
            pass

    token = request.GET.get("token")
    if token:
        vra = VRA.objects.filter(device_token=token, active=True).select_related("ward").first()
        if vra:
            return JsonResponse({
                "bound": True,
                "vra_id": vra.id,
                "vra_name": vra.name,
                "ward_id": vra.ward_id,
                "ward_name": vra.ward.name,
            })

    return JsonResponse({"bound": False})


@require_GET
def resolve_clerk(request):
    fingerprint = request.GET.get("fingerprint")
    if fingerprint:
        try:
            device = Device.objects.select_related("clerk", "clerk__ward").get(
                fingerprint=fingerprint, is_burned=True, is_active=True,
            )
            if device.clerk:
                return JsonResponse({
                    "bound": True,
                    "clerk_id": device.clerk.id,
                    "clerk_name": device.clerk.name,
                    "ward_id": device.clerk.ward_id if device.clerk.ward else None,
                    "ward_name": device.clerk.ward.name if device.clerk.ward else None,
                    "device_id": device.id,
                    "is_authorized": True,
                })
        except Device.DoesNotExist:
            pass

    token = request.GET.get("token")
    if token:
        clerk = Clerk.objects.filter(device_token=token, active=True).select_related("ward").first()
        if clerk:
            return JsonResponse({
                "bound": True,
                "clerk_id": clerk.id,
                "clerk_name": clerk.name,
                "ward_id": clerk.ward_id if clerk.ward else None,
                "ward_name": clerk.ward.name if clerk.ward else None,
            })

    return JsonResponse({"bound": False})


@csrf_exempt
@require_POST
def bind_ward(request):
    """Bind a VRA to a device via constituency + ward selection."""
    token = request.POST.get("token")
    ward_id = request.POST.get("ward_id")
    fingerprint = request.POST.get("fingerprint")
    constituency_id = request.POST.get("constituency_id")

    if not ward_id:
        return JsonResponse({"ok": False, "error": "Ward is required."}, status=400)

    ward = Ward.objects.select_related("constituency").filter(id=ward_id).first()
    if not ward:
        return JsonResponse({"ok": False, "error": "Selected ward was not found."}, status=404)

    if constituency_id:
        if str(ward.constituency_id) != str(constituency_id):
            return JsonResponse({
                "ok": False,
                "error": (
                    f"Ward '{ward.name}' does not belong to the selected "
                    f"constituency. Please pick a matching ward."
                ),
            }, status=400)
    elif not ward.constituency_id:
        return JsonResponse({
            "ok": False,
            "error": (
                "This ward has no constituency assigned. "
                "Please contact your ICT officer."
            ),
        }, status=400)

    if fingerprint:
        try:
            device = Device.objects.get(
                fingerprint=fingerprint, is_burned=True, is_active=True,
            )
        except Device.DoesNotExist:
            if not token:
                return JsonResponse({
                    "ok": False,
                    "error": (
                        "This device is not authorized. "
                        "Please contact your ICT officer."
                    ),
                }, status=403)

        else:
            vra = VRA.objects.filter(ward=ward, active=True).order_by("id").first()
            if not vra:
                return JsonResponse({
                    "ok": False,
                    "error": (
                        f"No active VRA is registered for ward "
                        f"'{ward.name}'. Please contact your ICT officer."
                    ),
                }, status=404)

            if vra.device_token and vra.device_token != fingerprint:
                return JsonResponse({
                    "ok": False,
                    "error": (
                        f"Ward '{ward.name}' is already registered on "
                        f"another device. Please contact your ICT officer."
                    ),
                }, status=409)

            if device.vra_id != vra.id:
                device.vra = vra
                device.save(update_fields=["vra"])

            update_fields = []
            if vra.device_fingerprint != fingerprint:
                vra.device_fingerprint = fingerprint
                update_fields.append("device_fingerprint")
            if not vra.device_token:
                vra.device_token = fingerprint
                update_fields.append("device_token")
            if update_fields:
                vra.save(update_fields=update_fields)

            return JsonResponse({
                "ok": True,
                "vra_id": vra.id,
                "vra_name": vra.name,
                "ward_id": vra.ward_id,
                "ward_name": vra.ward.name,
                "constituency_id": vra.ward.constituency_id,
                "constituency_name": (
                    vra.ward.constituency.name if vra.ward.constituency else None
                ),
                "device_id": device.id,
            })

    if not token:
        return JsonResponse({"ok": False, "error": "No authentication provided."}, status=400)

    vra = VRA.objects.filter(ward=ward, active=True).order_by("id").first()
    if not vra:
        return JsonResponse({
            "ok": False,
            "error": (
                f"No active VRA is registered for ward "
                f"'{ward.name}'. Please contact your ICT officer."
            ),
        }, status=404)

    if vra.device_token and vra.device_token != token:
        return JsonResponse({
            "ok": False,
            "error": (
                f"Ward '{ward.name}' is already registered on "
                f"another device. Please contact your ICT officer."
            ),
        }, status=409)

    if not vra.device_token:
        vra.device_token = token
        vra.save(update_fields=["device_token"])

    return JsonResponse({
        "ok": True,
        "vra_id": vra.id,
        "vra_name": vra.name,
        "ward_id": vra.ward_id,
        "ward_name": vra.ward.name,
        "constituency_id": vra.ward.constituency_id,
        "constituency_name": (
            vra.ward.constituency.name if vra.ward.constituency else None
        ),
    })


@csrf_exempt
@require_POST
def bind_clerk(request):
    """Legacy clerk binding (fingerprint preferred, token fallback)."""
    token = request.POST.get("token")
    ward_id = request.POST.get("ward_id")
    fingerprint = request.POST.get("fingerprint")

    if fingerprint:
        try:
            device = Device.objects.get(fingerprint=fingerprint, is_burned=True, is_active=True)
            clerk = Clerk.objects.filter(ward_id=ward_id, active=True).first()
            if clerk:
                device.clerk = clerk
                device.save(update_fields=["clerk"])
                clerk.device_fingerprint = fingerprint
                update_fields = ["device_fingerprint"]
                if not clerk.device_token:
                    clerk.device_token = fingerprint
                    update_fields.append("device_token")
                clerk.save(update_fields=update_fields)

                return JsonResponse({
                    "ok": True,
                    "clerk_id": clerk.id,
                    "clerk_name": clerk.name,
                    "ward_id": clerk.ward_id,
                    "ward_name": clerk.ward.name,
                    "device_id": device.id,
                })
        except Device.DoesNotExist:
            pass

    if not token:
        return JsonResponse({"ok": False, "error": "No authentication provided"}, status=400)

    clerk = Clerk.objects.filter(ward_id=ward_id, active=True).first()
    if not clerk:
        return JsonResponse({
            "ok": False,
            "error": "No Clerk is registered for this ward. Contact your ICT officer.",
        }, status=404)

    if clerk.device_token and clerk.device_token != token:
        return JsonResponse({
            "ok": False,
            "error": "This ward is already registered on another device. Contact your ICT officer.",
        }, status=409)

    if not clerk.device_token:
        clerk.device_token = token
        clerk.save(update_fields=["device_token"])

    return JsonResponse({
        "ok": True,
        "clerk_id": clerk.id,
        "clerk_name": clerk.name,
        "ward_id": clerk.ward_id,
        "ward_name": clerk.ward.name,
    })


@csrf_exempt
@require_POST
def auto_bind_vra(request):
    try:
        data = json.loads(request.body) if request.body else request.POST.dict()
        vra_id = data.get("vra_id")
        fingerprint = data.get("fingerprint")

        if not fingerprint:
            return JsonResponse({"ok": False, "error": "Fingerprint required"}, status=400)

        device = get_object_or_404(Device, fingerprint=fingerprint, is_burned=True, is_active=True)
        vra = get_object_or_404(VRA, id=vra_id, active=True)

        device.vra = vra
        device.save(update_fields=["vra"])

        vra.device_fingerprint = fingerprint
        if not vra.device_token:
            vra.device_token = fingerprint
        vra.save(update_fields=["device_fingerprint", "device_token"])

        return JsonResponse({
            "ok": True,
            "vra_id": vra.id,
            "vra_name": vra.name,
            "ward_id": vra.ward_id,
            "ward_name": vra.ward.name,
        })
    except Device.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Device not found or not authorized"}, status=404)
    except VRA.DoesNotExist:
        return JsonResponse({"ok": False, "error": "VRA not found"}, status=404)
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


@csrf_exempt
@require_POST
def auto_bind_clerk(request):
    try:
        data = json.loads(request.body) if request.body else request.POST.dict()
        clerk_id = data.get("clerk_id")
        fingerprint = data.get("fingerprint")

        if not fingerprint:
            return JsonResponse({"ok": False, "error": "Fingerprint required"}, status=400)

        device = get_object_or_404(Device, fingerprint=fingerprint, is_burned=True, is_active=True)
        clerk = get_object_or_404(Clerk, id=clerk_id, active=True)

        device.clerk = clerk
        device.save(update_fields=["clerk"])

        clerk.device_fingerprint = fingerprint
        if not clerk.device_token:
            clerk.device_token = fingerprint
        clerk.save(update_fields=["device_fingerprint", "device_token"])

        return JsonResponse({
            "ok": True,
            "clerk_id": clerk.id,
            "clerk_name": clerk.name,
            "ward_id": clerk.ward_id if clerk.ward else None,
            "ward_name": clerk.ward.name if clerk.ward else None,
        })
    except Device.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Device not found or not authorized"}, status=404)
    except Clerk.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Clerk not found"}, status=404)
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


# ═══════════════════════════════════════════════════════════
# Kits with entries
# ═══════════════════════════════════════════════════════════

@require_GET
def kits_with_entries(request):
    """Return kits for the VRA's ward, with registration / venue-mapping flags."""
    fingerprint = request.GET.get("fingerprint")
    date_str = request.GET.get("date")

    if fingerprint:
        try:
            device = Device.objects.select_related("vra", "vra__ward").get(
                fingerprint=fingerprint, is_burned=True, is_active=True,
            )
            vra = device.vra
        except Device.DoesNotExist:
            return JsonResponse({"error": "Device not authorized", "kits": []}, status=401)
    else:
        token = request.GET.get("token")
        if not token:
            return JsonResponse({"error": "No authentication provided", "kits": []}, status=400)
        vra = VRA.objects.filter(device_token=token, active=True).select_related("ward").first()
        if not vra:
            return JsonResponse({"error": "VRA not found or inactive", "kits": []}, status=404)

    if not vra:
        return JsonResponse({"error": "No VRA associated with this device", "kits": []}, status=404)

    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        return JsonResponse({"error": "No active phase found", "kits": []}, status=404)

    try:
        selected_date = (
            datetime.strptime(date_str, "%Y-%m-%d").date()
            if date_str else timezone.localdate()
        )
    except ValueError:
        selected_date = timezone.localdate()

    kits = KIEMSKit.objects.filter(ward=vra.ward, status=True).order_by("kit_name")

    # Merged map of entries keyed by kit_id (VRA's own + clerk-created)
    existing = {
        e.kiems_kit_id: e
        for e in DailyKIEMSEntry.objects.filter(
            kiems_kit__in=kits,
            phase=active_phase,
            entry_date=selected_date,
        )
    }

    data = []
    for kit in kits:
        entry = existing.get(kit.id)
        registered_male = entry.registered_male or 0 if entry else 0
        registered_female = entry.registered_female or 0 if entry else 0
        total = registered_male + registered_female
        has_registration = total > 0
        is_venue_mapping = bool(
            entry and entry.venue and entry.venue.strip() and total == 0
        )

        data.append({
            "kit_id": kit.id,
            "kit_name": kit.kit_name,
            "serial_no": kit.serial_no,
            "venue": entry.venue if entry else "",
            "registered_male": registered_male,
            "registered_female": registered_female,
            "total_registered": total,
            "total_transferred": entry.total_transferred if entry else 0,
            "has_entry": bool(entry),
            "has_registration": has_registration,
            "is_venue_mapping": is_venue_mapping,
            "is_today": selected_date == timezone.localdate(),
            "selected_date": selected_date.strftime("%Y-%m-%d"),
            "is_future": selected_date > timezone.localdate(),
            "entry_type": entry.entry_type if entry else None,
        })

    return JsonResponse({
        "kits": data,
        "ward_name": vra.ward.name,
        "vra_name": vra.name,
        "kit_count": len(data),
        "selected_date": selected_date.strftime("%Y-%m-%d"),
        "is_today": selected_date == timezone.localdate(),
        "is_future": selected_date > timezone.localdate(),
        "device_authorized": True,
    })


# ═══════════════════════════════════════════════════════════
# Clerk mapping helpers
# ═══════════════════════════════════════════════════════════

@require_http_methods(["GET"])
def ward_list(request):
    try:
        ward_data = list(Ward.objects.all().order_by("name").values("id", "name"))
        return JsonResponse({"ok": True, "wards": ward_data, "count": len(ward_data)})
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


@require_http_methods(["GET"])
def kit_list(request):
    ward_id = request.GET.get("ward_id")
    if not ward_id:
        return JsonResponse({"error": "Ward ID is required"}, status=400)

    try:
        ward = Ward.objects.get(id=ward_id)
    except Ward.DoesNotExist:
        return JsonResponse({"error": "Ward not found"}, status=404)

    kits_data = [
        {"id": k.id, "kit_name": k.kit_name, "serial_no": k.serial_no}
        for k in KIEMSKit.objects.filter(ward=ward, status=True).order_by("kit_name")
    ]
    return JsonResponse({
        "ok": True,
        "ward_name": ward.name,
        "kits": kits_data,
        "count": len(kits_data),
    })


@require_http_methods(["GET"])
def clerk_records(request):
    """All DailyKIEMSEntry rows for a kit, across all dates on file."""
    kit_id = request.GET.get("kit_id")
    fingerprint = request.GET.get("fingerprint")

    if not kit_id:
        return JsonResponse({"error": "Kit ID is required"}, status=400)

    try:
        kit = KIEMSKit.objects.get(id=kit_id, status=True)
    except KIEMSKit.DoesNotExist:
        return JsonResponse({"error": "Kit not found"}, status=404)

    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        return JsonResponse({"error": "No active phase found"}, status=404)

    entries = DailyKIEMSEntry.objects.filter(
        kiems_kit=kit, phase=active_phase,
    ).order_by("-entry_date")

    records = []
    for entry in entries:
        total = (entry.registered_male or 0) + (entry.registered_female or 0)
        is_registration = total > 0
        is_venue_mapping = bool(entry.venue and entry.venue.strip() and not is_registration)

        records.append({
            "entry_id": entry.id,
            "date": entry.entry_date.isoformat(),
            "venue": entry.venue or "",
            "editable": True,
            "has_registration": is_registration,
            "is_venue_mapping": is_venue_mapping,
            "entry_type": entry.entry_type,
        })

    today_str = timezone.now().date().isoformat()
    if not any(r["date"] == today_str for r in records):
        records.insert(0, {
            "entry_id": 0,
            "date": today_str,
            "venue": "",
            "editable": True,
            "is_new": True,
            "has_registration": False,
            "is_venue_mapping": False,
            "entry_type": None,
        })

    clerk_data = None
    if fingerprint:
        try:
            device = Device.objects.select_related("clerk", "clerk__ward").get(
                fingerprint=fingerprint, is_burned=True, is_active=True,
            )
            if device.clerk:
                clerk_data = {
                    "id": device.clerk.id,
                    "name": device.clerk.name,
                    "ward_name": device.clerk.ward.name if device.clerk.ward else None,
                }
        except Device.DoesNotExist:
            pass

    return JsonResponse({
        "ok": True,
        "kit_name": kit.kit_name,
        "kit_serial": kit.serial_no,
        "records": records,
        "count": len(records),
        "clerk": clerk_data,
    })


def _resolve_fallback_vra_for_kit(kit, vra, clerk, ward_id):
    """Best-effort VRA resolution when creating a venue-mapping entry."""
    if vra:
        return vra
    if clerk:
        ward_vra = VRA.objects.filter(ward=kit.ward, active=True).first()
        return ward_vra or VRA.objects.filter(active=True).first()
    if ward_id:
        ward = Ward.objects.filter(id=ward_id).first()
        if ward:
            return (
                    VRA.objects.filter(ward=ward, active=True).first()
                    or VRA.objects.filter(active=True).first()
            )
    return VRA.objects.filter(active=True).first()


@csrf_exempt
@require_http_methods(["POST"])
def save_clerk_venues(request):
    """Save venue updates from the clerk tool into DailyKIEMSEntry."""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"error": "Invalid JSON data"}, status=400)

    kit_id = data.get("kit_id")
    updates = data.get("updates", [])
    fallback_date_str = data.get("date")
    ward_id = data.get("ward_id")
    fingerprint = data.get("fingerprint")

    if not updates:
        return JsonResponse({"error": "No updates provided"}, status=400)

    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        return JsonResponse({"error": "No active phase found"}, status=404)

    try:
        fallback_date_obj = (
            datetime.strptime(fallback_date_str, "%Y-%m-%d").date()
            if fallback_date_str else timezone.now().date()
        )
    except ValueError:
        fallback_date_obj = timezone.now().date()

    clerk = None
    vra = None
    if fingerprint:
        try:
            device = Device.objects.select_related(
                "clerk", "clerk__ward", "vra", "vra__ward",
            ).get(fingerprint=fingerprint, is_burned=True, is_active=True)
            clerk = device.clerk
            vra = device.vra
        except Device.DoesNotExist:
            pass

    if not clerk and not vra:
        token = request.GET.get("token") or request.POST.get("token")
        if token:
            clerk = Clerk.objects.filter(device_token=token, active=True).first()
            if not clerk:
                vra = VRA.objects.filter(device_token=token, active=True).first()

    saved_count = 0
    created_entries = []
    updated_entries = []
    created_ids = []
    errors = []

    for update in updates:
        entry_id = update.get("entry_id")
        venue = (update.get("venue") or "").strip()
        kit_id_from_update = update.get("kit_id")
        row_date_str = update.get("date")

        try:
            row_date_obj = (
                datetime.strptime(row_date_str, "%Y-%m-%d").date()
                if row_date_str else fallback_date_obj
            )
        except ValueError:
            row_date_obj = fallback_date_obj

        if entry_id is None:
            errors.append("Missing entry_id")
            continue

        try:
            entry_id = int(entry_id)
        except (ValueError, TypeError):
            errors.append(f"Invalid entry_id: {entry_id}")
            continue

        if not venue:
            errors.append(f"Entry {entry_id}: venue is required")
            continue

        actual_kit_id = kit_id_from_update or kit_id
        is_new = (
                entry_id <= 0
                or kit_id_from_update is not None
                or not DailyKIEMSEntry.objects.filter(id=entry_id).exists()
        )

        if is_new:
            if not actual_kit_id:
                actual_kit_id = entry_id if entry_id > 0 else None
            if not actual_kit_id:
                errors.append("Missing kit_id for new entry")
                continue

            try:
                kit = KIEMSKit.objects.get(id=actual_kit_id, status=True)
            except KIEMSKit.DoesNotExist:
                errors.append(f"Kit {actual_kit_id} not found")
                continue

            existing_entry = DailyKIEMSEntry.objects.filter(
                kiems_kit=kit, phase=active_phase, entry_date=row_date_obj,
            ).first()

            if existing_entry:
                existing_entry.venue = venue
                existing_entry.entry_type = (
                    "REGISTRATION" if existing_entry.total_registered > 0 else "VENUE"
                )
                existing_entry.save(update_fields=["venue", "entry_type", "updated_at"])
                updated_entries.append(existing_entry)
                saved_count += 1
                continue

            resolved_vra = _resolve_fallback_vra_for_kit(kit, vra, clerk, ward_id)
            if not resolved_vra:
                errors.append("No VRA available")
                continue

            try:
                entry = DailyKIEMSEntry.objects.create(
                    kiems_kit=kit,
                    phase=active_phase,
                    ward=kit.ward,
                    entry_date=row_date_obj,
                    venue=venue,
                    registered_male=0,
                    registered_female=0,
                    entry_type="VENUE",
                    vra=resolved_vra,
                    clerk=clerk if clerk else None,
                )
            except Exception as e:
                # Unique constraint race — fall back to update
                existing_entry = DailyKIEMSEntry.objects.filter(
                    kiems_kit=kit, phase=active_phase, entry_date=row_date_obj,
                ).first()
                if existing_entry:
                    existing_entry.venue = venue
                    existing_entry.entry_type = (
                        "REGISTRATION" if existing_entry.total_registered > 0 else "VENUE"
                    )
                    existing_entry.save(update_fields=["venue", "entry_type", "updated_at"])
                    updated_entries.append(existing_entry)
                    saved_count += 1
                    continue
                errors.append(f"Could not create entry for kit {kit.id}: {e}")
                continue

            created_entries.append(entry)
            created_ids.append(entry.id)
            saved_count += 1

        else:  # existing entry, update by id
            try:
                entry = DailyKIEMSEntry.objects.get(id=entry_id)
                entry.venue = venue
                entry.entry_type = (
                    "REGISTRATION" if entry.total_registered > 0 else "VENUE"
                )
                if row_date_str and row_date_obj != entry.entry_date:
                    entry.entry_date = row_date_obj
                    entry.save(update_fields=[
                        "venue", "entry_date", "entry_type", "updated_at",
                    ])
                else:
                    entry.save(update_fields=["venue", "entry_type", "updated_at"])
                updated_entries.append(entry)
                saved_count += 1
            except DailyKIEMSEntry.DoesNotExist:
                errors.append(f"Entry {entry_id} not found")
            except Exception as e:
                errors.append(f"Entry {entry_id}: {e}")

    # Queue WhatsApp confirmations for genuine registration entries only
    registration_entries = [
        e for e in updated_entries + created_entries
        if e.total_registered > 0
    ]
    _enqueue_whatsapp_for_entries(registration_entries, kind="clerk.mapping")
    if registration_entries:
        drain_async(limit=15)

    return JsonResponse({
        "ok": True,
        "saved": saved_count,
        "created": len(created_entries),
        "updated": len(updated_entries),
        "registrations": len(registration_entries),
        "venue_mappings": len([
            e for e in created_entries + updated_entries if e.total_registered == 0
        ]),
        "entry_ids": created_ids,
        "errors": errors if errors else None,
        "message": f"Saved {saved_count} entr{'y' if saved_count == 1 else 'ies'}.",
    })


# ═══════════════════════════════════════════════════════════
# Movement schedule (client)
# ═══════════════════════════════════════════════════════════

@require_GET
def movement_kits(request):
    """Kits + existing schedules for a ward/date."""
    ward_id = request.GET.get("ward_id")
    date_str = request.GET.get("date")

    if not ward_id:
        return JsonResponse({"ok": False, "error": "ward_id required"}, status=400)

    try:
        ward = Ward.objects.select_related("constituency").get(id=ward_id, active=True)
    except Ward.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Ward not found"}, status=404)

    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        return JsonResponse({"ok": False, "error": "No active phase"}, status=404)

    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)

    if date_str:
        try:
            schedule_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            schedule_date = tomorrow
    else:
        schedule_date = tomorrow

    kits = KIEMSKit.objects.filter(ward=ward, status=True).order_by("kit_name")
    existing = {
        m.kiems_kit_id: m
        for m in MovementSchedule.objects.filter(
            kiems_kit__in=kits,
            phase=active_phase,
            schedule_date=schedule_date,
        )
    }

    data = [{
        "kit_id": k.id,
        "kit_name": k.kit_name,
        "serial_no": k.serial_no,
        "venue": existing[k.id].venue if k.id in existing else "",
        "has_schedule": k.id in existing,
        "notes": existing[k.id].notes if k.id in existing else "",
    } for k in kits]

    return JsonResponse({
        "ok": True,
        "ward_id": ward.id,
        "ward_name": ward.name,
        "constituency_id": ward.constituency_id,
        "constituency_name": ward.constituency.name if ward.constituency else None,
        "schedule_date": schedule_date.isoformat(),
        "is_past": schedule_date < today,
        "is_today": schedule_date == today,
        "is_future": schedule_date > today,
        "kits": data,
    })


@require_GET
def movement_summary(request):
    """Per-day filled-venue counts for a ward in a given month."""
    ward_id = request.GET.get("ward_id")
    month_str = request.GET.get("month")

    if not ward_id or not month_str:
        return JsonResponse({"ok": False, "error": "ward_id and month required"}, status=400)

    try:
        ward = Ward.objects.get(id=ward_id, active=True)
    except Ward.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Ward not found"}, status=404)

    try:
        year, mon = map(int, month_str.split("-"))
        month_start = date(year, mon, 1)
    except (ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "Invalid month"}, status=400)

    if mon == 12:
        month_end = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        month_end = date(year, mon + 1, 1) - timedelta(days=1)

    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        return JsonResponse({"ok": False, "error": "No active phase"}, status=404)

    total_kits = KIEMSKit.objects.filter(ward=ward, status=True).count()

    rows = (
        MovementSchedule.objects
        .filter(
            ward=ward,
            phase=active_phase,
            schedule_date__gte=month_start,
            schedule_date__lte=month_end,
        )
        .exclude(venue="")
        .values("schedule_date")
        .annotate(filled=Count("kiems_kit", distinct=True))
    )

    days = {
        row["schedule_date"].isoformat(): {"filled": row["filled"], "total": total_kits}
        for row in rows
    }
    return JsonResponse({"ok": True, "days": days})


@csrf_exempt
@require_http_methods(["POST"])
def save_clerk_movement_venues(request):
    """Save venue pre-mappings from the clerk tool into MovementSchedule."""
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON data"}, status=400)

    updates = data.get("updates", [])
    fallback_date_str = data.get("date")
    ward_id = data.get("ward_id")
    fingerprint = data.get("fingerprint")

    if not updates:
        return JsonResponse({"ok": False, "error": "No updates provided"}, status=400)

    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        return JsonResponse({"ok": False, "error": "No active phase found"}, status=404)

    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)

    try:
        fallback_date_obj = (
            datetime.strptime(fallback_date_str, "%Y-%m-%d").date()
            if fallback_date_str else tomorrow
        )
    except ValueError:
        fallback_date_obj = tomorrow

    clerk = None
    vra = None
    if fingerprint:
        try:
            device = Device.objects.select_related(
                "clerk", "clerk__ward", "vra", "vra__ward",
            ).get(fingerprint=fingerprint, is_burned=True, is_active=True)
            clerk = device.clerk
            vra = device.vra
        except Device.DoesNotExist:
            pass

    if not clerk and not vra:
        token = request.GET.get("token") or request.POST.get("token")
        if token:
            clerk = Clerk.objects.filter(device_token=token, active=True).first()
            if not clerk:
                vra = VRA.objects.filter(device_token=token, active=True).first()

    saved_count = 0
    created_count = 0
    updated_count = 0
    unchanged_count = 0
    errors = []
    saved_schedules = []

    for update in updates:
        venue = (update.get("venue") or "").strip()
        kit_id = update.get("kit_id")
        row_date_str = update.get("date")

        try:
            row_date_obj = (
                datetime.strptime(row_date_str, "%Y-%m-%d").date()
                if row_date_str else fallback_date_obj
            )
        except ValueError:
            row_date_obj = fallback_date_obj

        if row_date_obj < today:
            errors.append(f"Cannot schedule movement for past date {row_date_obj}.")
            continue
        if not kit_id:
            errors.append("Missing kit_id.")
            continue
        if not venue:
            errors.append(f"Kit {kit_id}: venue is required.")
            continue

        try:
            kit = KIEMSKit.objects.select_related("ward", "ward__constituency").get(
                id=kit_id, status=True,
            )
        except KIEMSKit.DoesNotExist:
            errors.append(f"Kit {kit_id} not found.")
            continue

        if ward_id and str(kit.ward_id) != str(ward_id):
            errors.append(f"Kit {kit_id} does not belong to ward {ward_id}.")
            continue

        obj, was_created = MovementSchedule.objects.get_or_create(
            kiems_kit=kit,
            schedule_date=row_date_obj,
            defaults={
                "ward": kit.ward,
                "constituency": kit.ward.constituency,
                "phase": active_phase,
                "vra": vra,
                "clerk": clerk,
                "venue": venue,
                "notes": "",
            },
        )

        if was_created:
            created_count += 1
            saved_count += 1
            saved_schedules.append(obj)
            continue

        if obj.venue == venue:
            unchanged_count += 1
            continue

        obj.venue = venue
        obj.edit_count += 1
        if clerk and not obj.clerk:
            obj.clerk = clerk
        if vra and not obj.vra:
            obj.vra = vra
        obj.save(update_fields=["venue", "edit_count", "clerk", "vra", "updated_at"])
        updated_count += 1
        saved_count += 1
        saved_schedules.append(obj)

    # Queue per-kit movement WhatsApp confirmations (only for tomorrow)
    if fallback_date_obj == tomorrow and saved_schedules:
        from home.services.movement_schedule import format_single_movement_message
        for row in saved_schedules:
            if not row.ward or not row.ward.constituency_id:
                continue
            msg = format_single_movement_message(row, is_update=False)
            if msg:
                enqueue_whatsapp(
                    constituency=row.ward.constituency,
                    message=msg,
                    kind="movement.save",
                )
        drain_async(limit=15)

    # Recompute report state (does not send grand report)
    if created_count or updated_count:
        try:
            from home.services.movement_schedule import reevaluate_movement_schedule
            ward_obj = Ward.objects.filter(id=ward_id).first() if ward_id else None
            if ward_obj:
                reevaluate_movement_schedule(ward_obj.constituency, fallback_date_obj)
        except Exception as e:
            print(f"[clerk-movement] State re-evaluation error: {e}")

    return JsonResponse({
        "ok": True,
        "saved": saved_count,
        "created": created_count,
        "updated": updated_count,
        "unchanged": unchanged_count,
        "errors": errors if errors else None,
        "message": f"Saved {saved_count} venue(s).",
    })


@require_http_methods(["GET"])
def clerk_movement_records(request):
    """Movement schedules for a kit, today onwards, plus tomorrow placeholder."""
    kit_id = request.GET.get("kit_id")
    fingerprint = request.GET.get("fingerprint")

    if not kit_id:
        return JsonResponse({"error": "Kit ID is required"}, status=400)

    try:
        kit = KIEMSKit.objects.get(id=kit_id, status=True)
    except KIEMSKit.DoesNotExist:
        return JsonResponse({"error": "Kit not found"}, status=404)

    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        return JsonResponse({"error": "No active phase found"}, status=404)

    today = timezone.localdate()

    schedules = MovementSchedule.objects.filter(
        kiems_kit=kit, phase=active_phase, schedule_date__gte=today,
    ).order_by("schedule_date")

    records = [{
        "entry_id": s.id,
        "date": s.schedule_date.isoformat(),
        "venue": s.venue or "",
        "editable": True,
        "entry_type": "MOVEMENT",
    } for s in schedules]

    tomorrow = today + timedelta(days=1)
    if not any(r["date"] == tomorrow.isoformat() for r in records):
        records.insert(0, {
            "entry_id": 0,
            "date": tomorrow.isoformat(),
            "venue": "",
            "editable": True,
            "is_new": True,
            "entry_type": "MOVEMENT",
        })

    clerk_data = None
    if fingerprint:
        try:
            device = Device.objects.select_related("clerk", "clerk__ward").get(
                fingerprint=fingerprint, is_burned=True, is_active=True,
            )
            if device.clerk:
                clerk_data = {
                    "id": device.clerk.id,
                    "name": device.clerk.name,
                    "ward_name": device.clerk.ward.name if device.clerk.ward else None,
                }
        except Device.DoesNotExist:
            pass

    return JsonResponse({
        "ok": True,
        "kit_name": kit.kit_name,
        "kit_serial": kit.serial_no,
        "records": records,
        "count": len(records),
        "clerk": clerk_data,
    })
