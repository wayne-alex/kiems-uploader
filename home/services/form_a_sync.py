# home/services/form_a_sync.py
"""
Reconcile a DailyKIEMSEntry against Form A consumption.

Called on every entry submit / edit. The contract:

    sync_form_a_for_entry(entry, previous_total, acting_vra=None)

`previous_total` is what the entry counted *before* this call
(0 for a brand-new entry). The function computes the delta and
writes / reverses FormAUsage rows to match.

Rules:
  1. Prefer a kit-level allocation on any active receipt in the ward.
  2. Otherwise fall back to the ward's oldest non-exhausted receipt
     and use it as a ward-level pool.
  3. If no forms are available, no exception is raised — the caller
     can inspect the return value and surface a warning.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Sum

from api.models import FormAUsage, FormAAllocation, FormAReceipt
from home.models import DailyKIEMSEntry

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    ok: bool
    delta: int
    receipt_id: str | None
    kit_id: int | None
    warning: str = ""
    allocated_from_kit: bool = False  # True if from kit alloc, False if ward pool


def _forms_used_on_receipt(receipt) -> int:
    return (
            receipt.usages.filter(kind=FormAUsage.KIND_USED)
            .aggregate(s=Sum("forms_used"))["s"] or 0
    )


def _forms_spoiled_on_receipt(receipt) -> int:
    return receipt.spoiled_count


def _receipt_available(receipt) -> int:
    """Forms still available on this receipt (regardless of allocation)."""
    return receipt.form_count - int(_forms_used_on_receipt(receipt)) - int(_forms_spoiled_on_receipt(receipt))


def _find_kit_allocation(kit, phase, entry_date):
    """The active allocation for this kit, preferring the most recent."""
    return (
        FormAAllocation.objects
        .filter(
            kiems_kit=kit,
            receipt__phase=phase,
            receipt__ward_id=kit.ward_id,
            receipt__is_archived=False,
            receipt__received_at__date__lte=entry_date,
        )
        .select_related("receipt")
        .order_by("-receipt__received_at")
        .first()
    )


def _find_ward_receipt(ward, phase, entry_date):
    """Oldest non-exhausted receipt in the ward — the fallback pool."""
    candidates = (
        FormAReceipt.objects
        .filter(
            ward=ward,
            phase=phase,
            is_archived=False,
            received_at__date__lte=entry_date,
        )
        .order_by("received_at")
    )
    for r in candidates:
        if _receipt_available(r) > 0:
            return r
    return None


def _record_usage(receipt, kit, vra, entry_date, forms, note):
    """Create a single usage row for the delta."""
    return FormAUsage.objects.create(
        receipt=receipt,
        kind=FormAUsage.KIND_USED,
        phase=receipt.phase,
        ward=receipt.ward,
        kiems_kit=kit,
        vra=vra,
        entry_date=entry_date,
        forms_used=forms,
        note=note,
    )


def _reverse_usage_for_delta(kit, entry_date, delta, vra, note):
    """
    Remove `delta` worth of forms from the usage rows that were created for
    this (kit, entry_date, vra). Deletes oldest rows first, spills into the
    next row if a single row is smaller than delta.
    """
    if delta <= 0:
        return

    qs = (
        FormAUsage.objects
        .filter(
            kiems_kit=kit,
            entry_date=entry_date,
            vra=vra,
            kind=FormAUsage.KIND_USED,
        )
        .order_by("created_at")
    )

    remaining = delta
    for usage in qs:
        if remaining <= 0:
            break
        take = min(usage.forms_used, remaining)
        if take == usage.forms_used:
            usage.delete()
        else:
            usage.forms_used = usage.forms_used - take
            usage.save(update_fields=["forms_used"])
        remaining -= take

    if remaining > 0:
        logger.warning(
            "form_a_sync: could not fully reverse %d forms for kit=%s date=%s vra=%s",
            remaining, kit.id, entry_date, getattr(vra, "id", None),
        )


@transaction.atomic
def sync_form_a_for_entry(entry: DailyKIEMSEntry, previous_total: int, acting_vra=None) -> SyncResult:
    """
    Reconcile the entry's registration count against Form A consumption.

    previous_total: what this entry counted *before* this save (0 for new).
    acting_vra: the VRA whose action triggered this, if any. Falls back to
                entry.vra. Used to tag FormAUsage rows for audit.
    """
    new_total = int(entry.total_registered or 0)
    delta = new_total - int(previous_total or 0)

    if delta == 0:
        return SyncResult(
            ok=True, delta=0, receipt_id=None, kit_id=None,
            warning="", allocated_from_kit=False,
        )

    vra = acting_vra or entry.vra

    # Determine the target receipt
    allocation = _find_kit_allocation(entry.kiems_kit, entry.phase, entry.entry_date)
    receipt = None
    allocated_from_kit = False

    if allocation:
        receipt = allocation.receipt
        allocated_from_kit = True
    else:
        receipt = _find_ward_receipt(entry.ward, entry.phase, entry.entry_date)

    if receipt is None:
        logger.warning(
            "form_a_sync: no receipt available for kit=%s ward=%s date=%s",
            entry.kiems_kit_id, entry.ward_id, entry.entry_date,
        )
        return SyncResult(
            ok=False, delta=delta, receipt_id=None, kit_id=entry.kiems_kit_id,
            warning="No Form A receipt available for this ward/phase.",
            allocated_from_kit=False,
        )

    # ── Positive delta: consume forms ──
    if delta > 0:
        available = _receipt_available(receipt)
        if delta > available:
            # Partial record — record what's available, warn about the gap
            note = (
                f"[PARTIAL] Entry registered {delta} but only {available} "
                f"forms left on receipt. {delta - available} unreconciled."
            )
            if available > 0:
                _record_usage(receipt, entry.kiems_kit, vra, entry.entry_date, available, note)
            return SyncResult(
                ok=False, delta=delta, receipt_id=str(receipt.id),
                kit_id=entry.kiems_kit_id, warning=note,
                allocated_from_kit=allocated_from_kit,
            )

        note = (
                f"Kit entry {entry.kiems_kit.kit_name} · {delta} forms"
                + ("" if allocated_from_kit else " (ward pool)")
        )
        _record_usage(receipt, entry.kiems_kit, vra, entry.entry_date, delta, note)
        return SyncResult(
            ok=True, delta=delta, receipt_id=str(receipt.id),
            kit_id=entry.kiems_kit_id, warning="",
            allocated_from_kit=allocated_from_kit,
        )

    # ── Negative delta (edit reduced the count): reverse the difference ──
    _reverse_usage_for_delta(entry.kiems_kit, entry.entry_date, -delta, vra, "edit reversal")
    return SyncResult(
        ok=True, delta=delta, receipt_id=str(receipt.id),
        kit_id=entry.kiems_kit_id, warning="",
        allocated_from_kit=allocated_from_kit,
    )
