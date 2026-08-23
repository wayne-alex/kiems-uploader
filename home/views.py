from django.db.models import Sum
from django.http import JsonResponse
from django.shortcuts import render, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST
import requests
from django.conf import settings

from .models import Ward, VRA, Phase, KIEMSKit, DailyKIEMSEntry


# ==================== WHATSAPP HELPER FUNCTIONS ====================

def get_whatsapp_group_for_vra(vra):
    """Get WhatsApp group for VRA submissions - uses superadmin settings"""
    try:
        from django.contrib.auth.models import User
        from .models import WhatsAppSetting, WhatsAppGroup

        # Get the first superadmin user
        admin_user = User.objects.filter(is_superuser=True).first()
        if admin_user:
            setting = WhatsAppSetting.objects.filter(user=admin_user).first()
            if setting and setting.default_group and setting.default_group.is_active:
                return setting.default_group.group_id

        # Fallback: get first active group
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
            print("No WhatsApp group configured for VRA submissions")
            return False

        bot_url = getattr(settings, 'WHATSAPP_BOT_URL', 'http://localhost:3000')
        response = requests.post(
            f"{bot_url}/send",
            json={'groupId': group_id, 'message': message},
            timeout=10,
            headers={'Content-Type': 'application/json'}
        )

        if response.status_code == 200:
            print("✅ WhatsApp message sent successfully from VRA")
            return True
        else:
            print(f"❌ WhatsApp send failed: {response.text}")
            return False
    except Exception as e:
        print(f"❌ WhatsApp error: {str(e)}")
        return False


def format_vra_submission_message(entry, is_update=False):
    """Format VRA submission or update message - Super Simple"""

    # Header
    if is_update:
        message = f"{entry.ward.name.upper()} UPDATED ✏️\n"
    else:
        message = f"{entry.ward.name.upper()} CONFIRMED ✅ \n"

    # Kit line: Kit Name: M / F = Total
    message += f"{entry.kiems_kit.kit_name}: MALE:{entry.registered_male} FEMALE:{entry.registered_female} = {entry.total_registered}\n"

    # Transferred if any
    if entry.total_transferred and entry.total_transferred > 0:
        message += f"Transferred: {entry.total_transferred}"

    return message


def format_grand_total_message(entries, total_wards):
    """Format grand total message when all wards submit - Super Simple"""
    today = timezone.now().date()

    # Calculate grand totals
    total_male = entries.aggregate(Sum('registered_male'))['registered_male__sum'] or 0
    total_female = entries.aggregate(Sum('registered_female'))['registered_female__sum'] or 0
    total_registered = entries.aggregate(Sum('total_registered'))['total_registered__sum'] or 0
    total_transferred = entries.aggregate(Sum('total_transferred'))['total_transferred__sum'] or 0

    # Get per ward breakdown
    ward_data = entries.values('ward__name').annotate(
        male=Sum('registered_male'),
        female=Sum('registered_female'),
        total=Sum('total_registered')
    ).order_by('ward__name')

    # Build message - Simple format
    message = f"{today.strftime('%d %b %Y')}\n"
    message += f"✅ All {total_wards} Wards Submitted!\n\n"

    # Per ward breakdown - one line each
    for w in ward_data:
        message += f"{w['ward__name']}: MALE:{w['male']} FEMALE:{w['female']} = {w['total']}\n"

    # Grand totals at the end
    message += f"\nTOTAL: MALE:{total_male} FEMALE:{total_female} = {total_registered}"
    if total_transferred > 0:
        message += f" | Transferred: {total_transferred}"

    return message


def check_and_send_daily_report_from_vra(vra):
    """Check if all wards submitted and send grand total"""
    try:
        from django.db.models import Sum
        from .models import Ward, DailyKIEMSEntry
        from django.contrib.auth.models import User
        from .models import WhatsAppSetting

        today = timezone.now().date()

        # Get total wards
        total_wards = Ward.objects.count()
        if total_wards == 0:
            return

        # Get wards that have submitted today
        submitted_wards = DailyKIEMSEntry.objects.filter(
            entry_date=today
        ).values('ward').distinct().count()

        # If not all wards submitted, return
        if submitted_wards < total_wards:
            return

        # Check if grand total notifications are enabled
        admin_user = User.objects.filter(is_superuser=True).first()
        if admin_user:
            setting = WhatsAppSetting.objects.filter(user=admin_user).first()
            if setting and not setting.notify_grand_total:
                print("Grand total notifications disabled")
                return

        # Get all entries for today
        entries = DailyKIEMSEntry.objects.filter(entry_date=today)

        if not entries.exists():
            return

        # Format and send message
        message = format_grand_total_message(entries, total_wards)
        send_whatsapp_message_from_vra(message, vra)

    except Exception as e:
        print(f"Error checking daily report: {str(e)}")
        import traceback
        traceback.print_exc()

# ==================== ORIGINAL VIEWS ====================

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
    """All kits for the VRA's ward, each with existing entry (if any) for the given date."""
    token = request.GET.get("token")
    date_str = request.GET.get("date")

    if not token:
        return JsonResponse({"error": "No token provided", "kits": []}, status=400)

    vra = VRA.objects.filter(device_token=token, active=True).select_related("ward").first()
    if not vra:
        return JsonResponse({"error": "VRA not found or inactive", "kits": []}, status=404)

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
        "is_today": selected_date == timezone.localdate()
    })


def kiems_entry_view(request):
    wards = Ward.objects.all()
    active_phase = Phase.objects.filter(active=True).first()
    return render(request, "home.html", {"wards": wards, "active_phase": active_phase})


@require_POST
def submit_daily_entries(request):
    """Bulk submit – one row per kit with male/female breakdown."""
    token = request.POST.get("token")
    date_str = request.POST.get("date")

    if not token:
        return JsonResponse({"ok": False, "error": "No token provided"}, status=400)

    vra = get_object_or_404(VRA, device_token=token, active=True)
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

    # ==================== SEND WHATSAPP NOTIFICATIONS ====================
    try:
        # Send notification for each entry created/updated
        for entry in entries_created:
            # Check if this is a new entry or update
            is_update = entry.edit_count > 0

            # Format and send message
            message = format_vra_submission_message(entry, is_update)
            send_whatsapp_message_from_vra(message, vra)
            print(f"WhatsApp: {'Update' if is_update else 'Submission'} sent for {entry.ward.name}")

        # Check if all wards submitted and send grand total
        if entries_created:
            check_and_send_daily_report_from_vra(vra)

    except Exception as e:
        print(f"WhatsApp error in submit: {str(e)}")
        import traceback
        traceback.print_exc()

    if errors:
        return JsonResponse({"ok": False, "errors": errors, "saved": saved}, status=400)

    return JsonResponse({
        "ok": True,
        "saved": saved,
        "message": "Entries submitted successfully!"
    })