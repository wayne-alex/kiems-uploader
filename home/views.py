import json
from datetime import datetime
from datetime import timedelta

from django.conf import settings
from django.db.models import Sum, Q
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST

from home.models import MovementSchedule
from home.services.movement_schedule import (
    format_single_movement_message, )
from .models import (
    Ward, VRA, Clerk, KIEMSKit, Phase, DailyKIEMSEntry,
    Device, WhatsAppSetting, Constituency
)
from .services.whatsapp import send_to_constituency, get_group_for_constituency


# Set to False if unbound devices must be allowed to pick any ward.
MOVEMENT_REQUIRE_BOUND_DEVICE = getattr(settings, "MOVEMENT_REQUIRE_BOUND_DEVICE", True)
# ==================== WHATSAPP HELPER FUNCTIONS ====================


def get_whatsapp_group_for_vra(vra):
    """Kept for backward compatibility — resolves via the VRA's ward constituency."""
    if not vra or not vra.ward or not vra.ward.constituency_id:
        return None
    group = get_group_for_constituency(vra.ward.constituency)
    return group.group_id if group else None


def send_whatsapp_message_from_vra(message, vra):
    """Route a VRA message to their constituency's group. Never raises."""
    if not vra or not vra.ward or not vra.ward.constituency_id:
        print(f"[whatsapp] VRA '{vra}' has no constituency — message dropped")
        return False

    ok, err, group_id = send_to_constituency(vra.ward.constituency, message)
    if not ok:
        print(
            f"[whatsapp] Send failed for {vra.ward.constituency.name} "
            f"(group: {group_id}): {err}"
        )
    return ok


def get_whatsapp_settings():
    """
    Fetch the (single, admin-level) WhatsApp notification settings.
    This is client-facing code with no logged-in admin user, so we look
    up whichever superuser's settings have been configured in the panel.
    """
    try:
        from django.contrib.auth.models import User
        admin_user = User.objects.filter(is_superuser=True).first()
        if not admin_user:
            return None
        setting, _ = WhatsAppSetting.objects.get_or_create(
            user=admin_user,
            defaults={
                'notify_vra': True,
                'notify_edit': True,
                'notify_daily': True,
                'notify_grand_total': True,
            }
        )
        return setting
    except Exception as e:
        print(f"WhatsApp settings error: {str(e)}")
        return None


def format_vra_submission_message(entry, is_update=False):
    """Format VRA submission message - only for REGISTRATION entries"""
    total = entry.registered_male + entry.registered_female

    # Only send if there are actual registrations
    if total == 0:
        return None

    if is_update:
        message = f"{entry.ward.name.upper()} UPDATED\n"
    else:
        message = f"{entry.ward.name.upper()} CONFIRMED ✅\n"

    message += f"{entry.kiems_kit.kit_name}:Male:{entry.registered_male} Female:{entry.registered_female} ={total}"

    if entry.total_transferred and entry.total_transferred > 0:
        message += f"\nTransferred: {entry.total_transferred}"

    return message


def format_grand_total_message(entries, total_wards):
    """Format grand total message - only for REGISTRATION entries"""
    today = timezone.now().date()

    total_male = entries.aggregate(Sum('registered_male'))['registered_male__sum'] or 0
    total_female = entries.aggregate(Sum('registered_female'))['registered_female__sum'] or 0
    total_registered = entries.aggregate(Sum('total_registered'))['total_registered__sum'] or 0
    total_transferred = entries.aggregate(Sum('total_transferred'))['total_transferred__sum'] or 0

    ward_data = entries.values('ward__name').annotate(
        male=Sum('registered_male'),
        female=Sum('registered_female'),
        total=Sum('total_registered')
    ).order_by('ward__name')

    message = f"DAILY REPORT - {today.strftime('%d %b %Y')}\n"
    message += f"All {total_wards} Wards Submitted\n\n"

    for w in ward_data:
        message += f"{w['ward__name']}: Male: {w['male']} Female: {w['female']} = {w['total']}\n"

    message += f"\nTOTAL: Male: {total_male} Female: {total_female} = {total_registered}"
    if total_transferred > 0:
        message += f" | Transferred: {total_transferred}"

    return message


# ==================== DEVICE AUTHENTICATION HELPERS ====================

def get_vra_from_request(request):
    """Get VRA from request using fingerprint or token"""
    fingerprint = request.GET.get('fingerprint') or request.POST.get('fingerprint')

    if fingerprint:
        try:
            device = Device.objects.select_related('vra', 'vra__ward').get(
                fingerprint=fingerprint,
                is_burned=True,
                is_active=True
            )
            if device.vra:
                return device.vra
        except Device.DoesNotExist:
            pass

    # Fallback to token
    token = request.GET.get('token') or request.POST.get('token')
    if token:
        return VRA.objects.filter(device_token=token, active=True).select_related('ward').first()

    return None


def get_clerk_from_request(request):
    """Get Clerk from request using fingerprint or token"""
    fingerprint = request.GET.get('fingerprint') or request.POST.get('fingerprint')

    if fingerprint:
        try:
            device = Device.objects.select_related('clerk', 'clerk__ward').get(
                fingerprint=fingerprint,
                is_burned=True,
                is_active=True
            )
            if device.clerk:
                return device.clerk
        except Device.DoesNotExist:
            pass

    # Fallback to token
    token = request.GET.get('token') or request.POST.get('token')
    if token:
        return Clerk.objects.filter(device_token=token, active=True).select_related('ward').first()

    return None


def get_device_from_fingerprint(fingerprint):
    """Get device from fingerprint"""
    if not fingerprint:
        return None
    try:
        return Device.objects.select_related('vra', 'vra__ward', 'clerk', 'clerk__ward').get(
            fingerprint=fingerprint,
            is_burned=True,
            is_active=True
        )
    except Device.DoesNotExist:
        return None


# ==================== CLIENT-SIDE VIEWS ====================

def kiems_entry_view(request):
    """Main entry view for VRA."""
    constituencies = Constituency.objects.filter(active=True).order_by("name")

    # If the VRA is already bound (fingerprint / token), lock them to
    # their ward's constituency and skip the dropdown entirely.
    bound_constituency = None
    vra = get_vra_from_request(request)
    if vra and vra.ward and getattr(vra.ward, "constituency_id", None):
        bound_constituency = vra.ward.constituency

    wards = Ward.objects.select_related("constituency").order_by(
        "constituency__name", "name"
    )

    active_phase = Phase.objects.filter(active=True).first()

    return render(request, "home.html", {
        "constituencies": constituencies,
        "wards": wards,
        "bound_constituency": bound_constituency,
        "active_phase": active_phase,
    })


@require_GET
def wards_by_constituency(request):
    """Return active wards for a given constituency."""
    constituency_id = request.GET.get("constituency_id")
    if not constituency_id:
        return JsonResponse({"ok": False, "wards": []}, status=400)

    wards = (
        Ward.objects
        .filter(constituency_id=constituency_id)
        .order_by("name")
        .values("id", "name", "code")
    )
    return JsonResponse({"ok": True, "wards": list(wards)})


@csrf_exempt
@require_POST
def register_device(request):
    """Register a device with its fingerprint - auto-authorize by default"""
    try:
        data = json.loads(request.body)
        fingerprint = data.get('fingerprint')
        device_info = data.get('device_info', {})

        if not fingerprint:
            return JsonResponse({
                'ok': False,
                'error': 'Device fingerprint required'
            }, status=400)

        # Check if device exists
        device, created = Device.objects.get_or_create(
            fingerprint=fingerprint,
            defaults={
                'user_agent': request.META.get('HTTP_USER_AGENT', ''),
                'ip_address': request.META.get('REMOTE_ADDR'),
                'screen_resolution': device_info.get('screenResolution', ''),
                'language': device_info.get('language', ''),
                'platform': device_info.get('platform', ''),
                'timezone': device_info.get('timezone', ''),
                'is_burned': True,  # AUTO-AUTHORIZE by default
                'is_active': True,
            }
        )

        # Update device info
        if not created:
            device.user_agent = request.META.get('HTTP_USER_AGENT', device.user_agent)
            device.ip_address = request.META.get('REMOTE_ADDR', device.ip_address)
            device.screen_resolution = device_info.get('screenResolution', device.screen_resolution)
            device.language = device_info.get('language', device.language)
            device.platform = device_info.get('platform', device.platform)
            device.timezone = device_info.get('timezone', device.timezone)
            device.save(update_fields=['user_agent', 'ip_address', 'screen_resolution',
                                       'language', 'platform', 'timezone'])

        # Auto-bind to VRA if fingerprint matches a VRA's device_token or device_fingerprint
        vra = VRA.objects.filter(
            Q(device_token=fingerprint) | Q(device_fingerprint=fingerprint),
            active=True
        ).first()

        if vra and not device.vra:
            device.vra = vra
            device.save(update_fields=['vra'])
            if not vra.device_fingerprint:
                vra.device_fingerprint = fingerprint
                vra.save(update_fields=['device_fingerprint'])

        # Auto-bind to Clerk if fingerprint matches a Clerk's device_token or device_fingerprint
        clerk = Clerk.objects.filter(
            Q(device_token=fingerprint) | Q(device_fingerprint=fingerprint),
            active=True
        ).first()

        if clerk and not device.clerk:
            device.clerk = clerk
            device.save(update_fields=['clerk'])
            if not clerk.device_fingerprint:
                clerk.device_fingerprint = fingerprint
                clerk.save(update_fields=['device_fingerprint'])

        return JsonResponse({
            'ok': True,
            'device_id': device.id,
            'is_burned': device.is_burned,
            'is_active': device.is_active,
            'vra_id': device.vra_id,
            'vra_name': device.vra.name if device.vra else None,
            'ward_id': device.vra.ward_id if device.vra else None,
            'ward_name': device.vra.ward.name if device.vra else None,
            'clerk_id': device.clerk_id if device.clerk else None,
            'clerk_name': device.clerk.name if device.clerk else None,
            'created': created,
            'auto_authorized': True
        })

    except Exception as e:
        return JsonResponse({
            'ok': False,
            'error': str(e)
        }, status=500)


@require_GET
def check_device_status(request):
    """Check if a device is authorized"""
    fingerprint = request.GET.get('fingerprint')

    if not fingerprint:
        return JsonResponse({
            'ok': False,
            'error': 'Fingerprint required'
        }, status=400)

    try:
        device = Device.objects.select_related('vra', 'vra__ward', 'clerk', 'clerk__ward').get(fingerprint=fingerprint)

        return JsonResponse({
            'ok': True,
            'is_authorized': device.is_burned,
            'is_active': device.is_active,
            'vra_id': device.vra_id,
            'vra_name': device.vra.name if device.vra else None,
            'ward_id': device.vra.ward_id if device.vra else None,
            'ward_name': device.vra.ward.name if device.vra else None,
            'clerk_id': device.clerk_id if device.clerk else None,
            'clerk_name': device.clerk.name if device.clerk else None,
            'authorized_date': device.burn_date.isoformat() if device.burn_date else None,
            'device_id': device.id,
        })

    except Device.DoesNotExist:
        return JsonResponse({
            'ok': True,
            'is_authorized': False,
            'is_active': False,
            'vra_id': None,
            'vra_name': None,
            'ward_id': None,
            'ward_name': None,
            'clerk_id': None,
            'clerk_name': None,
        })


@require_GET
def resolve_vra(request):
    """Resolve VRA using fingerprint (modern method)"""
    fingerprint = request.GET.get('fingerprint')

    if fingerprint:
        try:
            device = Device.objects.select_related('vra', 'vra__ward').get(
                fingerprint=fingerprint,
                is_burned=True,
                is_active=True
            )
            if device.vra:
                return JsonResponse({
                    "bound": True,
                    "vra_id": device.vra.id,
                    "vra_name": device.vra.name,
                    "ward_id": device.vra.ward_id,
                    "ward_name": device.vra.ward.name,
                    "device_id": device.id,
                    "is_authorized": True
                })
        except Device.DoesNotExist:
            pass

    # Fallback to token-based method (legacy support)
    token = request.GET.get("token")
    if token:
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


@require_GET
def resolve_clerk(request):
    """Resolve Clerk using fingerprint (modern method)"""
    fingerprint = request.GET.get('fingerprint')

    if fingerprint:
        try:
            device = Device.objects.select_related('clerk', 'clerk__ward').get(
                fingerprint=fingerprint,
                is_burned=True,
                is_active=True
            )
            if device.clerk:
                return JsonResponse({
                    "bound": True,
                    "clerk_id": device.clerk.id,
                    "clerk_name": device.clerk.name,
                    "ward_id": device.clerk.ward_id if device.clerk.ward else None,
                    "ward_name": device.clerk.ward.name if device.clerk.ward else None,
                    "device_id": device.id,
                    "is_authorized": True
                })
        except Device.DoesNotExist:
            pass

    # Fallback to token-based method (legacy support)
    token = request.GET.get("token")
    if token:
        clerk = Clerk.objects.filter(device_token=token, active=True).select_related("ward").first()
        if clerk:
            return JsonResponse({
                "bound": True,
                "clerk_id": clerk.id,
                "clerk_name": clerk.name,
                "ward_id": clerk.ward_id if clerk.ward else None,
                "ward_name": clerk.ward.name if clerk.ward else None
            })

    return JsonResponse({"bound": False})


@csrf_exempt
@require_POST
def bind_ward(request):
    """
    Bind a VRA to a device using constituency + ward selection.

    Priority order:
      1. fingerprint → look up Device, verify authorized, bind to a VRA in the ward
      2. token       → legacy token-based path

    Validation:
      - ward must exist
      - if constituency_id was sent, ward.constituency must match it
      - if the VRA already has a different device_token, reject (ward is
        already bound to another device)
    """
    token = request.POST.get("token")
    ward_id = request.POST.get("ward_id")
    fingerprint = request.POST.get("fingerprint")
    constituency_id = request.POST.get("constituency_id")

    # ------------------------------------------------------------------
    # 1. Ward must exist
    # ------------------------------------------------------------------
    if not ward_id:
        return JsonResponse(
            {"ok": False, "error": "Ward is required."},
            status=400,
        )

    ward = Ward.objects.select_related("constituency").filter(id=ward_id).first()
    if not ward:
        return JsonResponse(
            {"ok": False, "error": "Selected ward was not found."},
            status=404,
        )

    # ------------------------------------------------------------------
    # 2. Constituency ↔ Ward consistency
    # ------------------------------------------------------------------
    if constituency_id:
        # Ward belongs to a different constituency than the one selected
        if str(ward.constituency_id) != str(constituency_id):
            return JsonResponse(
                {
                    "ok": False,
                    "error": (
                        f"Ward '{ward.name}' does not belong to the "
                        f"selected constituency. Please pick a matching ward."
                    ),
                },
                status=400,
            )
    # If the client didn't send a constituency, require the ward to have one
    elif not ward.constituency_id:
        return JsonResponse(
            {
                "ok": False,
                "error": (
                    "This ward has no constituency assigned. "
                    "Please contact your ICT officer."
                ),
            },
            status=400,
        )

    # ------------------------------------------------------------------
    # 3. Fingerprint path (modern)
    # ------------------------------------------------------------------
    if fingerprint:
        try:
            device = Device.objects.get(
                fingerprint=fingerprint,
                is_burned=True,  # burned-in = authorized
                is_active=True,
            )
        except Device.DoesNotExist:
            # Device isn't registered or is unauthorized - fall through to
            # token path only if a token was provided, otherwise error out.
            if not token:
                return JsonResponse(
                    {
                        "ok": False,
                        "error": (
                            "This device is not authorized. "
                            "Please contact your ICT officer."
                        ),
                    },
                    status=403,
                )

        else:
            # Device is authorized: find a VRA for the selected ward
            vra = VRA.objects.filter(
                ward=ward,
                active=True,
            ).order_by("id").first()

            if not vra:
                return JsonResponse(
                    {
                        "ok": False,
                        "error": (
                            f"No active VRA is registered for ward "
                            f"'{ward.name}'. Please contact your ICT officer."
                        ),
                    },
                    status=404,
                )

            # Ward-level exclusivity: one VRA = one device
            if vra.device_token and vra.device_token != fingerprint:
                return JsonResponse(
                    {
                        "ok": False,
                        "error": (
                            f"Ward '{ward.name}' is already registered on "
                            f"another device. Please contact your ICT officer."
                        ),
                    },
                    status=409,
                )

            # Link Device ↔ VRA in both directions
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

    # ------------------------------------------------------------------
    # 4. Token path (legacy fallback)
    # ------------------------------------------------------------------
    if not token:
        return JsonResponse(
            {
                "ok": False,
                "error": "No authentication provided.",
            },
            status=400,
        )

    vra = VRA.objects.filter(
        ward=ward,
        active=True,
    ).order_by("id").first()

    if not vra:
        return JsonResponse(
            {
                "ok": False,
                "error": (
                    f"No active VRA is registered for ward "
                    f"'{ward.name}'. Please contact your ICT officer."
                ),
            },
            status=404,
        )

    # Ward-level exclusivity for token path too
    if vra.device_token and vra.device_token != token:
        return JsonResponse(
            {
                "ok": False,
                "error": (
                    f"Ward '{ward.name}' is already registered on "
                    f"another device. Please contact your ICT officer."
                ),
            },
            status=409,
        )

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
    """Bind a Clerk to a device using ward selection (legacy method)"""
    token = request.POST.get("token")
    ward_id = request.POST.get("ward_id")
    fingerprint = request.POST.get("fingerprint")

    # Try fingerprint first
    if fingerprint:
        try:
            device = Device.objects.get(fingerprint=fingerprint, is_burned=True, is_active=True)
            clerk = Clerk.objects.filter(ward_id=ward_id, active=True).first()
            if clerk:
                device.clerk = clerk
                device.save(update_fields=['clerk'])
                clerk.device_fingerprint = fingerprint
                clerk.save(update_fields=['device_fingerprint'])

                # Also set the device token for legacy support
                if not clerk.device_token:
                    clerk.device_token = fingerprint
                    clerk.save(update_fields=['device_token'])

                return JsonResponse({
                    "ok": True,
                    "clerk_id": clerk.id,
                    "clerk_name": clerk.name,
                    "ward_id": clerk.ward_id,
                    "ward_name": clerk.ward.name,
                    "device_id": device.id
                })
        except Device.DoesNotExist:
            pass

    # Fallback to token-based (legacy)
    if not token:
        return JsonResponse({
            "ok": False,
            "error": "No authentication provided"
        }, status=400)

    clerk = Clerk.objects.filter(ward_id=ward_id, active=True).first()
    if not clerk:
        return JsonResponse({
            "ok": False,
            "error": "No Clerk is registered for this ward. Contact your ICT officer."
        }, status=404)

    if clerk.device_token and clerk.device_token != token:
        return JsonResponse({
            "ok": False,
            "error": "This ward is already registered on another device. Contact your ICT officer."
        }, status=409)

    if not clerk.device_token:
        clerk.device_token = token
        clerk.save(update_fields=["device_token"])

    return JsonResponse({
        "ok": True,
        "clerk_id": clerk.id,
        "clerk_name": clerk.name,
        "ward_id": clerk.ward_id,
        "ward_name": clerk.ward.name
    })


@csrf_exempt
@require_POST
def auto_bind_vra(request):
    """Auto-bind a VRA to a device using fingerprint"""
    try:
        data = json.loads(request.body) if request.body else request.POST.dict()
        vra_id = data.get('vra_id')
        fingerprint = data.get('fingerprint')

        if not fingerprint:
            return JsonResponse({"ok": False, "error": "Fingerprint required"}, status=400)

        device = get_object_or_404(Device, fingerprint=fingerprint, is_burned=True, is_active=True)
        vra = get_object_or_404(VRA, id=vra_id, active=True)

        # Update device
        device.vra = vra
        device.save(update_fields=['vra'])

        # Update VRA
        vra.device_fingerprint = fingerprint
        if not vra.device_token:
            vra.device_token = fingerprint
        vra.save(update_fields=['device_fingerprint', 'device_token'])

        return JsonResponse({
            "ok": True,
            "vra_id": vra.id,
            "vra_name": vra.name,
            "ward_id": vra.ward_id,
            "ward_name": vra.ward.name
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
    """Auto-bind a Clerk to a device using fingerprint"""
    try:
        data = json.loads(request.body) if request.body else request.POST.dict()
        clerk_id = data.get('clerk_id')
        fingerprint = data.get('fingerprint')

        if not fingerprint:
            return JsonResponse({"ok": False, "error": "Fingerprint required"}, status=400)

        device = get_object_or_404(Device, fingerprint=fingerprint, is_burned=True, is_active=True)
        clerk = get_object_or_404(Clerk, id=clerk_id, active=True)

        # Update device
        device.clerk = clerk
        device.save(update_fields=['clerk'])

        # Update Clerk
        clerk.device_fingerprint = fingerprint
        if not clerk.device_token:
            clerk.device_token = fingerprint
        clerk.save(update_fields=['device_fingerprint', 'device_token'])

        return JsonResponse({
            "ok": True,
            "clerk_id": clerk.id,
            "clerk_name": clerk.name,
            "ward_id": clerk.ward_id if clerk.ward else None,
            "ward_name": clerk.ward.name if clerk.ward else None
        })

    except Device.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Device not found or not authorized"}, status=404)
    except Clerk.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Clerk not found"}, status=404)
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=500)


@require_GET
def kits_with_entries(request):
    """Get kits with entries - distinguishes between venue mappings and registrations"""
    fingerprint = request.GET.get('fingerprint')
    date_str = request.GET.get('date')

    # Try to find VRA by fingerprint
    if fingerprint:
        try:
            device = Device.objects.select_related('vra', 'vra__ward').get(
                fingerprint=fingerprint,
                is_burned=True,
                is_active=True
            )
            vra = device.vra
        except Device.DoesNotExist:
            return JsonResponse({"error": "Device not authorized", "kits": []}, status=401)
    else:
        # Fallback to token-based
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

    # Parse date or use today
    try:
        if date_str:
            selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        else:
            selected_date = timezone.localdate()
    except ValueError:
        selected_date = timezone.localdate()

    # Get all kits for this ward
    kits = KIEMSKit.objects.filter(ward=vra.ward, status=True).order_by('kit_name')

    # Get existing entries for the selected date (including future dates)
    existing = {
        e.kiems_kit_id: e for e in DailyKIEMSEntry.objects.filter(
            kiems_kit__in=kits,
            phase=active_phase,
            entry_date=selected_date,
            vra=vra
        )
    }

    # Also check for entries created by clerks for this ward on this date
    clerk_entries = DailyKIEMSEntry.objects.filter(
        kiems_kit__in=kits,
        phase=active_phase,
        entry_date=selected_date,
        ward=vra.ward
    ).exclude(vra=vra)

    # Merge clerk entries into existing
    for entry in clerk_entries:
        if entry.kiems_kit_id not in existing:
            existing[entry.kiems_kit_id] = entry

    data = []
    for kit in kits:
        entry = existing.get(kit.id)

        # Determine if this is a registration or venue mapping
        has_registration = False
        is_venue_mapping = False
        registered_male = 0
        registered_female = 0
        total = 0

        if entry:
            registered_male = entry.registered_male or 0
            registered_female = entry.registered_female or 0
            total = registered_male + registered_female

            # Check if there are actual registrations
            has_registration = total > 0

            # Check if it's a venue mapping (has venue but no registrations)
            is_venue_mapping = (entry.venue and entry.venue.strip() and total == 0)

        kit_data = {
            "kit_id": kit.id,
            "kit_name": kit.kit_name,
            "serial_no": kit.serial_no,
            "venue": entry.venue if entry else "",
            "registered_male": registered_male,
            "registered_female": registered_female,
            "total_registered": total,
            "total_transferred": entry.total_transferred if entry else 0,
            "has_entry": bool(entry),
            "has_registration": has_registration,  # NEW: actual voter registrations
            "is_venue_mapping": is_venue_mapping,  # NEW: venue only, no registrations
            "is_today": selected_date == timezone.localdate(),
            "selected_date": selected_date.strftime('%Y-%m-%d'),
            "is_future": selected_date > timezone.localdate(),
            "entry_type": entry.entry_type if entry else None,  # NEW: entry type from model
        }
        data.append(kit_data)

    return JsonResponse({
        "kits": data,
        "ward_name": vra.ward.name,
        "vra_name": vra.name,
        "kit_count": len(data),
        "selected_date": selected_date.strftime('%Y-%m-%d'),
        "is_today": selected_date == timezone.localdate(),
        "is_future": selected_date > timezone.localdate(),
        "device_authorized": True
    })


@csrf_exempt
@require_POST
def submit_daily_entries(request):
    """
    Submit daily entries for a VRA's ward.

    Semantics:
      - Every kit the VRA submits is saved with entry_type='REGISTRATION'.
        A 0/0/0 submission is still valid — it's the VRA saying
        "I checked this kit today; nothing happened here".
      - VENUE-type rows are only produced by the clerk pre-mapping tool,
        never by this endpoint.
      - This view NEVER sends the grand-total report. It only updates
        the constituency's DailyReportState; the reconciler worker sends.
    """
    # ---------- Auth ----------
    fingerprint = request.POST.get("fingerprint")
    if fingerprint:
        try:
            device = Device.objects.select_related("vra").get(
                fingerprint=fingerprint,
                is_burned=True,  # is_burned=True ⇒ authorized
                is_active=True,
            )
            vra = device.vra
        except Device.DoesNotExist:
            return JsonResponse(
                {"ok": False, "error": "Device not authorized"}, status=401
            )
    else:
        token = request.POST.get("token")
        if not token:
            return JsonResponse(
                {"ok": False, "error": "No authentication provided"}, status=400
            )
        vra = get_object_or_404(VRA, device_token=token, active=True)

    if not vra:
        return JsonResponse(
            {"ok": False, "error": "No VRA associated with this device"},
            status=404,
        )
    if not vra.ward:
        return JsonResponse(
            {"ok": False, "error": "VRA is not assigned to a ward"}, status=400
        )

    # ---------- Phase + date ----------
    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        return JsonResponse(
            {"ok": False, "error": "No active phase found"}, status=404
        )

    date_str = request.POST.get("date")
    try:
        entry_date = (
            datetime.strptime(date_str, "%Y-%m-%d").date()
            if date_str else timezone.localdate()
        )
    except (ValueError, TypeError):
        entry_date = timezone.localdate()

    if entry_date < timezone.localdate():
        return JsonResponse(
            {"ok": False, "error": "Cannot submit entries for past dates"},
            status=403,
        )

    # ---------- Parse arrays ----------
    kit_ids = request.POST.getlist("kit_id[]")
    venues = request.POST.getlist("venue[]")
    male_vals = request.POST.getlist("registered_male[]")
    female_vals = request.POST.getlist("registered_female[]")

    if not kit_ids:
        return JsonResponse(
            {"ok": False, "error": "No entries provided"}, status=400
        )

    errors = {}
    saved = 0
    registrations_created = []  # entries that carry actual numbers (or zero-final)
    updated_entries = []  # entries that already existed and were edited

    # ---------- Loop ----------
    for i, kit_id in enumerate(kit_ids):
        venue = (venues[i] if i < len(venues) else "").strip()

        try:
            male_count = int(male_vals[i]) if i < len(male_vals) and male_vals[i] else 0
        except (ValueError, TypeError):
            male_count = 0
        try:
            female_count = int(female_vals[i]) if i < len(female_vals) and female_vals[i] else 0
        except (ValueError, TypeError):
            female_count = 0

        if male_count < 0:
            male_count = 0
        if female_count < 0:
            female_count = 0

        if not venue:
            errors[kit_id] = "Venue is required."
            continue

        # Scope kit lookup to this VRA's ward so cross-ward submissions fail loudly
        try:
            kit = KIEMSKit.objects.get(id=kit_id, ward=vra.ward, status=True)
        except KIEMSKit.DoesNotExist:
            errors[kit_id] = "Kit not found in your ward."
            continue

        total_count = male_count + female_count

        # --- CRITICAL: a VRA submission is always REGISTRATION ---
        # Even at 0/0/0 it means "I have finished this kit for today".
        # VENUE-type rows are exclusively produced by the clerk pre-map tool.
        entry_type = "REGISTRATION"

        entry, created = DailyKIEMSEntry.objects.get_or_create(
            kiems_kit=kit,
            phase=active_phase,
            entry_date=entry_date,
            vra=vra,
            defaults={
                "ward": vra.ward,
                "venue": venue,
                "registered_male": male_count,
                "registered_female": female_count,
                "total_registered": total_count,
                "entry_type": entry_type,
            },
        )

        # Was there a real registration on this row *before* this save?
        was_registration_before = (not created) and (entry.total_registered > 0)

        if not created:
            entry.venue = venue
            entry.registered_male = male_count
            entry.registered_female = female_count
            entry.entry_type = entry_type
            entry.edit_count += 1
            entry.save()

        # Bucket for notifications/logging
        if created:
            registrations_created.append(entry)
        else:
            entry._was_registration_before = was_registration_before
            updated_entries.append(entry)

        saved += 1

    # ---------- WhatsApp notifications (per-entry) ----------
    try:
        settings_obj = get_whatsapp_settings()
        notify_vra = settings_obj.notify_vra if settings_obj else True
        notify_edit = settings_obj.notify_edit if settings_obj else True

        # Only fire messages when there is something to say (non-zero totals).
        for entry in registrations_created:
            if notify_vra:
                message = format_vra_submission_message(entry, is_update=False)
                if message:
                    send_whatsapp_message_from_vra(message, vra)

        for entry in updated_entries:
            if notify_edit:
                is_update = getattr(entry, "_was_registration_before", False)
                message = format_vra_submission_message(entry, is_update=is_update)
                if message:
                    send_whatsapp_message_from_vra(message, vra)
    except Exception as e:
        print(f"WhatsApp per-entry error: {str(e)}")

    # ---------- Re-evaluate the constituency report state ----------
    # This is the ONLY trigger. It updates DailyReportState but never sends.
    # A separate worker (cron / tick) will pick up READY states and send.
    try:
        from home.services.daily_report import reevaluate_constituency_report
        reevaluate_constituency_report(vra.ward.constituency, entry_date)
    except Exception as e:
        # Don't fail the submission because state recomputation hiccupped;
        # the next submission or the periodic tick will catch up.
        print(f"State re-evaluation error: {str(e)}")

    # ---------- Response ----------
    if errors:
        return JsonResponse(
            {"ok": False, "errors": errors, "saved": saved}, status=400
        )

    return JsonResponse({
        "ok": True,
        "saved": saved,
        "registrations": len(registrations_created),
        "updates": len(updated_entries),
        "message": f"{saved} entr{'y' if saved == 1 else 'ies'} submitted successfully!",
    })


# ==================== CLERK MAPPING API ENDPOINTS ====================


def clerk_venue_mapping_view(request):
    """Clerk venue mapping tool - assign venues to entries."""
    context = {
        'active_phase': Phase.objects.filter(active=True).first(),
    }
    return render(request, 'clerk_mapping.html', context)


@require_http_methods(["GET"])
def ward_list(request):
    """Get list of all active wards."""
    try:
        wards = Ward.objects.all().order_by('name')
        ward_data = [{'id': w.id, 'name': w.name} for w in wards]
        return JsonResponse({
            'ok': True,
            'wards': ward_data,
            'count': len(ward_data)
        })
    except Exception as e:
        return JsonResponse({
            'ok': False,
            'error': str(e)
        }, status=500)


@require_http_methods(["GET"])
def kit_list(request):
    """Get kits assigned to a specific ward."""
    ward_id = request.GET.get('ward_id')

    if not ward_id:
        return JsonResponse({'error': 'Ward ID is required'}, status=400)

    try:
        ward = Ward.objects.get(id=ward_id)
    except Ward.DoesNotExist:
        return JsonResponse({'error': 'Ward not found'}, status=404)

    # Get kits for this ward (KIEMSKit has ward ForeignKey directly)
    kits = KIEMSKit.objects.filter(ward=ward, status=True).order_by('kit_name')

    kits_data = []
    for kit in kits:
        kits_data.append({
            'id': kit.id,
            'kit_name': kit.kit_name,
            'serial_no': kit.serial_no,
        })

    return JsonResponse({
        'ok': True,
        'ward_name': ward.name,
        'kits': kits_data,
        'count': len(kits_data)
    })


@require_http_methods(["GET"])
def clerk_records(request):
    """Get ALL records for a specific kit, across every date on file."""
    kit_id = request.GET.get('kit_id')
    fingerprint = request.GET.get('fingerprint')

    if not kit_id:
        return JsonResponse({'error': 'Kit ID is required'}, status=400)

    try:
        kit = KIEMSKit.objects.get(id=kit_id, status=True)
    except KIEMSKit.DoesNotExist:
        return JsonResponse({'error': 'Kit not found'}, status=404)

    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        return JsonResponse({'error': 'No active phase found'}, status=404)

    # Every entry for this kit, across all dates
    entries = DailyKIEMSEntry.objects.filter(
        kiems_kit=kit,
        phase=active_phase,
    ).order_by('-entry_date')

    records = []
    for entry in entries:
        # Determine if this is a registration or venue mapping
        total = (entry.registered_male or 0) + (entry.registered_female or 0)
        is_registration = total > 0
        is_venue_mapping = bool(entry.venue and entry.venue.strip() and not is_registration)

        records.append({
            'entry_id': entry.id,
            'date': entry.entry_date.isoformat(),
            'venue': entry.venue or '',
            'editable': True,
            'has_registration': is_registration,
            'is_venue_mapping': is_venue_mapping,
            'entry_type': entry.entry_type,
        })

    # Ensure there's always a row for today, even if nothing's been saved yet
    today_str = timezone.now().date().isoformat()
    if not any(r['date'] == today_str for r in records):
        records.insert(0, {
            'entry_id': 0,
            'date': today_str,
            'venue': '',
            'editable': True,
            'is_new': True,
            'has_registration': False,
            'is_venue_mapping': False,
            'entry_type': None,
        })

    clerk_data = None
    if fingerprint:
        try:
            device = Device.objects.select_related('clerk', 'clerk__ward').get(
                fingerprint=fingerprint,
                is_burned=True,
                is_active=True
            )
            if device.clerk:
                clerk_data = {
                    'id': device.clerk.id,
                    'name': device.clerk.name,
                    'ward_name': device.clerk.ward.name if device.clerk.ward else None,
                }
        except Device.DoesNotExist:
            pass

    return JsonResponse({
        'ok': True,
        'kit_name': kit.kit_name,
        'kit_serial': kit.serial_no,
        'records': records,
        'count': len(records),
        'clerk': clerk_data,
    })


@csrf_exempt
@require_http_methods(["POST"])
def save_clerk_venues(request):
    """
    Save venue updates for one or more entries.
    DISTINGUISHES between venue mappings and registration entries.
    entry_type is always derived from the actual registered_male/registered_female
    counts on the record - never trusted from a client-supplied flag - so a
    venue-only premap can never masquerade as a REGISTRATION entry.
    """
    try:
        data = json.loads(request.body)
        kit_id = data.get('kit_id')
        updates = data.get('updates', [])
        fallback_date_str = data.get('date')
        ward_id = data.get('ward_id')
        fingerprint = data.get('fingerprint')

        if not updates:
            return JsonResponse({'error': 'No updates provided'}, status=400)

        active_phase = Phase.objects.filter(active=True).first()
        if not active_phase:
            return JsonResponse({'error': 'No active phase found'}, status=404)

        # Fallback date used only when an individual update has none
        try:
            fallback_date_obj = (
                datetime.strptime(fallback_date_str, '%Y-%m-%d').date()
                if fallback_date_str else timezone.now().date()
            )
        except ValueError:
            fallback_date_obj = timezone.now().date()

        # Resolve clerk / VRA from device fingerprint (or token fallback)
        clerk = None
        vra = None

        if fingerprint:
            try:
                device = Device.objects.select_related(
                    'clerk', 'clerk__ward', 'vra', 'vra__ward'
                ).get(fingerprint=fingerprint, is_burned=True, is_active=True)
                clerk = device.clerk
                vra = device.vra
                print(f"[clerk-mapping] Found device: clerk={clerk.id if clerk else None}, "
                      f"vra={vra.id if vra else None}")
            except Device.DoesNotExist:
                print(f"[clerk-mapping] Device not found for fingerprint: {fingerprint}")

        if not clerk and not vra:
            token = request.GET.get('token') or request.POST.get('token')
            if token:
                clerk = Clerk.objects.filter(device_token=token, active=True).first()
                if not clerk:
                    vra = VRA.objects.filter(device_token=token, active=True).first()
                print(f"[clerk-mapping] Token fallback: clerk={clerk.id if clerk else None}, "
                      f"vra={vra.id if vra else None}")

        saved_count = 0
        errors = []
        updated_entries = []
        created_entries = []
        venue_mappings_created = []
        created_ids = []

        for update in updates:
            entry_id = update.get('entry_id')
            venue = (update.get('venue') or '').strip()
            kit_id_from_update = update.get('kit_id')
            row_date_str = update.get('date')

            # Per-row date, falling back to the request-level date
            try:
                row_date_obj = (
                    datetime.strptime(row_date_str, '%Y-%m-%d').date()
                    if row_date_str else fallback_date_obj
                )
            except ValueError:
                row_date_obj = fallback_date_obj

            if entry_id is None:
                errors.append('Missing entry_id')
                continue

            try:
                entry_id = int(entry_id)
            except (ValueError, TypeError):
                errors.append(f'Invalid entry_id: {entry_id}')
                continue

            if not venue:
                errors.append(f'Entry {entry_id}: venue is required')
                continue

            actual_kit_id = kit_id_from_update or kit_id

            print(f"[clerk-mapping] Processing: entry_id={entry_id}, kit_id={actual_kit_id}, "
                  f"date={row_date_obj}, venue={venue}")

            # entry_id <= 0 always means "new row from the table"
            is_from_mapping = (
                    entry_id <= 0
                    or kit_id_from_update is not None
                    or not DailyKIEMSEntry.objects.filter(id=entry_id).exists()
            )

            if is_from_mapping:
                if not actual_kit_id:
                    actual_kit_id = entry_id if entry_id > 0 else None

                if not actual_kit_id:
                    errors.append('Missing kit_id for new entry')
                    continue

                try:
                    kit = KIEMSKit.objects.get(id=actual_kit_id, status=True)
                except KIEMSKit.DoesNotExist:
                    errors.append(f'Kit {actual_kit_id} not found')
                    continue

                # Does an entry already exist for this kit on this exact date?
                existing_entry = DailyKIEMSEntry.objects.filter(
                    kiems_kit=kit,
                    phase=active_phase,
                    entry_date=row_date_obj,
                ).first()

                if existing_entry:
                    old_venue = existing_entry.venue
                    existing_entry.venue = venue
                    # entry_type is derived from the record's own counts, never
                    # from a client flag - this tool never writes vote counts,
                    # so an entry only becomes REGISTRATION if it already has
                    # real registered voters on it from elsewhere.
                    existing_entry.entry_type = (
                        'REGISTRATION' if existing_entry.total_registered > 0 else 'VENUE'
                    )
                    existing_entry.save(update_fields=['venue', 'entry_type', 'updated_at'])
                    updated_entries.append(existing_entry)
                    saved_count += 1
                    print(f"[clerk-mapping] Updated existing entry {existing_entry.id} for kit "
                          f"{kit.id} on {row_date_obj}: '{old_venue}' -> '{venue}' "
                          f"(entry_type={existing_entry.entry_type})")
                else:
                    # Brand new row from this tool is always a pure venue mapping -
                    # it never carries vote counts, so it is always 'VENUE'.
                    entry_data = {
                        'kiems_kit': kit,
                        'phase': active_phase,
                        'ward': kit.ward,
                        'entry_date': row_date_obj,
                        'venue': venue,
                        'registered_male': 0,
                        'registered_female': 0,
                        'entry_type': 'VENUE',
                    }

                    if vra:
                        entry_data['vra'] = vra
                        entry_data['clerk'] = clerk if clerk else None
                    elif clerk:
                        ward_vra = VRA.objects.filter(ward=kit.ward, active=True).first()
                        if ward_vra:
                            entry_data['vra'] = ward_vra
                            entry_data['clerk'] = clerk
                        else:
                            any_vra = VRA.objects.filter(active=True).first()
                            if any_vra:
                                entry_data['vra'] = any_vra
                                entry_data['clerk'] = clerk
                            else:
                                errors.append(f'No VRA available for ward {kit.ward.name}')
                                continue
                    else:
                        if ward_id:
                            ward = Ward.objects.filter(id=ward_id).first()
                            if ward:
                                ward_vra = VRA.objects.filter(ward=ward, active=True).first()
                                entry_data['vra'] = ward_vra or VRA.objects.filter(active=True).first()
                                if not entry_data['vra']:
                                    errors.append('No VRA available')
                                    continue
                            else:
                                errors.append(f'Ward {ward_id} not found')
                                continue
                        else:
                            any_vra = VRA.objects.filter(active=True).first()
                            if any_vra:
                                entry_data['vra'] = any_vra
                            else:
                                errors.append('No VRA available')
                                continue

                    try:
                        entry = DailyKIEMSEntry.objects.create(**entry_data)
                    except Exception as e:
                        # Unique constraint race - fall back to update
                        existing_entry = DailyKIEMSEntry.objects.filter(
                            kiems_kit=kit, phase=active_phase, entry_date=row_date_obj
                        ).first()
                        if existing_entry:
                            existing_entry.venue = venue
                            existing_entry.entry_type = (
                                'REGISTRATION' if existing_entry.total_registered > 0 else 'VENUE'
                            )
                            existing_entry.save(update_fields=['venue', 'entry_type', 'updated_at'])
                            updated_entries.append(existing_entry)
                            saved_count += 1
                            continue
                        errors.append(f'Could not create entry for kit {kit.id}: {str(e)}')
                        continue

                    created_entries.append(entry)
                    created_ids.append(entry.id)
                    saved_count += 1
                    print(f"[clerk-mapping] Created new venue-mapping entry {entry.id} for kit "
                          f"{kit.id} on {row_date_obj}")

            elif entry_id > 0:
                try:
                    entry = DailyKIEMSEntry.objects.get(id=entry_id)
                    old_venue = entry.venue
                    old_date = entry.entry_date

                    entry.venue = venue
                    # Derived purely from the record's own counts - this endpoint
                    # never touches registered_male/registered_female.
                    entry.entry_type = 'REGISTRATION' if entry.total_registered > 0 else 'VENUE'

                    # Allow the clerk to correct the date on an existing row too
                    if row_date_str and row_date_obj != entry.entry_date:
                        entry.entry_date = row_date_obj
                        entry.save(update_fields=['venue', 'entry_date', 'entry_type', 'updated_at'])
                    else:
                        entry.save(update_fields=['venue', 'entry_type', 'updated_at'])

                    updated_entries.append(entry)
                    saved_count += 1
                    print(f"[clerk-mapping] Updated entry {entry_id}: date={old_date}->{entry.entry_date}, "
                          f"venue: '{old_venue}' -> '{venue}' (entry_type={entry.entry_type})")

                except DailyKIEMSEntry.DoesNotExist:
                    errors.append(f'Entry {entry_id} not found')
                except Exception as e:
                    errors.append(f'Entry {entry_id}: {str(e)}')
            else:
                errors.append(f'Invalid entry_id: {entry_id}')

        # Only notify WhatsApp when there's actual voter-count data (not plain venue edits)
        try:
            settings_obj = get_whatsapp_settings()
            notify_vra = settings_obj.notify_vra if settings_obj else True
            notify_edit = settings_obj.notify_edit if settings_obj else True

            registration_entries = [e for e in updated_entries + created_entries
                                    if e.total_registered > 0]
            venue_only_entries = [e for e in updated_entries + created_entries
                                  if e.total_registered == 0]

            if registration_entries and vra:
                for entry in registration_entries:
                    if entry.vra:
                        is_update = entry in updated_entries
                        should_send = notify_edit if is_update else notify_vra
                        if should_send:
                            message = format_vra_submission_message(entry, is_update)
                            if message:
                                send_whatsapp_message_from_vra(message, entry.vra)

            if venue_only_entries:
                print(f"Venue mappings saved: {len(venue_only_entries)} entries")

        except Exception as e:
            print(f"[clerk-mapping] WhatsApp error: {str(e)}")

        return JsonResponse({
            'ok': True,
            'saved': saved_count,
            'created': len(created_entries),
            'updated': len(updated_entries),
            'registrations': len([e for e in created_entries + updated_entries if e.total_registered > 0]),
            'venue_mappings': len([e for e in created_entries + updated_entries if e.total_registered == 0]),
            'entry_ids': created_ids,
            'errors': errors if errors else None,
            'message': f'Saved {saved_count} entr{"y" if saved_count == 1 else "ies"}.',
        })

    except json.JSONDecodeError:
        return JsonResponse({'error': 'Invalid JSON data'}, status=400)
    except Exception as e:
        print(f"[clerk-mapping] Save error: {str(e)}")
        return JsonResponse({'ok': False, 'error': str(e)}, status=500)


# ==================== MOVEMENT SCHEDULE (CLIENT) ====================

def movement_schedule_view(request):
    """Client page: pick ward, see all kits, enter tomorrow's venue per kit."""
    bound_constituency = None
    bound_ward = None
    bound_clerk = None
    bound_vra = None

    # Try VRA first, then Clerk
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

    constituencies = Constituency.objects.filter(active=True).order_by("name")
    wards = Ward.objects.select_related("constituency").order_by(
        "constituency__name", "name"
    )
    active_phase = Phase.objects.filter(active=True).first()

    # Tomorrow (not today!)
    tomorrow = timezone.localdate() + timedelta(days=1)

    return render(request, "movement_schedule.html", {
        "constituencies": constituencies,
        "wards": wards,
        "bound_constituency": bound_constituency,
        "bound_ward": bound_ward,
        "bound_vra": bound_vra,
        "bound_clerk": bound_clerk,
        "active_phase": active_phase,
        "schedule_date": tomorrow.isoformat(),
    })


@require_GET
def movement_kits(request):
    """
    Given a ward, return all active kits for that ward plus any existing
    MovementSchedule row for tomorrow (so the client can pre-fill).
    """
    ward_id = request.GET.get("ward_id")
    fingerprint = request.GET.get("fingerprint")
    token = request.GET.get("token")

    if not ward_id:
        return JsonResponse({"ok": False, "error": "ward_id required"}, status=400)

    try:
        ward = Ward.objects.select_related("constituency").get(id=ward_id, active=True)
    except Ward.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Ward not found"}, status=404)

    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        return JsonResponse({"ok": False, "error": "No active phase"}, status=404)

    schedule_date = timezone.localdate() + timedelta(days=1)

    kits = KIEMSKit.objects.filter(ward=ward, status=True).order_by("kit_name")

    existing = {
        m.kiems_kit_id: m
        for m in MovementSchedule.objects.filter(
            kiems_kit__in=kits,
            phase=active_phase,
            schedule_date=schedule_date,
        )
    }

    data = []
    for kit in kits:
        row = existing.get(kit.id)
        data.append({
            "kit_id": kit.id,
            "kit_name": kit.kit_name,
            "serial_no": kit.serial_no,
            "venue": row.venue if row else "",
            "has_schedule": bool(row),
            "notes": row.notes if row else "",
        })

    return JsonResponse({
        "ok": True,
        "ward_id": ward.id,
        "ward_name": ward.name,
        "constituency_id": ward.constituency_id,
        "constituency_name": ward.constituency.name if ward.constituency else None,
        "schedule_date": schedule_date.isoformat(),
        "kits": data,
    })




def _resolve_submitter(fingerprint, token):
    """Return (vra, clerk) for the device or token. Either may be None."""
    vra = clerk = None

    if fingerprint:
        device = (
            Device.objects
            .select_related("vra", "vra__ward", "clerk", "clerk__ward")
            .filter(fingerprint=fingerprint, is_burned=True, is_active=True)
            .first()
        )
        if device:
            vra = device.vra
            clerk = device.clerk

    if not (vra or clerk) and token:
        vra = VRA.objects.select_related("ward").filter(
            device_token=token, active=True
        ).first()
        if not vra:
            clerk = Clerk.objects.select_related("ward").filter(
                device_token=token, active=True
            ).first()

    return vra, clerk


def _resolve_schedule_date(raw):
    """
    Use the date the page displayed if it is today or tomorrow (covers a page
    left open past midnight). Anything else falls back to tomorrow.
    """
    today = timezone.localdate()
    tomorrow = today + timedelta(days=1)
    if raw:
        try:
            d = datetime.strptime(str(raw), "%Y-%m-%d").date()
            if today <= d <= tomorrow:
                return d
        except (ValueError, TypeError):
            pass
    return tomorrow


@csrf_exempt
@require_POST
def save_movement_schedule(request):
    """
    Save tomorrow's venue for one or more kits in one ward.

    Body: {
        ward_id, fingerprint?, token?, schedule_date?,
        entries: [{kit_id, venue, notes?}]
    }

    Behaviour:
      - Only VRAs/clerks bound to the ward may save (see MOVEMENT_REQUIRE_BOUND_DEVICE).
      - `notes` is only changed when the client actually sends it.
      - Rows whose venue/notes did not change are skipped: no edit_count bump,
        no WhatsApp message.
      - Valid rows are saved even if others fail; failures come back in `errors`
        keyed by kit id.
    """
    # ---------- Parse body ----------
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, TypeError):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    if not isinstance(data, dict):
        return JsonResponse({"ok": False, "error": "Invalid JSON"}, status=400)

    ward_id = data.get("ward_id")
    fingerprint = data.get("fingerprint")
    token = data.get("token")
    entries = data.get("entries")

    if not ward_id or not isinstance(entries, list) or not entries:
        return JsonResponse(
            {"ok": False, "error": "ward_id and entries required"}, status=400
        )

    try:
        ward_id = int(ward_id)
    except (ValueError, TypeError):
        return JsonResponse({"ok": False, "error": "Invalid ward_id"}, status=400)

    # ---------- Ward + phase ----------
    try:
        ward = Ward.objects.select_related("constituency").get(id=ward_id, active=True)
    except Ward.DoesNotExist:
        return JsonResponse({"ok": False, "error": "Ward not found"}, status=404)

    if not ward.constituency:
        return JsonResponse(
            {"ok": False, "error": "Ward has no constituency"}, status=400
        )

    active_phase = Phase.objects.filter(active=True).first()
    if not active_phase:
        return JsonResponse({"ok": False, "error": "No active phase"}, status=404)

    # ---------- Who is submitting ----------
    vra, clerk = _resolve_submitter(fingerprint, token)

    if MOVEMENT_REQUIRE_BOUND_DEVICE:
        if not (vra or clerk):
            return JsonResponse(
                {
                    "ok": False,
                    "error": "This device is not authorized. Please contact your ICT officer.",
                },
                status=401,
            )
        if vra and vra.ward_id != ward.id:
            return JsonResponse(
                {"ok": False, "error": "You can only save for your own ward."},
                status=403,
            )
        if not vra and clerk and clerk.ward_id and clerk.ward_id != ward.id:
            return JsonResponse(
                {"ok": False, "error": "You can only save for your own ward."},
                status=403,
            )

    schedule_date = _resolve_schedule_date(data.get("schedule_date"))

    # ---------- Save loop ----------
    created, updated, errors = [], [], {}
    unchanged = 0

    for item in entries:
        if not isinstance(item, dict):
            errors["entry"] = "Invalid entry."
            continue

        kit_id = item.get("kit_id")
        venue = (item.get("venue") or "").strip()

        raw_notes = item.get("notes")
        notes = raw_notes.strip() if isinstance(raw_notes, str) else None  # None = not sent

        if not kit_id:
            errors["kit_id"] = "Missing kit_id"
            continue

        try:
            kit_id = int(kit_id)
        except (ValueError, TypeError):
            errors[str(kit_id)] = "Invalid kit_id."
            continue

        if not venue:
            errors[str(kit_id)] = "Venue is required."
            continue

        try:
            kit = KIEMSKit.objects.get(id=kit_id, ward=ward, status=True)
        except KIEMSKit.DoesNotExist:
            errors[str(kit_id)] = "Kit not in this ward."
            continue

        obj, was_created = MovementSchedule.objects.get_or_create(
            kiems_kit=kit,
            schedule_date=schedule_date,
            defaults={
                "ward": ward,
                "constituency": ward.constituency,
                "phase": active_phase,
                "vra": vra,
                "clerk": clerk,
                "venue": venue,
                "notes": notes or "",
            },
        )

        if was_created:
            created.append(obj)
            continue

        new_notes = obj.notes if notes is None else notes
        if obj.venue == venue and obj.notes == new_notes:
            unchanged += 1
            continue

        obj.venue = venue
        obj.notes = new_notes
        obj.edit_count += 1
        obj.save(update_fields=["venue", "notes", "edit_count", "updated_at"])
        updated.append(obj)

    # ---------- WhatsApp (one message per changed kit; never raises) ----------
    try:
        settings_obj = get_whatsapp_settings()
        notify_vra = settings_obj.notify_vra if settings_obj else True
        notify_edit = settings_obj.notify_edit if settings_obj else True

        if notify_vra:
            for obj in created:
                try:
                    send_to_constituency(
                        ward.constituency,
                        format_single_movement_message(obj, is_update=False),
                    )
                except Exception as e:
                    print(f"[movement] WhatsApp send failed (new kit {obj.kiems_kit_id}): {e}")

        if notify_edit:
            for obj in updated:
                try:
                    send_to_constituency(
                        ward.constituency,
                        format_single_movement_message(obj, is_update=True),
                    )
                except Exception as e:
                    print(f"[movement] WhatsApp send failed (edit kit {obj.kiems_kit_id}): {e}")
    except Exception as e:
        print(f"[movement] WhatsApp error: {e}")

    # ---------- Recompute report state (does not send the grand report) ----------
    if created or updated:
        try:
            from home.services.movement_schedule import reevaluate_movement_schedule
            reevaluate_movement_schedule(ward.constituency, schedule_date)
        except Exception as e:
            print(f"[movement] State re-evaluation error: {e}")

    # ---------- Response ----------
    counts = {
        "created": len(created),
        "updated": len(updated),
        "unchanged": unchanged,
    }

    if errors:
        return JsonResponse({"ok": False, "errors": errors, **counts}, status=400)

    saved = len(created) + len(updated)
    if saved == 0:
        message = "No changes to save."
    else:
        message = f"{saved} venue(s) saved for {schedule_date}."

    return JsonResponse({"ok": True, "message": message, **counts})