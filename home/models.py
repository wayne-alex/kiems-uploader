import uuid

from django.contrib.auth.models import User
from django.db import models


class Ward(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=20, unique=True, blank=True, null=True)

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
        # Auto-calculate total from male + female
        self.total_registered = self.registered_male + self.registered_female
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.kiems_kit.kit_name} – {self.entry_date}"


# ==================== WHATSAPP MODELS ====================

class WhatsAppGroup(models.Model):
    """WhatsApp Groups configuration"""
    group_id = models.CharField(max_length=100, unique=True)
    name = models.CharField(max_length=200)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} ({self.group_id})"

    class Meta:
        ordering = ['name']


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


# models.py - Add these new models

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
    is_burned = models.BooleanField(default=False)  # Device is burned/authorized
    is_active = models.BooleanField(default=True)
    burn_date = models.DateTimeField(null=True, blank=True)
    burn_notes = models.TextField(blank=True)

    # Associated VRA
    vra = models.ForeignKey(
        'VRA',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='devices'
    )
    # Associated Clerk
    clerk = models.ForeignKey(
        'Clerk',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='devices'
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
        return f"{self.fingerprint[:12]}... ({'Burned' if self.is_burned else 'Unburned'})"


class DeviceBurnLog(models.Model):
    """Log of device burning/unburning operations"""
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name='burn_logs')
    action = models.CharField(max_length=20, choices=[
        ('BURN', 'Burn Device'),
        ('UNBURN', 'Unburn Device'),
        ('REVOKE', 'Revoke Access'),
        ('RESTORE', 'Restore Access'),
    ])
    performed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.device.fingerprint[:12]} - {self.action} by {self.performed_by}"