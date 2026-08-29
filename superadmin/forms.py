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
            'status': forms.Select(attrs={'class': 'form-input'}, choices=((True, 'Active'), (False, 'Inactive'))),
            'ward': forms.Select(attrs={'class': 'form-input'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'ward' in self.data:
            try:
                ward_id = int(self.data.get('ward'))
                self.fields['assigned_clerks'].queryset = Clerk.objects.filter(ward_id=ward_id, active=True)
            except (ValueError, TypeError):
                pass
        elif self.instance.pk and self.instance.ward:
            self.fields['assigned_clerks'].queryset = Clerk.objects.filter(ward=self.instance.ward, active=True)


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
        fields = [
            'kiems_kit', 'phase', 'ward', 'vra', 'entry_date', 'venue',
            'registered_male', 'registered_female',
            'total_transferred', 'uploaded'
        ]
        widgets = {
            'kiems_kit': forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}),
            'phase': forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}),
            'ward': forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}),
            'vra': forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}),
            'entry_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-input', 'style': 'font-size:13px;'}),
            'venue': forms.TextInput(attrs={'class': 'form-input', 'placeholder': 'Venue location', 'style': 'font-size:13px;'}),
            'registered_male': forms.NumberInput(attrs={'class': 'form-input', 'min': 0, 'placeholder': '0', 'style': 'font-size:13px;'}),
            'registered_female': forms.NumberInput(attrs={'class': 'form-input', 'min': 0, 'placeholder': '0', 'style': 'font-size:13px;'}),
            'total_transferred': forms.NumberInput(attrs={'class': 'form-input', 'min': 0, 'placeholder': '0', 'style': 'font-size:13px;'}),
            'uploaded': forms.CheckboxInput(attrs={'class': 'form-checkbox', 'style': 'width:18px;height:18px;'})
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Filter VRAs by ward
        if 'ward' in self.data:
            try:
                ward_id = int(self.data.get('ward'))
                self.fields['vra'].queryset = VRA.objects.filter(ward_id=ward_id, active=True)
            except (ValueError, TypeError):
                pass
        elif self.instance.pk and self.instance.ward:
            self.fields['vra'].queryset = VRA.objects.filter(ward=self.instance.ward, active=True)


class DailyEntryFilterForm(forms.Form):
    phase = forms.ModelChoiceField(queryset=Phase.objects.all(), required=False,
                                   widget=forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}))
    ward = forms.ModelChoiceField(queryset=Ward.objects.all(), required=False,
                                  widget=forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}))
    kit = forms.ModelChoiceField(queryset=KIEMSKit.objects.all(), required=False,
                                 widget=forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}))
    vra = forms.ModelChoiceField(queryset=VRA.objects.all(), required=False,
                                 widget=forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}))
    date_from = forms.DateField(widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-input', 'style': 'font-size:13px;'}), required=False)
    date_to = forms.DateField(widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-input', 'style': 'font-size:13px;'}), required=False)
    uploaded = forms.ChoiceField(choices=[('', 'All'), ('True', 'Uploaded'), ('False', 'Not Uploaded')],
                                 required=False, widget=forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}))


class ImportForm(forms.Form):
    MODEL_CHOICES = [
        ('ward', 'Wards'),
        ('vra', 'VRAs'),
        ('clerk', 'Clerks'),
        ('kiemskit', 'KIEMS Kits'),
        ('phase', 'Phases'),
        ('entry', 'Daily Entries'),
        ('venue_mapping', 'Venue Mapping'),
    ]

    model_type = forms.ChoiceField(choices=MODEL_CHOICES, widget=forms.Select(attrs={'class': 'form-input'}))
    file = forms.FileField(widget=forms.FileInput(attrs={'class': 'form-input'}))


class ExportForm(forms.Form):
    MODEL_CHOICES = [
        ('entry', 'Daily Entries'),
        ('ward', 'Wards'),
        ('vra', 'VRAs'),
        ('clerk', 'Clerks'),
        ('kiemskit', 'KIEMS Kits'),
        ('phase', 'Phases'),
    ]

    FORMAT_CHOICES = [
        ('csv', 'CSV'),
        ('xlsx', 'Excel'),
    ]

    model_type = forms.ChoiceField(choices=MODEL_CHOICES, initial='entry',
                                   widget=forms.Select(attrs={'class': 'form-input'}))
    format = forms.ChoiceField(choices=FORMAT_CHOICES, initial='csv',
                               widget=forms.Select(attrs={'class': 'form-input'}))