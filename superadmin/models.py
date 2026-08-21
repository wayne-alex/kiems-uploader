from django.contrib.auth.models import User
from django.db import models


class AuditLog(models.Model):
    """Audit Log Model"""
    ACTION_CHOICES = [
        ('create', 'Create'),
        ('update', 'Update'),
        ('delete', 'Delete'),
        ('upload', 'Upload'),
        ('sync', 'Sync'),
        ('login', 'Login'),
        ('logout', 'Logout'),
    ]

    MODEL_CHOICES = [
        ('clerk', 'Clerk'),
        ('kit', 'KIEMS Kit'),
        ('ward', 'Ward'),
        ('entry', 'Daily Entry'),
        ('phase', 'Phase'),
    ]

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='audit_logs')
    action = models.CharField(max_length=20, choices=ACTION_CHOICES)
    model_type = models.CharField(max_length=20, choices=MODEL_CHOICES)
    object_id = models.PositiveIntegerField()
    object_repr = models.CharField(max_length=200)
    changes = models.JSONField(default=dict)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=500, blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Audit Log'
        verbose_name_plural = 'Audit Logs'

    def __str__(self):
        return f"{self.user.username} - {self.get_action_display()} - {self.object_repr}"


class ImportExportLog(models.Model):
    """Import/Export Log Model"""
    OPERATION_CHOICES = [
        ('import', 'Import'),
        ('export', 'Export'),
    ]

    MODEL_CHOICES = [
        ('clerk', 'Clerk'),
        ('kit', 'KIEMS Kit'),
        ('ward', 'Ward'),
        ('entry', 'Daily Entry'),
    ]

    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='import_export_logs')
    operation = models.CharField(max_length=20, choices=OPERATION_CHOICES)
    model_type = models.CharField(max_length=20, choices=MODEL_CHOICES)
    filename = models.CharField(max_length=255)
    rows_processed = models.IntegerField(default=0)
    rows_success = models.IntegerField(default=0)
    rows_failed = models.IntegerField(default=0)
    error_log = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Import/Export Log'
        verbose_name_plural = 'Import/Export Logs'

    def __str__(self):
        return f"{self.user.username} - {self.get_operation_display()} - {self.model_type}"
