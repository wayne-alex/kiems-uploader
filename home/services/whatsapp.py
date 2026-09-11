import requests
from django.conf import settings

from home.models import WhatsAppGroup

def get_group_for_constituency(constituency):
    if constituency is None:
        return None

    # 1. Active, scoped to this constituency
    group = (
        WhatsAppGroup.objects
        .filter(constituency=constituency, is_active=True)
        .order_by("name")
        .first()
    )
    if group:
        return group

    # 2. Active, global
    return (
        WhatsAppGroup.objects
        .filter(constituency__isnull=True, is_active=True)
        .order_by("name")
        .first()
    )


def send_to_constituency(constituency, message, timeout=15):
    """
    Send a message to the group that belongs to a given constituency.
    Returns (ok: bool, error: str|None, group_id: str|None).
    Never raises.
    """
    group = get_group_for_constituency(constituency)
    if not group:
        return False, "No WhatsApp group configured for this constituency", None

    bot_url = getattr(settings, "WHATSAPP_BOT_URL", "http://localhost:3000")
    try:
        r = requests.post(
            f"{bot_url}/send",
            json={"groupId": group.group_id, "message": message},
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
    except requests.exceptions.ConnectionError:
        return False, "Bot offline", group.group_id
    except requests.exceptions.Timeout:
        return False, "Bot timed out", group.group_id
    except Exception as e:
        return False, f"Network error: {e}", group.group_id

    if r.status_code != 200:
        return False, f"Bot {r.status_code}: {r.text[:200]}", group.group_id

    return True, None, group.group_id
