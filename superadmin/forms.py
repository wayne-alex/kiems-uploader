from django import forms
from django.contrib.auth.models import User
from home.models import (
    Ward, VRA, Clerk, KIEMSKit, Phase, DailyKIEMSEntry
)


class WardForm(forms.ModelForm):
    class Meta:
        model = Ward
        fields = ['name', 'code']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Enter ward name'}),
            'code': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Enter ward code'}),
        }


class VRAForm(forms.ModelForm):
    class Meta:
        model = VRA
        fields = ['name', 'ward', 'active', 'device_token', 'device_fingerprint']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'VRA name'}),
            'ward': forms.Select(attrs={'class': 'form-input'}),
            'device_token': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Device token'}),
            'device_fingerprint': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Device fingerprint'}),
        }


class ClerkForm(forms.ModelForm):
    class Meta:
        model = Clerk
        fields = ['name', 'ward', 'active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Clerk name'}),
            'ward': forms.Select(attrs={'class': 'form-input'}),
        }


class KIEMSKitForm(forms.ModelForm):
    assigned_clerks = forms.ModelMultipleChoiceField(
        queryset=Clerk.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={'class': 'form-input', 'size': 4})
    )

    class Meta:
        model = KIEMSKit
        fields = ['kit_name', 'serial_no', 'status', 'ward', 'assigned_clerks']
        widgets = {
            'kit_name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'e.g., Kit 19'}),
            'serial_no': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Serial number'}),
            'status': forms.Select(attrs={'class': 'form-input'}),
            'ward': forms.Select(attrs={'class': 'form-input'}),
        }


class PhaseForm(forms.ModelForm):
    class Meta:
        model = Phase
        fields = ['name', 'start_date', 'end_date', 'active']
        widgets = {
            'name': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Phase name'}),
            'start_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-input'}),
            'end_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-input'}),
        }


class DailyKIEMSEntryForm(forms.ModelForm):
    class Meta:
        model = DailyKIEMSEntry
        fields = ['kiems_kit', 'phase', 'ward', 'vra', 'entry_date', 'venue',
                  'total_registered', 'total_transferred', 'total_deleted', 'uploaded']
        widgets = {
            'kiems_kit': forms.Select(attrs={'class': 'form-input'}),
            'phase': forms.Select(attrs={'class': 'form-input'}),
            'ward': forms.Select(attrs={'class': 'form-input'}),
            'vra': forms.Select(attrs={'class': 'form-input'}),
            'entry_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-input'}),
            'venue': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Venue location'}),
            'total_registered': forms.NumberInput(attrs={'class': 'form-input', 'min': 0}),
            'total_transferred': forms.NumberInput(attrs={'class': 'form-input', 'min': 0}),
            'total_deleted': forms.NumberInput(attrs={'class': 'form-input', 'min': 0}),
        }


class DailyEntryFilterForm(forms.Form):
    phase = forms.ModelChoiceField(queryset=Phase.objects.all(), required=False,
                                   widget=forms.Select(attrs={'class': 'form-input'}))
    ward = forms.ModelChoiceField(queryset=Ward.objects.all(), required=False,
                                  widget=forms.Select(attrs={'class': 'form-input'}))
    kit = forms.ModelChoiceField(queryset=KIEMSKit.objects.all(), required=False,
                                 widget=forms.Select(attrs={'class': 'form-input'}))
    vra = forms.ModelChoiceField(queryset=VRA.objects.all(), required=False,
                                 widget=forms.Select(attrs={'class': 'form-input'}))
    date_from = forms.DateField(widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-input'}), required=False)
    date_to = forms.DateField(widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-input'}), required=False)
    uploaded = forms.ChoiceField(choices=[('', 'All'), ('True', 'Uploaded'), ('False', 'Not Uploaded')],
                                 required=False, widget=forms.Select(attrs={'class': 'form-input'}))


class ImportForm(forms.Form):
    FILE_TYPES = [
        ('csv', 'CSV'),
        ('xlsx', 'Excel'),
    ]

    MODEL_CHOICES = [
        ('ward', 'Wards'),
        ('vra', 'VRAs'),
        ('clerk', 'Clerks'),
        ('kiemskit', 'KIEMS Kits'),
        ('phase', 'Phases'),
        ('entry', 'Daily Entries'),
    ]

    model_type = forms.ChoiceField(choices=MODEL_CHOICES, widget=forms.Select(attrs={'class': 'form-input'}))
    file = forms.FileField(widget=forms.FileInput(attrs={'class': 'form-input'}))


class ExportForm(forms.Form):
    MODEL_CHOICES = [
        ('ward', 'Wards'),
        ('vra', 'VRAs'),
        ('clerk', 'Clerks'),
        ('kiemskit', 'KIEMS Kits'),
        ('phase', 'Phases'),
        ('entry', 'Daily Entries'),
    ]

    FORMAT_CHOICES = [
        ('csv', 'CSV'),
        ('xlsx', 'Excel'),
    ]

    model_type = forms.ChoiceField(choices=MODEL_CHOICES, widget=forms.Select(attrs={'class': 'form-input'}))
    format = forms.ChoiceField(choices=FORMAT_CHOICES, initial='csv',
                               widget=forms.Select(attrs={'class': 'form-input'}))