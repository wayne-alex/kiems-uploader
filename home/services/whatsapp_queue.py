"""
Non-blocking WhatsApp send queue.

Usage from request handlers:
    enqueue_whatsapp(constituency, message, kind="entry.submit")

Usage from cron / management command:
    summary = drain_whatsapp_queue(limit=50)
"""
import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

from home.models import OutgoingWhatsAppMessage
from home.services.whatsapp import get_group_for_constituency

logger = logging.getLogger(__name__)

BOT_URL = lambda: getattr(settings, "WHATSAPP_BOT_URL", "http://localhost:3000")
MAX_ATTEMPTS = 5
LOCK_TIMEOUT_SECONDS = 90
BOT_SEND_TIMEOUT = 8   # short — we're already off the hot path
BACKOFF_SECONDS = [30, 120, 600, 3600, 14400]  # exponential-ish


def enqueue_whatsapp(constituency, message, kind=""):
    """Enqueue a message. Returns the row or None if group is missing.

    This is intentionally fast: one INSERT, no network.
    """
    if not constituency:
        return None

    group = get_group_for_constituency(constituency)
    if not group:
        logger.warning(
            "enqueue_whatsapp: no group for %s — dropping message",
            constituency.name if hasattr(constituency, "name") else constituency,
        )
        return None

    return OutgoingWhatsAppMessage.objects.create(
        constituency=constituency,
        group_id=group.group_id,
        message=message,
        kind=kind,
        status=OutgoingWhatsAppMessage.STATUS_QUEUED,
    )


def _claim_pending(limit=25):
    """Claim QUEUED (or FAILED-with-due-time) messages by marking them SENDING."""
    now = timezone.now()
    stale = now - timedelta(seconds=LOCK_TIMEOUT_SECONDS)

    # Release any SENDING rows whose lock has expired (probably died mid-send)
    OutgoingWhatsAppMessage.objects.filter(
        status=OutgoingWhatsAppMessage.STATUS_SENDING,
        locked_at__lt=stale,
    ).update(
        status=OutgoingWhatsAppMessage.STATUS_QUEUED,
        locked_at=None,
    )

    qs = (
        OutgoingWhatsAppMessage.objects
        .filter(status__in=[
            OutgoingWhatsAppMessage.STATUS_QUEUED,
            OutgoingWhatsAppMessage.STATUS_FAILED,
        ])
        .filter(
            models_q_due(now)
        )
        .order_by("created_at")[:limit]
    )

    claimed = []
    for msg in qs:
        updated = OutgoingWhatsAppMessage.objects.filter(
            pk=msg.pk,
            status=msg.status,
        ).update(
            status=OutgoingWhatsAppMessage.STATUS_SENDING,
            locked_at=now,
            attempts=msg.attempts + 1,
        )
        if updated:
            msg.refresh_from_db()
            claimed.append(msg)
    return claimed


def models_q_due(now):
    """Helper: (status=QUEUED) OR (status=FAILED and next_attempt_at due)."""
    from django.db.models import Q
    return (
        Q(status=OutgoingWhatsAppMessage.STATUS_QUEUED)
        | Q(status=OutgoingWhatsAppMessage.STATUS_FAILED,
            next_attempt_at__lte=now)
        | Q(status=OutgoingWhatsAppMessage.STATUS_FAILED,
            next_attempt_at__isnull=True)
    )


def _send_one(msg):
    """Try a single send. Returns (ok, error)."""
    try:
        r = requests.post(
            f"{BOT_URL()}/send",
            json={"groupId": msg.group_id, "message": msg.message},
            headers={"Content-Type": "application/json"},
            timeout=BOT_SEND_TIMEOUT,
        )
    except requests.exceptions.ConnectionError:
        return False, "bot offline (connection refused)"
    except requests.exceptions.Timeout:
        return False, f"bot timeout after {BOT_SEND_TIMEOUT}s"
    except requests.RequestException as exc:
        return False, f"network error: {exc}"

    if r.status_code == 200:
        return True, ""

    return False, f"bot HTTP {r.status_code}: {r.text[:160]}"


def drain_whatsapp_queue(limit=25):
    """Send up to `limit` queued messages. Call from cron or a per-request
    background thread. Returns a summary dict."""
    claimed = _claim_pending(limit=limit)
    sent = failed = gave_up = 0
    now = timezone.now()

    for msg in claimed:
        ok, err = _send_one(msg)

        if ok:
            msg.status = OutgoingWhatsAppMessage.STATUS_SENT
            msg.sent_at = now
            msg.last_error = ""
            msg.save(update_fields=["status", "sent_at", "last_error"])
            sent += 1
            continue

        msg.last_error = err[:500]

        if msg.attempts >= MAX_ATTEMPTS:
            msg.status = OutgoingWhatsAppMessage.STATUS_GAVE_UP
            msg.save(update_fields=["status", "last_error"])
            gave_up += 1
            logger.warning(
                "WhatsApp message %s gave up after %d attempts: %s",
                msg.id, msg.attempts, err,
            )
            continue

        backoff_idx = min(msg.attempts - 1, len(BACKOFF_SECONDS) - 1)
        next_try = now + timedelta(seconds=BACKOFF_SECONDS[backoff_idx])
        msg.status = OutgoingWhatsAppMessage.STATUS_FAILED
        msg.next_attempt_at = next_try
        msg.save(update_fields=["status", "last_error", "next_attempt_at"])
        failed += 1

    return {
        "claimed": len(claimed),
        "sent": sent,
        "failed": failed,
        "gave_up": gave_up,
    }


def drain_async(limit=15):
    """Fire-and-forget drain in a background thread.

    Called after enqueue to try to send *right now* if the bot is online.
    The VRA's HTTP request returns immediately; this thread does the work.

    Don't call this from cron — cron should call drain_whatsapp_queue()
    synchronously so failures are visible in the response.
    """
    import threading

    def _run():
        try:
            drain_whatsapp_queue(limit=limit)
        except Exception:
            logger.exception("drain_async: uncaught error")

    t = threading.Thread(target=_run, daemon=True, name="whatsapp-drain")
    t.start()