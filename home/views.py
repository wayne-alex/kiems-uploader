import json
from datetime import datetime

import requests
from django.conf import settings
from django.db.models import Sum, Q
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_GET, require_POST

from .models import (
    Ward, VRA, Clerk, KIEMSKit, Phase, DailyKIEMSEntry,
    Device, WhatsAppSetting, WhatsAppGroup
)


# ==================== WHATSAPP HELPER FUNCTIONS ====================

def get_whatsapp_group_for_vra(vra):
    """Get WhatsApp group for VRA submissions"""
    try:
        from django.contrib.auth.models import User
        admin_user = User.objects.filter(is_superuser=True).first()
        if admin_user:
            setting = WhatsAppSetting.objects.filter(user=admin_user).first()
            if setting and setting.default_group and setting.default_group.is_active:
                return setting.default_group.group_id

        group = WhatsAppGroup.objects.filter(is_active=True).first()
        if group:
            return group.group_id
    except Exception as e:
        print(f"WhatsApp group error: {str(e)}")
    return None


def send_whatsapp_message_from_vra(message, vra):
    """Send WhatsApp message from VRA submission"""
    try:
        group_id = get_whatsapp_group_for_vra(vra)
        if not group_id:
            return False

        bot_url = getattr(settings, 'WHATSAPP_BOT_URL', 'http://localhost:3000')
        response = requests.post(
            f"{bot_url}/send",
            json={'groupId': group_id, 'message': message},
            timeout=10,
            headers={'Content-Type': 'application/json'}
        )
        return response.status_code == 200
    except Exception as e:
        print(f"WhatsApp error: {str(e)}")
        return False

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


def check_and_send_daily_report_from_vra(vra):
    """Check if all wards submitted and send grand total - only for REGISTRATION entries with actual votes"""
    try:
        today = timezone.now().date()
        total_wards = Ward.objects.count()
        if total_wards == 0:
            return

        # Only count wards that have REGISTRATION entries with actual registered voters
        # (guards against mislabeled 0-count venue-mapping rows triggering this early)
        submitted_wards = DailyKIEMSEntry.objects.filter(
            entry_date=today,
            entry_type='REGISTRATION',
            total_registered__gt=0,
        ).values('ward').distinct().count()

        if submitted_wards < total_wards:
            return

        # Get all REGISTRATION entries for today with actual votes
        entries = DailyKIEMSEntry.objects.filter(
            entry_date=today,
            entry_type='REGISTRATION',
            total_registered__gt=0,
        )
        if not entries.exists():
            return

        message = format_grand_total_message(entries, total_wards)
        send_whatsapp_message_from_vra(message, vra)
    except Exception as e:
        print(f"Error checking daily report: {str(e)}")


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
    """Main entry view for VRA"""
    wards = Ward.objects.all()
    active_phase = Phase.objects.filter(active=True).first()
    return render(request, "home.html", {"wards": wards, "active_phase": active_phase})


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
    """Bind a VRA to a device using ward selection (legacy method)"""
    token = request.POST.get("token")
    ward_id = request.POST.get("ward_id")
    fingerprint = request.POST.get("fingerprint")

    # Try fingerprint first
    if fingerprint:
        try:
            device = Device.objects.get(fingerprint=fingerprint, is_burned=True, is_active=True)
            vra = VRA.objects.filter(ward_id=ward_id, active=True).first()
            if vra:
                device.vra = vra
                device.save(update_fields=['vra'])
                vra.device_fingerprint = fingerprint
                vra.save(update_fields=['device_fingerprint'])

                # Also set the device token for legacy support
                if not vra.device_token:
                    vra.device_token = fingerprint
                    vra.save(update_fields=['device_token'])

                return JsonResponse({
                    "ok": True,
                    "vra_id": vra.id,
                    "vra_name": vra.name,
                    "ward_id": vra.ward_id,
                    "ward_name": vra.ward.name,
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
    """Submit entries - distinguishes between venue mappings and registrations"""
    fingerprint = request.POST.get('fingerprint')

    if fingerprint:
        try:
            device = Device.objects.select_related('vra').get(
                fingerprint=fingerprint,
                is_burned=True,
                is_active=True
            )
            vra = device.vra
        except Device.DoesNotExist:
            return JsonResponse({"ok": False, "error": "Device not authorized"}, status=401)
    else:
        token = request.POST.get("token")
        if not token:
            return JsonResponse({"ok": False, "error": "No authentication provided"}, status=400)
        vra = get_object_or_404(VRA, device_token=token, active=True)

    if not vra:
        return JsonResponse({"ok": False, "error": "No VRA associated with this device"}, status=404)

    date_str = request.POST.get("date")
    active_phase = Phase.objects.filter(active=True).first()

    if not active_phase:
        return JsonResponse({"ok": False, "error": "No active phase found"}, status=404)

    try:
        if date_str:
            entry_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        else:
            entry_date = timezone.localdate()
    except ValueError:
        entry_date = timezone.localdate()

    if entry_date < timezone.localdate():
        return JsonResponse({"ok": False, "error": "Cannot submit entries for past dates"}, status=403)

    kit_ids = request.POST.getlist("kit_id[]")
    venues = request.POST.getlist("venue[]")
    male_vals = request.POST.getlist("registered_male[]")
    female_vals = request.POST.getlist("registered_female[]")

    errors = {}
    saved = 0
    entries_created = []
    venue_mappings_created = []

    for i, kit_id in enumerate(kit_ids):
        venue = venues[i].strip() if i < len(venues) else ""
        male_count = int(male_vals[i]) if i < len(male_vals) and male_vals[i] and male_vals[i].isdigit() else 0
        female_count = int(female_vals[i]) if i < len(female_vals) and female_vals[i] and female_vals[
            i].isdigit() else 0

        if not venue:
            errors[kit_id] = "Venue is required."
            continue

        has_registrations = male_count > 0 or female_count > 0
        kit = get_object_or_404(KIEMSKit, id=kit_id, ward=vra.ward)
        total_count = male_count + female_count
        entry_type = 'REGISTRATION' if has_registrations else 'VENUE'

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

        # The row may already exist purely because a venue was pre-mapped -
        # that is NOT a prior "registration". Only treat this as an UPDATE
        # if it already had actual registered voters on it before this save.
        is_update = (not created) and entry.total_registered > 0

        if not created:
            entry.venue = venue
            entry.registered_male = male_count
            entry.registered_female = female_count
            entry.entry_type = entry_type
            entry.edit_count += 1
            entry.save()

        if has_registrations:
            entries_created.append(entry)
            entry._is_update_for_message = is_update
        else:
            venue_mappings_created.append(entry)

        saved += 1

    # Send WhatsApp notifications ONLY for REGISTRATION entries
    try:
        if entries_created:
            for entry in entries_created:
                is_update = getattr(entry, '_is_update_for_message', False)
                message = format_vra_submission_message(entry, is_update)
                if message:
                    send_whatsapp_message_from_vra(message, vra)

            check_and_send_daily_report_from_vra(vra)

        if venue_mappings_created:
            print(f"Venue mappings created: {len(venue_mappings_created)} entries")
            for vm in venue_mappings_created:
                print(f"  - {vm.ward.name}: {vm.venue} on {vm.entry_date}")

    except Exception as e:
        print(f"WhatsApp error: {str(e)}")

    if errors:
        return JsonResponse({"ok": False, "errors": errors, "saved": saved}, status=400)

    return JsonResponse({
        "ok": True,
        "saved": saved,
        "registrations": len(entries_created),
        "venue_mappings": len(venue_mappings_created),
        "message": f"{saved} entries submitted successfully!"
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
            registration_entries = [e for e in updated_entries + created_entries
                                    if e.total_registered > 0]
            venue_only_entries = [e for e in updated_entries + created_entries
                                  if e.total_registered == 0]

            if registration_entries and vra:
                for entry in registration_entries:
                    if entry.vra:
                        is_update = entry in updated_entries
                        message = format_vra_submission_message(entry, is_update)
                        if message:
                            send_whatsapp_message_from_vra(message, entry.vra)
                check_and_send_daily_report_from_vra(vra)

            if venue_only_entries:
                print(f"? Venue mappings saved: {len(venue_only_entries)} entries")

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