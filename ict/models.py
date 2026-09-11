from django.conf import settings
from django.db import models

from home.models import Constituency


class ICTOfficerProfile(models.Model):
    """
    Links a logged-in User to exactly one Constituency. This is what lets
    ConstituencyScopedMixin figure out "whose data" an ICT officer should see,
    since Ward/VRA/Clerk/KIEMSKit/DailyKIEMSEntry only reach Constituency via
    Ward.constituency, and AuditLog already has it directly.
    """
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ict_profile",
    )
    constituency = models.ForeignKey(
        Constituency,
        on_delete=models.PROTECT,
        related_name="ict_officers",
    )
    phone_number = models.CharField(max_length=20, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "ICT Officer Profile"

    def __str__(self):
        name = self.user.get_full_name() or self.user.username
        return f"{name} — {self.constituency.name}"