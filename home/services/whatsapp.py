# home/services/whatsapp.py

import requests
from django.conf import settings
from django.db.models import Q

from home.models import WhatsAppGroup, WhatsAppSetting


def _bot_url():
    return getattr(settings, "WHATSAPP_BOT_URL", "http://localhost:3000")


def get_default_group(constituency=None, user=None):
    """
    Resolve which WhatsApp group reports should go to.
    Priority:
      1. user's explicitly selected group
      2. constituency-scoped active group
      3. global active group
    """
    if user:
        setting = WhatsAppSetting.objects.filter(user=user).first()
        if setting and setting.default_group and setting.default_group.is_active:
            return setting.default_group.group_id

    if constituency:
        g = WhatsAppGroup.objects.filter(
            constituency=constituency, is_active=True
        ).first()
        if g:
            return g.group_id

    g = WhatsAppGroup.objects.filter(
        Q(constituency__isnull=True), is_active=True
    ).first()
    return g.group_id if g else None


def send_whatsapp_to_group(group_id, message, timeout=15):
    """
    Returns (ok: bool, error: str|None). Never raises.
    """
    if not group_id:
        return False, "No group id"

    try:
        r = requests.post(
            f"{_bot_url()}/send",
            json={"groupId": group_id, "message": message},
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
    except requests.exceptions.ConnectionError:
        return False, "Bot offline"
    except requests.exceptions.Timeout:
        return False, "Bot timed out"
    except Exception as e:
        return False, f"Network error: {e}"

    if r.status_code != 200:
        return False, f"Bot {r.status_code}: {r.text[:200]}"
    return True, None