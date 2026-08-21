import uuid
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
    Voter Registration Assistant — assigned to one ward.
    device_token/fingerprint identify the VRA's browser across sessions
    (no login), so a returning VRA is recognized and can edit rather
    than duplicate their own entries.
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

    class Meta:
        ordering = ["ward__name", "name"]

    def __str__(self):
        return f"{self.name} ({self.ward.name})"


class KIEMSKit(models.Model):
    """
    A physical KIEMS kit assigned to a ward, and to one or more clerks
    (e.g. shared across shifts, or rotated between clerks over the phase).
    `status` = True means working/active, False means faulty/inactive.
    """
    kit_name = models.CharField(max_length=50)   # e.g. "Kit 19"
    serial_no = models.CharField(max_length=100, unique=True)
    status = models.BooleanField(default=True)
    ward = models.ForeignKey(Ward, on_delete=models.PROTECT, related_name="kits")
    assigned_clerks = models.ManyToManyField(Clerk, related_name="kits", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["ward__name", "kit_name"]
        indexes = [models.Index(fields=["ward", "status"])]

    def __str__(self):
        return f"{self.kit_name} — {self.serial_no}"


class Phase(models.Model):
    """
    The name of the program/exercise, e.g. Continuous Voter Registration (CVR),
    Mass Voter Registration (MVR), Enhanced CVR, etc.
    """
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

    entry_date = models.DateField()
    venue = models.CharField(max_length=150)

    # VRA-editable (field data)
    total_registered = models.PositiveIntegerField(default=0)

    # Office-only — VRA can see but never edit
    total_transferred = models.PositiveIntegerField(default=0)
    total_deleted = models.PositiveIntegerField(default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    edit_count = models.PositiveIntegerField(default=0)
    uploaded = models.BooleanField(default=False)

    # who last touched the office-only fields, for audit
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

    def __str__(self):
        return f"{self.kiems_kit.kit_name} — {self.entry_date}"