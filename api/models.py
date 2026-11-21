import uuid

from django.conf import settings
from django.db import models
from django.db.models import Sum

KIEMS_APP = "home"


def ref(model_name):
    return f"{KIEMS_APP}.{model_name}"


# ============================================================
# IDEMPOTENCY
# ============================================================

class IdempotencyRecord(models.Model):
    key = models.CharField(max_length=100, db_index=True)
    scope = models.CharField(max_length=40, db_index=True)

    fingerprint = models.CharField(max_length=255, blank=True)
    device = models.ForeignKey(
        ref("Device"), on_delete=models.SET_NULL, null=True, blank=True,
        related_name="idempotency_records",
    )
    request_meta = models.JSONField(default=dict, blank=True)

    status_code = models.PositiveSmallIntegerField(default=200)
    response_body = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["key", "scope"],
                name="unique_idempotency_key_per_scope",
            ),
        ]
        indexes = [models.Index(fields=["expires_at"])]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.scope}:{self.key}"


# ============================================================
# KIT BASELINE
# ============================================================

class KitBaseline(models.Model):
    kiems_kit = models.ForeignKey(
        ref("KIEMSKit"), on_delete=models.CASCADE, related_name="baselines",
    )
    phase = models.ForeignKey(
        ref("Phase"), on_delete=models.CASCADE, related_name="kit_baselines",
    )

    baseline_total = models.PositiveIntegerField(default=0)
    baseline_male = models.PositiveIntegerField(default=0)
    baseline_female = models.PositiveIntegerField(default=0)

    source = models.CharField(
        max_length=20,
        choices=[("AUTO", "Auto-seeded"), ("MANUAL", "Manually set")],
        default="AUTO",
    )
    set_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="kit_baselines_set",
    )
    note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["kiems_kit", "phase"],
                name="unique_kit_baseline_per_phase",
            ),
        ]
        # NOTE: ordering by a related field needs a dotted lookup; this is valid
        # *only* once the FK resolves. Kept because it's what we want.
        ordering = ["phase__start_date", "kiems_kit__kit_name"]

    def __str__(self):
        return f"{self.kiems_kit.kit_name} @ {self.phase.name}: {self.baseline_total}"


# ============================================================
# TALLY VERIFICATION
# ============================================================

class TallyVerification(models.Model):
    RESULT_MATCH = "MATCH"
    RESULT_DRIFT = "DRIFT"
    RESULT_BIG_DRIFT = "BIG_DRIFT"
    RESULT_NO_BASELINE = "NO_BASELINE"

    RESULT_CHOICES = [
        (RESULT_MATCH, "Matches our records"),
        (RESULT_DRIFT, "Small drift — likely a typo or missed day"),
        (RESULT_BIG_DRIFT, "Large drift — needs supervisor review"),
        (RESULT_NO_BASELINE, "No baseline yet — first verify"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    kiems_kit = models.ForeignKey(
        ref("KIEMSKit"), on_delete=models.PROTECT, related_name="verifications",
    )
    ward = models.ForeignKey(
        ref("Ward"), on_delete=models.PROTECT, related_name="verifications",
    )
    phase = models.ForeignKey(
        ref("Phase"), on_delete=models.PROTECT, related_name="verifications",
    )
    vra = models.ForeignKey(
        ref("VRA"), on_delete=models.SET_NULL, null=True, blank=True,
        related_name="verifications",
    )
    clerk = models.ForeignKey(
        ref("Clerk"), on_delete=models.SET_NULL, null=True, blank=True,
        related_name="verifications",
    )
    device = models.ForeignKey(
        ref("Device"), on_delete=models.SET_NULL, null=True, blank=True,
        related_name="verifications",
    )

    entry_date = models.DateField(db_index=True)

    observed_total = models.PositiveIntegerField()
    observed_male = models.PositiveIntegerField(null=True, blank=True)
    observed_female = models.PositiveIntegerField(null=True, blank=True)

    expected_total = models.PositiveIntegerField(default=0)
    delta = models.IntegerField(default=0)

    result = models.CharField(max_length=16, choices=RESULT_CHOICES)
    acknowledged = models.BooleanField(default=False)
    note = models.TextField(blank=True)

    client_op_id = models.CharField(max_length=64, blank=True, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["kiems_kit", "-entry_date"]),
            models.Index(fields=["ward", "-entry_date"]),
            models.Index(fields=["result", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.kiems_kit.kit_name} {self.entry_date} → {self.result} (Δ{self.delta:+d})"


# ============================================================
# INCIDENTS
# ============================================================

class Incident(models.Model):
    KIND_CHOICES = [
        ("KIT_FAULT", "KIEMS kit fault"),
        ("KIT_MISSING", "KIEMS kit missing"),
        ("SECURITY", "Security concern"),
        ("NETWORK", "Network / signal issue"),
        ("SUPPLIES", "Missing supplies"),
        ("OTHER", "Other"),
    ]
    SEVERITY_CHOICES = [
        ("LOW", "Low — informational"),
        ("MEDIUM", "Medium — needs attention today"),
        ("HIGH", "High — blocking work"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    severity = models.CharField(max_length=10, choices=SEVERITY_CHOICES, default="MEDIUM")

    ward = models.ForeignKey(
        ref("Ward"), on_delete=models.PROTECT, related_name="incidents",
    )
    constituency = models.ForeignKey(
        ref("Constituency"), on_delete=models.PROTECT,
        related_name="incidents", null=True, blank=True,
    )
    phase = models.ForeignKey(
        ref("Phase"), on_delete=models.PROTECT,
        related_name="incidents", null=True, blank=True,
    )
    kiems_kit = models.ForeignKey(
        ref("KIEMSKit"), on_delete=models.SET_NULL,
        null=True, blank=True, related_name="incidents",
    )
    vra = models.ForeignKey(
        ref("VRA"), on_delete=models.SET_NULL, null=True, blank=True,
        related_name="incidents",
    )
    clerk = models.ForeignKey(
        ref("Clerk"), on_delete=models.SET_NULL, null=True, blank=True,
        related_name="incidents",
    )
    device = models.ForeignKey(
        ref("Device"), on_delete=models.SET_NULL, null=True, blank=True,
        related_name="incidents",
    )

    description = models.TextField(blank=True)

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    accuracy_m = models.FloatField(null=True, blank=True)

    photo = models.FileField(
        upload_to="incidents/%Y/%m/",
        null=True, blank=True,
        help_text="Optional photo evidence. Compressed client-side to webp.",
    )

    client_op_id = models.CharField(max_length=64, blank=True, db_index=True)

    resolved = models.BooleanField(default=False)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="incidents_resolved",
    )
    resolution_note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["ward", "-created_at"]),
            models.Index(fields=["resolved", "-created_at"]),
            models.Index(fields=["severity", "-created_at"]),
        ]

    def __str__(self):
        return f"[{self.severity}] {self.get_kind_display()} — {self.ward.name}"


class Announcement(models.Model):
    """
    Unified announcements feed. Two sources:
      - MANUAL  — created by an ICT officer in Django admin (or a future UI)
      - IEBC    — scraped from iebc.or.ke/media/?news

    Fields that only apply to IEBC-sourced items are nullable.
    """
    SOURCE_CHOICES = [
        ("MANUAL", "Manually posted"),
        ("IEBC", "IEBC website news"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    source = models.CharField(
        max_length=10, choices=SOURCE_CHOICES, default="MANUAL", db_index=True,
    )

    # Visibility scoping (existing)
    constituency = models.ForeignKey(
        ref("Constituency"), on_delete=models.CASCADE,
        null=True, blank=True, related_name="announcements",
    )
    ward = models.ForeignKey(
        ref("Ward"), on_delete=models.CASCADE,
        null=True, blank=True, related_name="announcements",
    )

    title = models.CharField(max_length=300)
    body = models.TextField(blank=True)

    body_html = models.TextField(blank=True)

    priority = models.CharField(
        max_length=10,
        choices=[("NORMAL", "Normal"), ("HIGH", "High")],
        default="NORMAL",
    )

    # --- IEBC-specific ---
    external_url = models.URLField(
        max_length=500, blank=True, db_index=True,
        help_text="Original article URL on iebc.or.ke",
    )
    external_slug = models.CharField(
        max_length=500, blank=True, db_index=True,
        help_text="IEBC URL query slug, used for dedupe",
    )
    image_url = models.URLField(max_length=500, blank=True)
    published_at = models.DateField(
        null=True, blank=True, db_index=True,
        help_text="Publication date (from source). Null = admin post time.",
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="announcements",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    push_dispatched = models.BooleanField(default=False)
    push_dispatched_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-published_at", "-created_at"]
        indexes = [
            models.Index(fields=["constituency", "-created_at"]),
            models.Index(fields=["ward", "-created_at"]),
            models.Index(fields=["source", "-published_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "external_slug"],
                condition=~models.Q(external_slug=""),
                name="uniq_iebc_slug",
            ),
        ]

    def __str__(self):
        scope = self.ward.name if self.ward else (
            self.constituency.name if self.constituency else "GLOBAL"
        )
        return f"[{self.source} · {scope}] {self.title}"


# ============================================================
# PUSH SUBSCRIPTIONS
# ============================================================

class PushSubscription(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    device = models.ForeignKey(
        ref("Device"), on_delete=models.CASCADE, related_name="push_subscriptions",
    )
    fingerprint = models.CharField(max_length=255, db_index=True)

    endpoint = models.URLField(max_length=500, unique=True)
    p256dh = models.CharField(max_length=200)
    auth = models.CharField(max_length=100)

    user_agent = models.TextField(blank=True)

    pref_daily_reminder = models.BooleanField(default=True)
    pref_movement_reminder = models.BooleanField(default=True)
    pref_sync_failure = models.BooleanField(default=True)
    pref_announcements = models.BooleanField(default=True)

    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    last_failure = models.TextField(blank=True)
    failure_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.fingerprint[:12]}… {self.endpoint[:40]}…"


# ============================================================
# SYNC EVENTS
# ============================================================

class SyncEvent(models.Model):
    fingerprint = models.CharField(max_length=255, db_index=True)
    sent = models.PositiveIntegerField(default=0)
    failed = models.PositiveIntegerField(default=0)
    still_queued = models.PositiveIntegerField(default=0)
    duration_ms = models.PositiveIntegerField(default=0)
    app_version = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["fingerprint", "-created_at"])]


class VoterVerification(models.Model):
    """
    Audit trail for every voter verification from a VRA device.
    Required for Kenya Data Protection Act compliance:
    must be able to prove who looked up which ID and why.
    """
    RESULT_CHOICES = [
        ("OK", "Found"),
        ("NOT_FOUND", "Not found"),
        ("CACHED", "Served from cache"),
        ("NETWORK_ERROR", "Network error to IEBC"),
        ("HTTP_ERROR", "IEBC returned non-200"),
        ("PARSE_ERROR", "Could not parse IEBC response"),
        ("DISABLED", "Feature disabled on server"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    vra = models.ForeignKey(
        ref("VRA"), on_delete=models.SET_NULL,
        null=True, blank=True, related_name="voter_verifications",
    )
    device = models.ForeignKey(
        ref("Device"), on_delete=models.SET_NULL,
        null=True, blank=True, related_name="voter_verifications",
    )
    id_number = models.CharField(max_length=20, db_index=True)
    year_of_birth = models.PositiveIntegerField()
    result = models.CharField(max_length=20, choices=RESULT_CHOICES)
    payload = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    duration_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["vra", "-created_at"]),
            models.Index(fields=["id_number", "-created_at"]),
            models.Index(fields=["result", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.id_number} {self.year_of_birth} → {self.result}"


class FormAReceipt(models.Model):
    """A receipt of materials at the office.

    FORM_A  : loose forms. `form_count` is authoritative.
              Serials optional (many batches arrive unserialed).
    ACK     : serialed booklets of 50. `booklet_count * 50 = form_count`.
              Serial range REQUIRED.
    """
    FORM_A = "FORM_A"
    ACK = "ACKNOWLEDGMENT"
    TYPE_CHOICES = [
        (FORM_A, "Form A (loose forms)"),
        (ACK, "Acknowledgment booklet"),
    ]
    ACK_FORMS_PER_BOOKLET = 50

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    form_type = models.CharField(max_length=20, choices=TYPE_CHOICES, db_index=True)

    phase = models.ForeignKey(ref("Phase"), on_delete=models.PROTECT, related_name="form_a_receipts")
    ward = models.ForeignKey(ref("Ward"), on_delete=models.PROTECT, related_name="form_a_receipts")

    form_count = models.PositiveIntegerField(help_text="Total forms in this receipt")
    is_archived = models.BooleanField(default=False, db_index=True)

    booklet_count = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="ACK only. form_count must equal booklet_count * 50.",
    )

    serial_start = models.CharField(max_length=30, blank=True)
    serial_end = models.CharField(max_length=30, blank=True)

    received_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="form_a_received",
    )
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    note = models.TextField(blank=True)
    client_op_id = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ["-received_at"]
        indexes = [models.Index(fields=["ward", "-received_at"])]

    def __str__(self):
        return (
            f"{self.get_form_type_display()} · {self.ward.name} · "
            f"{self.form_count} forms "
            f"({self.serial_start or '—'}–{self.serial_end or '—'})"
        )

    @property
    def used_count(self):
        return (
                self.usages.filter(kind=FormAUsage.KIND_USED)
                .aggregate(s=Sum("forms_used"))["s"] or 0
        )

    @property
    def spoiled_count(self):
        serial_spoiled = self.spoilages.count()
        count_spoiled = (
                self.usages.filter(kind=FormAUsage.KIND_SPOILED)
                .aggregate(s=Sum("forms_used"))["s"] or 0
        )
        return int(serial_spoiled) + int(count_spoiled)

    @property
    def remaining(self):
        return self.form_count - int(self.used_count) - int(self.spoiled_count)

    @property
    def allocated_count(self):
        return (
            self.allocations.aggregate(s=Sum("forms_allocated"))["s"] or 0
        )

    @property
    def unallocated(self):
        return self.form_count - int(self.allocated_count) - int(self.spoiled_count)


class FormAUsage(models.Model):
    """Forms consumed on a given day. Works for both receipt types.

    kind=USED     : normal spending
    kind=SPOILED  : loose Form A spoilage count (ACK spoilage is per-serial
                    in FormASpoilage instead)
    """
    KIND_USED = "USED"
    KIND_SPOILED = "SPOILED"
    KIND_CHOICES = [
        (KIND_USED, "Used"),
        (KIND_SPOILED, "Spoiled (count only)"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    receipt = models.ForeignKey(FormAReceipt, on_delete=models.PROTECT, related_name="usages")

    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default=KIND_USED, db_index=True)
    spoil_reason = models.CharField(max_length=30, blank=True)

    phase = models.ForeignKey(ref("Phase"), on_delete=models.PROTECT)
    ward = models.ForeignKey(ref("Ward"), on_delete=models.PROTECT)
    kiems_kit = models.ForeignKey(ref("KIEMSKit"), on_delete=models.SET_NULL, null=True, blank=True)
    vra = models.ForeignKey(ref("VRA"), on_delete=models.SET_NULL, null=True, blank=True)
    device = models.ForeignKey(ref("Device"), on_delete=models.SET_NULL, null=True, blank=True)

    entry_date = models.DateField(db_index=True)
    forms_used = models.PositiveIntegerField()
    note = models.TextField(blank=True)

    client_op_id = models.CharField(max_length=64, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-entry_date", "-created_at"]
        indexes = [
            models.Index(fields=["ward", "-entry_date"]),
            models.Index(fields=["receipt", "-entry_date"]),
            models.Index(fields=["kind", "-entry_date"]),
        ]


class FormASpoilage(models.Model):
    """One spoiled form with its serial number. ACK receipts only."""
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    receipt = models.ForeignKey(FormAReceipt, on_delete=models.PROTECT, related_name="spoilages")
    phase = models.ForeignKey(ref("Phase"), on_delete=models.PROTECT)
    ward = models.ForeignKey(ref("Ward"), on_delete=models.PROTECT)
    vra = models.ForeignKey(ref("VRA"), on_delete=models.SET_NULL, null=True, blank=True)
    device = models.ForeignKey(ref("Device"), on_delete=models.SET_NULL, null=True, blank=True)

    serial_number = models.CharField(max_length=30, db_index=True, blank=True, null=True)
    kiems_kit = models.ForeignKey(  # ← ADD THIS
        ref("KIEMSKit"), on_delete=models.SET_NULL, null=True, blank=True,
        related_name="form_a_spoilages",
    )
    entry_date = models.DateField(db_index=True)
    reason = models.CharField(
        max_length=30,
        choices=[
            ("TORN", "Torn"),
            ("MISPRINT", "Misprinter"),
            ("WRONG_ENTRY", "Wrong entry"),
            ("WATER_DAMAGE", "Water damage"),
            ("OTHER", "Other"),
        ],
        default="OTHER",
    )
    note = models.TextField(blank=True)
    client_op_id = models.CharField(max_length=64, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-entry_date", "-created_at"]
        indexes = [models.Index(fields=["ward", "-entry_date"])]
        constraints = [
            models.UniqueConstraint(
                fields=["receipt", "serial_number"],
                name="uniq_serial_spoiled_per_receipt",
            ),
        ]


class FormAAllocation(models.Model):
    """A VRA distributes booklets from a ward receipt to specific kits.

    One receipt may be split across many kits. Count is in forms, not
    booklets, but for ACK receipts it must be a multiple of 50.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    receipt = models.ForeignKey(
        FormAReceipt, on_delete=models.CASCADE, related_name="allocations",
    )
    kiems_kit = models.ForeignKey(
        ref("KIEMSKit"), on_delete=models.PROTECT, related_name="form_a_allocations",
    )
    vra = models.ForeignKey(
        ref("VRA"), on_delete=models.SET_NULL, null=True, blank=True,
        related_name="form_a_allocations",
    )
    device = models.ForeignKey(
        ref("Device"), on_delete=models.SET_NULL, null=True, blank=True,
        related_name="form_a_allocations",
    )

    forms_allocated = models.PositiveIntegerField(
        help_text="Number of forms allocated to this kit from this receipt",
    )
    note = models.TextField(blank=True)

    client_op_id = models.CharField(max_length=64, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["receipt", "-created_at"]),
            models.Index(fields=["kiems_kit", "-created_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["receipt", "kiems_kit"],
                name="uniq_allocation_per_kit_receipt",
            ),
        ]

    def __str__(self):
        return (
            f"{self.forms_allocated} forms of "
            f"{self.receipt.get_form_type_display()} "
            f"→ {self.kiems_kit.kit_name}"
        )
