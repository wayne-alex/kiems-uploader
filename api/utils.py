import json
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import IdempotencyRecord


class IdempotencyConflict(Exception):
    """Raised when the same key is reused with a different payload."""
    pass


def with_idempotency(request, scope, fn):
    """
    Run `fn()` exactly once per Idempotency-Key (scoped).
    On replay, returns (status_code, body, is_replay=True).
    """
    key = request.headers.get("Idempotency-Key") or request.META.get("HTTP_IDEMPOTENCY_KEY")
    if not key:
        # No key → just run (best-effort path, still correct)
        status, body = fn()
        return status, body, False

    key = key.strip()[:100]

    existing = IdempotencyRecord.objects.filter(key=key, scope=scope).first()
    if existing:
        return existing.status_code, existing.response_body, True

    # Guard against concurrent double-arrivals: unique constraint will
    # reject a second insert from another worker.
    try:
        with transaction.atomic():
            status, body = fn()
            IdempotencyRecord.objects.create(
                key=key,
                scope=scope,
                fingerprint=getattr(request, "fingerprint", "") or "",
                status_code=status,
                response_body=body,
                request_meta={
                    "path": request.path,
                    "method": request.method,
                },
                expires_at=timezone.now() + timedelta(days=7),
            )
        return status, body, False
    except Exception as exc:
        # If the insert failed only because another worker got there first,
        # replay that worker's record.
        again = IdempotencyRecord.objects.filter(key=key, scope=scope).first()
        if again:
            return again.status_code, again.response_body, True
        raise exc