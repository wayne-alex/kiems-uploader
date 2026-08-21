from django import forms
from .models import DailyKIEMSEntry, KIEMSKit


class VRAEntryForm(forms.ModelForm):
    """What the VRA sees and can submit — office fields excluded entirely."""
    class Meta:
        model = DailyKIEMSEntry
        fields = ["kiems_kit", "venue", "total_registered"]

    def __init__(self, *args, ward=None, **kwargs):
        super().__init__(*args, **kwargs)
        if ward:
            self.fields["kiems_kit"].queryset = KIEMSKit.objects.filter(ward=ward)


class OfficeCorrectionForm(forms.ModelForm):
    """Only reachable from the staff-authenticated side."""
    class Meta:
        model = DailyKIEMSEntry
        fields = ["total_transferred", "total_deleted"]