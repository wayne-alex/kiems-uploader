import json
from django.db import transaction
from django.db.models import Sum, Q
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
import requests
from django.conf import settings

from .models import Ward, VRA, Phase, KIEMSKit, DailyKIEMSEntry, Device, DeviceBurnLog


# ==================== WHATSAPP HELPER FUNCTIONS ====================

def get_whatsapp_group_for_vra(vra):
    """Get WhatsApp group for VRA submissions"""
    try:
        from django.contrib.auth.models import User
        from .models import WhatsAppSetting, WhatsAppGroup

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
    """Format VRA submission message"""
    if is_update:
        message = f"{entry.ward.name.upper()} UPDATED\n"
    else:
        message = f"{entry.ward.name.upper()} CONFIRMED\n"

    message += f"{entry.kiems_kit.kit_name}: MALE:{entry.registered_male} FEMALE:{entry.registered_female} = {entry.total_registered}\n"

    if entry.total_transferred and entry.total_transferred > 0:
        message += f"Transferred: {entry.total_transferred}"

    return message


def format_grand_total_message(entries, total_wards):
    """Format grand total message"""
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

    message = f"{today.strftime('%d %b %Y')}\n"
    message += f"All {total_wards} Wards Submitted!\n\n"

    for w in ward_data:
        message += f"{w['ward__name']}: MALE:{w['male']} FEMALE:{w['female']} = {w['total']}\n"

    message += f"\nTOTAL: MALE:{total_male} FEMALE:{total_female} = {total_registered}"
    if total_transferred > 0:
        message += f" | Transferred: {total_transferred}"

    return message


def check_and_send_daily_report_from_vra(vra):
    """Check if all wards submitted and send grand total"""
    try:
        today = timezone.now().date()
        total_wards = Ward.objects.count()
        if total_wards == 0:
            return

        submitted_wards = DailyKIEMSEntry.objects.filter(
            entry_date=today
        ).values('ward').distinct().count()

        if submitted_wards < total_wards:
            return

        entries = DailyKIEMSEntry.objects.filter(entry_date=today)
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


def get_device_from_fingerprint(fingerprint):
    """Get device from fingerprint"""
    if not fingerprint:
        return None
    try:
        return Device.objects.select_related('vra', 'vra__ward').get(
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

        return JsonResponse({
            'ok': True,
            'device_id': device.id,
            'is_burned': device.is_burned,
            'is_active': device.is_active,
            'vra_id': device.vra_id,
            'vra_name': device.vra.name if device.vra else None,
            'ward_id': device.vra.ward_id if device.vra else None,
            'ward_name': device.vra.ward.name if device.vra else None,
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
        device = Device.objects.select_related('vra', 'vra__ward').get(fingerprint=fingerprint)

        return JsonResponse({
            'ok': True,
            'is_authorized': device.is_burned,
            'is_active': device.is_active,
            'vra_id': device.vra_id,
            'vra_name': device.vra.name if device.vra else None,
            'ward_id': device.vra.ward_id if device.vra else None,
            'ward_name': device.vra.ward.name if device.vra else None,
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


@require_POST
def auto_bind_vra(request):
    """Auto-bind a VRA to a device using fingerprint"""
    try:
        data = json.loads(request.body) if request.body else request.POST
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


@require_GET
def kits_with_entries(request):
    """Get kits with entries - uses fingerprint for auth"""
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
            selected_date = timezone.datetime.strptime(date_str, '%Y-%m-%d').date()
        else:
            selected_date = timezone.localdate()
    except ValueError:
        selected_date = timezone.localdate()

    # Get all kits for this ward
    kits = KIEMSKit.objects.filter(ward=vra.ward, status=True).order_by('kit_name')

    # Get existing entries for the selected date
    existing = {
        e.kiems_kit_id: e for e in DailyKIEMSEntry.objects.filter(
            kiems_kit__in=kits,
            phase=active_phase,
            entry_date=selected_date,
            vra=vra
        )
    }

    data = []
    for kit in kits:
        entry = existing.get(kit.id)
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
            "has_entry": bool(entry),
            "is_today": selected_date == timezone.localdate(),
            "selected_date": selected_date.strftime('%Y-%m-%d'),
        }
        data.append(kit_data)

    return JsonResponse({
        "kits": data,
        "ward_name": vra.ward.name,
        "vra_name": vra.name,
        "kit_count": len(data),
        "selected_date": selected_date.strftime('%Y-%m-%d'),
        "is_today": selected_date == timezone.localdate(),
        "device_authorized": True
    })


@require_POST
def submit_daily_entries(request):
    """Submit entries - uses fingerprint for auth"""
    fingerprint = request.POST.get('fingerprint')

    # Try to find VRA by fingerprint
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
        # Fallback to token-based
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

    # Parse date or use today
    try:
        if date_str:
            entry_date = timezone.datetime.strptime(date_str, '%Y-%m-%d').date()
        else:
            entry_date = timezone.localdate()
    except ValueError:
        entry_date = timezone.localdate()

    # Prevent editing entries from past dates (only allow today)
    if entry_date != timezone.localdate():
        return JsonResponse({"ok": False, "error": "Can only submit entries for today's date"}, status=403)

    kit_ids = request.POST.getlist("kit_id[]")
    venues = request.POST.getlist("venue[]")
    male_vals = request.POST.getlist("registered_male[]")
    female_vals = request.POST.getlist("registered_female[]")

    errors = {}
    saved = 0
    entries_created = []

    for i, kit_id in enumerate(kit_ids):
        venue = venues[i].strip() if i < len(venues) else ""
        male_count = int(male_vals[i]) if i < len(male_vals) and male_vals[i] and male_vals[i].isdigit() else 0
        female_count = int(female_vals[i]) if i < len(female_vals) and female_vals[i] and female_vals[
            i].isdigit() else 0

        if not venue:
            errors[kit_id] = "Venue is required."
            continue

        if male_count == 0 and female_count == 0:
            errors[kit_id] = "Enter at least one registered voter (Male/Female)."
            continue

        kit = get_object_or_404(KIEMSKit, id=kit_id, ward=vra.ward)

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
            },
        )

        is_update = False
        if not created:
            is_update = True
            entry.venue = venue
            entry.registered_male = male_count
            entry.registered_female = female_count
            entry.edit_count += 1
            entry.save(update_fields=["venue", "registered_male", "registered_female",
                                      "edit_count", "updated_at"])

        entries_created.append(entry)
        saved += 1

    # Send WhatsApp notifications
    try:
        for entry in entries_created:
            is_update = entry.edit_count > 0
            message = format_vra_submission_message(entry, is_update)
            send_whatsapp_message_from_vra(message, vra)

        if entries_created:
            check_and_send_daily_report_from_vra(vra)
    except Exception as e:
        print(f"WhatsApp error: {str(e)}")

    if errors:
        return JsonResponse({"ok": False, "errors": errors, "saved": saved}, status=400)

    return JsonResponse({
        "ok": True,
        "saved": saved,
        "message": "Entries submitted successfully!"
    })