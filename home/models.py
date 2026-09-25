import uuid

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

class County(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=20, blank=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Counties"

    def __str__(self):
        return self.name
class Constituency(models.Model):
    name = models.CharField(max_length=150, unique=True)
    county = models.ForeignKey(
        County,
        on_delete=models.PROTECT,
        related_name="constituencies",
        null=True, blank=True,
    )
    code = models.CharField(max_length=20, unique=True, blank=True, null=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "Constituencies"

    def __str__(self):
        return self.name


class Ward(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=20, unique=True, blank=True, null=True)
    active = models.BooleanField(default=True)
    constituency = models.ForeignKey(
        Constituency,
        on_delete=models.PROTECT,
        related_name="wards",
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class VRA(models.Model):
    """
    Voter Registration Assistant – assigned to one ward.
    """
    name = models.CharField(max_length=150)
    ward = models.ForeignKey(Ward, on_delete=models.PROTECT, related_name="vras")
    active = models.BooleanField(default=True)

    device_token = models.CharField(max_length=64, unique=True, db_index=True, blank=True, null=True)
    device_fingerprint = models.CharField(max_length=64, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "VRA"
        verbose_name_plural = "VRAs"
        ordering = ["ward__name", "name"]

    def __str__(self):
        return f"{self.name} ({self.ward.name})"


class Clerk(models.Model):
    name = models.CharField(max_length=150)
    ward = models.ForeignKey(Ward, on_delete=models.PROTECT, related_name="clerks")
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    # Device association for clerk (optional)
    device_token = models.CharField(max_length=64, unique=True, db_index=True, blank=True, null=True)
    device_fingerprint = models.CharField(max_length=64, blank=True)

    class Meta:
        ordering = ["ward__name", "name"]

    def __str__(self):
        return f"{self.name} ({self.ward.name})"


class KIEMSKit(models.Model):
    kit_name = models.CharField(max_length=50)
    serial_no = models.CharField(max_length=100, unique=True)
    status = models.BooleanField(default=True)
    ward = models.ForeignKey(Ward, on_delete=models.PROTECT, related_name="kits")
    assigned_clerks = models.ManyToManyField(Clerk, related_name="kits", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["ward__name", "kit_name"]
        indexes = [models.Index(fields=["ward", "status"])]

    def __str__(self):
        return f"{self.kit_name} – {self.serial_no}"


class Phase(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    start_date = models.DateField()
    end_date = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return self.name


class DailyKIEMSEntry(models.Model):
    ENTRY_TYPES = [
        ('VENUE', 'Venue Mapping Only'),
        ('REGISTRATION', 'Registration Entry'),
    ]

    entry_type = models.CharField(
        max_length=20,
        choices=ENTRY_TYPES,
        default='REGISTRATION'
    )
    kiems_kit = models.ForeignKey(KIEMSKit, on_delete=models.PROTECT, related_name="daily_entries")
    phase = models.ForeignKey(Phase, on_delete=models.PROTECT, related_name="daily_entries")
    ward = models.ForeignKey(Ward, on_delete=models.PROTECT, related_name="daily_entries")
    vra = models.ForeignKey(VRA, on_delete=models.PROTECT, related_name="daily_entries")
    clerk = models.ForeignKey(Clerk, on_delete=models.PROTECT, related_name="daily_entries", null=True, blank=True)

    entry_date = models.DateField()
    venue = models.CharField(max_length=150)

    # Gender breakdown - Male & Female only
    registered_male = models.PositiveIntegerField(default=0)
    registered_female = models.PositiveIntegerField(default=0)
    total_registered = models.PositiveIntegerField(default=0)

    # Office-only fields
    total_transferred = models.PositiveIntegerField(default=0)
    total_updated = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    edit_count = models.PositiveIntegerField(default=0)
    uploaded = models.BooleanField(default=False)

    office_updated_by = models.CharField(max_length=150, blank=True)
    office_updated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-entry_date", "ward__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["kiems_kit", "phase", "entry_date", "vra"],
                name="unique_kit_phase_day_vra",
            )
        ]
        indexes = [
            models.Index(fields=["ward", "entry_date"]),
            models.Index(fields=["phase", "entry_date"]),
        ]

    def save(self, *args, **kwargs):
        # Always recalculate total from male + female
        self.total_registered = self.registered_male + self.registered_female
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.kiems_kit.kit_name} - {self.entry_date}"


class MovementSchedule(models.Model):
    """
    Tomorrow's movement plan: for each KIEMS kit in a ward, where will it be
    stationed tomorrow? One row per (kit, schedule_date). Populated from the
    client side by VRAs/Clerks, and viewable/editable by the ICT officer.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    kiems_kit = models.ForeignKey(
        KIEMSKit, on_delete=models.PROTECT, related_name="movement_schedules"
    )
    ward = models.ForeignKey(
        Ward, on_delete=models.PROTECT, related_name="movement_schedules"
    )
    constituency = models.ForeignKey(
        Constituency, on_delete=models.PROTECT, related_name="movement_schedules"
    )
    phase = models.ForeignKey(
        Phase, on_delete=models.PROTECT, related_name="movement_schedules"
    )

    # Who submitted it (either one will be set)
    vra = models.ForeignKey(
        VRA, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="movement_schedules",
    )
    clerk = models.ForeignKey(
        Clerk, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="movement_schedules",
    )

    schedule_date = models.DateField(db_index=True)  # always "tomorrow" at creation
    venue = models.CharField(max_length=200)
    notes = models.CharField(max_length=255, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    edit_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-schedule_date", "ward__name", "kiems_kit__kit_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["kiems_kit", "schedule_date"],
                name="unique_kit_movement_per_day",
            ),
        ]
        indexes = [
            models.Index(fields=["constituency", "schedule_date"]),
            models.Index(fields=["ward", "schedule_date"]),
        ]

    def __str__(self):
        return f"{self.kiems_kit.kit_name} @ {self.venue} on {self.schedule_date}"


class MovementScheduleState(models.Model):
    """
    One row per (constituency, schedule_date). Tracks whether all wards have
    submitted their movement plan for that date, so the grand report is sent
    exactly once.
    """
    STATUS_CHOICES = [
        ("PENDING", "Pending — wards still submitting"),
        ("READY", "Ready — all wards in"),
        ("SENDING", "Sending — locked by a worker"),
        ("SENT", "Sent"),
        ("FAILED", "Failed — will retry"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    constituency = models.ForeignKey(
        Constituency, on_delete=models.CASCADE,
        related_name="movement_schedule_states",
    )
    schedule_date = models.DateField(db_index=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="PENDING")

    total_wards = models.PositiveIntegerField(default=0)
    submitted_wards = models.PositiveIntegerField(default=0)

    ready_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    message_hash = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["constituency", "schedule_date"],
                name="unique_movement_schedule_per_constituency_day",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "schedule_date"]),
        ]
        ordering = ["-schedule_date"]

    def __str__(self):
        return f"{self.constituency.name} {self.schedule_date} — {self.status}"


class MovementScheduleLog(models.Model):
    """
    Audit + delivery log for movement-schedule WhatsApp messages,
    whether auto-sent (per-kit or grand) or manually sent by the ICT officer.
    """
    KIND_CHOICES = [
        ("PER_KIT", "Per-kit submission"),
        ("GRAND", "Grand report (all wards)"),
        ("MANUAL", "Manual send by ICT officer"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    constituency = models.ForeignKey(
        Constituency, on_delete=models.CASCADE, related_name="movement_schedule_logs"
    )
    schedule_date = models.DateField(db_index=True)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)

    ward = models.ForeignKey(
        Ward, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="movement_schedule_logs",
    )
    kiems_kit = models.ForeignKey(
        KIEMSKit, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="movement_schedule_logs",
    )

    group_id = models.CharField(max_length=100, blank=True)
    group_name = models.CharField(max_length=200, blank=True)
    message = models.TextField()
    sent_ok = models.BooleanField(default=False)
    error = models.TextField(blank=True)

    sent_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="movement_schedule_logs",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["constituency", "-created_at"]),
            models.Index(fields=["schedule_date", "kind"]),
        ]

    def __str__(self):
        return f"{self.get_kind_display()} - {self.schedule_date} - {'OK' if self.sent_ok else 'FAIL'}"


# ==================== WHATSAPP MODELS ====================

class WhatsAppGroup(models.Model):
    """
    WhatsApp group. Every group is scoped to exactly one constituency,
    or explicitly marked as global (constituency=NULL) for cross-cutting
    broadcasts (e.g. a national ops channel).
    """
    group_id = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)

    constituency = models.ForeignKey(
        "Constituency",
        on_delete=models.CASCADE,
        related_name="whatsapp_groups",
        null=True,
        blank=True,
        help_text=(
            "Constituency this group belongs to. "
            "Leave blank ONLY for genuinely global groups "
            "(national ops, superadmin broadcasts)."
        ),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['constituency__name', 'name']
        constraints = [
            # A constituency cannot have two groups with the same name.
            # (Group IDs are globally unique already.)
            models.UniqueConstraint(
                fields=["constituency", "name"],
                name="unique_group_name_per_constituency",
            ),
        ]

    def __str__(self):
        scope = self.constituency.name if self.constituency else "GLOBAL"
        return f"[{scope}] {self.name}"


class WhatsAppSetting(models.Model):
    """User settings for WhatsApp"""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='whatsapp_settings')
    default_group = models.ForeignKey(WhatsAppGroup, on_delete=models.SET_NULL, null=True, blank=True)

    # Notification toggles
    notify_vra = models.BooleanField(default=True, help_text="Send notification when VRA submits")
    notify_edit = models.BooleanField(default=True, help_text="Send notification when VRA edits")
    notify_daily = models.BooleanField(default=True, help_text="Send daily report")
    notify_grand_total = models.BooleanField(default=True, help_text="Send grand total when all wards submit")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - WhatsApp Settings"

    class Meta:
        ordering = ['user__username']


class Device(models.Model):
    """Registered devices for KIEMS system"""
    fingerprint = models.CharField(max_length=255, unique=True, db_index=True)
    user_agent = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    # Device metadata
    screen_resolution = models.CharField(max_length=50, blank=True)
    language = models.CharField(max_length=10, blank=True)
    platform = models.CharField(max_length=50, blank=True)
    timezone = models.CharField(max_length=50, blank=True)

    # Status
    # is_burned=True  → device is AUTHORIZED (burned-in token granted)
    # is_burned=False → device is NOT AUTHORIZED
    is_burned = models.BooleanField(
        default=False,
        help_text="True = authorized (burned-in). False = not authorized.",
    )
    is_active = models.BooleanField(default=True)
    burn_date = models.DateTimeField(
        null=True, blank=True,
        help_text="When the device was authorized (burned-in).",
    )
    burn_notes = models.TextField(
        blank=True,
        help_text="Notes recorded at authorization time.",
    )

    # Associated VRA
    vra = models.ForeignKey(
        'VRA', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='devices',
    )
    # Associated Clerk
    clerk = models.ForeignKey(
        'Clerk', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='devices',
    )

    # Timestamps
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-last_seen']
        indexes = [
            models.Index(fields=['fingerprint']),
            models.Index(fields=['is_burned', 'is_active']),
        ]

    def __str__(self):
        status = "Authorized" if self.is_burned else "Not Authorized"
        return f"{self.fingerprint[:12]}... ({status})"

    @property
    def is_authorized(self):
        """Readable alias for templates / views."""
        return self.is_burned


class DeviceBurnLog(models.Model):
    """Audit log of device authorization / revocation operations."""
    device = models.ForeignKey(
        Device, on_delete=models.CASCADE, related_name='burn_logs'
    )
    action = models.CharField(max_length=20, choices=[
        # New, semantically-correct actions
        ("AUTHORIZE", "Authorized"),
        ("REVOKE", "Authorization Revoked"),
        # Legacy values kept so old rows still render
        ("BURN", "Burned (legacy)"),
        ("UNBURN", "Unburned (legacy)"),
        ("REVOKE_ACCESS", "Access Revoked (legacy)"),
        ("RESTORE", "Access Restored (legacy)"),
    ])
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        who = self.performed_by.username if self.performed_by else "system"
        return f"{self.device.fingerprint[:12]} - {self.action} by {who}"


class DailyReportLog(models.Model):
    """Records that the all-wards grand-total report was already sent for a
    given date, so repeated submissions that day don't re-trigger it."""
    report_date = models.DateField(unique=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    total_wards = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-report_date']

    def __str__(self):
        return f"Daily report sent for {self.report_date} at {self.sent_at}"


class AuditLog(models.Model):
    ACTIONS = [
        ("CREATE", "Create"),
        ("UPDATE", "Update"),
        ("DELETE", "Delete"),
        ("LOGIN", "Login"),
        ("LOGOUT", "Logout"),
        ("BURN", "Burn Device"),
        ("UNBURN", "Unburn Device"),
        ("SUBMIT", "Submit"),
        ("EDIT", "Edit"),
    ]

    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, related_name="audit_logs"
    )
    constituency = models.ForeignKey(
        Constituency, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=20, choices=ACTIONS)
    model_name = models.CharField(max_length=100, blank=True)  # e.g. "VRA", "KIEMSKit"
    object_id = models.CharField(max_length=64, blank=True)
    object_repr = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["constituency", "-created_at"]),
            models.Index(fields=["actor", "-created_at"]),
            models.Index(fields=["model_name", "object_id"]),
        ]

    def __str__(self):
        return f"[{self.created_at:%Y-%m-%d %H:%M}] {self.actor} {self.action} {self.model_name}#{self.object_id}"


class DailyReportState(models.Model):
    """
    One row per (constituency, report_date).
    This is the authoritative state machine that decides whether the
    grand-total report should be built and sent for a given day.
    """
    STATUS_CHOICES = [
        ("PENDING", "Pending — wards still submitting"),
        ("READY", "Ready — all wards in, waiting to send"),
        ("SENDING", "Sending — locked by a worker"),
        ("SENT", "Sent — done"),
        ("FAILED", "Failed — will be retried"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    constituency = models.ForeignKey(
        "Constituency", on_delete=models.CASCADE,
        related_name="daily_report_states",
    )
    report_date = models.DateField()

    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="PENDING")

    # Snapshot of what "submitted" looked like when we last evaluated
    total_wards = models.PositiveIntegerField(default=0)
    submitted_wards = models.PositiveIntegerField(default=0)

    # When we transitioned to READY, and when we actually sent
    ready_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    # Send metadata
    message_hash = models.CharField(max_length=64, blank=True)  # SHA-256 of payload
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)

    # Guards against a slow SENDER holding the row forever
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.CharField(max_length=100, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["constituency", "report_date"],
                name="unique_daily_report_per_constituency_day",
            ),
        ]
        indexes = [
            models.Index(fields=["status", "report_date"]),
            models.Index(fields=["constituency", "-report_date"]),
        ]
        ordering = ["-report_date"]

    def __str__(self):
        return f"{self.constituency.name} {self.report_date} — {self.status}"

    # ---- State machine transitions ----
    def mark_ready(self):
        """Called when all wards have submitted."""
        if self.status in ("READY", "SENT", "SENDING"):
            return False
        self.status = "READY"
        self.ready_at = timezone.now()
        self.save(update_fields=["status", "ready_at", "updated_at"])
        return True

    def mark_pending(self):
        """Called when a ward edits its numbers, invalidating an earlier READY."""
        if self.status == "SENT":
            # Do NOT un-send an already-sent report; it's a historical fact.
            return False
        if self.status in ("PENDING", "SENDING"):
            return False
        self.status = "PENDING"
        self.ready_at = None
        self.save(update_fields=["status", "ready_at", "updated_at"])
        return True


class CronHeartbeat(models.Model):
    """
    Records the last successful run of the daily-report cron tick.
    One row per job name so we can track multiple cron jobs later.
    """
    name = models.CharField(max_length=100, unique=True)
    last_run_at = models.DateTimeField(auto_now=True)
    last_summary = models.JSONField(default=dict, blank=True)
    total_runs = models.PositiveIntegerField(default=0)
    consecutive_failures = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} @ {self.last_run_at:%Y-%m-%d %H:%M:%S}"
