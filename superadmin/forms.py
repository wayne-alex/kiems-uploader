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
    entry_type = forms.ChoiceField(
        choices=[('', 'Auto-detect from registrations')] + list(DailyKIEMSEntry.ENTRY_TYPES),
        required=False,
        widget=forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}),
        help_text='Leave as Auto-detect to let the system decide from Male/Female counts, or force a type explicitly.'
    )

    class Meta:
        model = DailyKIEMSEntry
        fields = [
            'kiems_kit', 'phase', 'ward', 'vra', 'entry_date', 'venue',
            'entry_type',
            'registered_male', 'registered_female',
            'total_transferred', 'uploaded'
        ]
        widgets = {
            'kiems_kit': forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}),
            'phase': forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}),
            'ward': forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}),
            'vra': forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}),
            'entry_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-input', 'style': 'font-size:13px;'}),
            'venue': forms.TextInput(
                attrs={'class': 'form-input', 'placeholder': 'Venue location', 'style': 'font-size:13px;'}),
            'registered_male': forms.NumberInput(
                attrs={'class': 'form-input', 'min': 0, 'placeholder': '0', 'style': 'font-size:13px;'}),
            'registered_female': forms.NumberInput(
                attrs={'class': 'form-input', 'min': 0, 'placeholder': '0', 'style': 'font-size:13px;'}),
            'total_transferred': forms.NumberInput(
                attrs={'class': 'form-input', 'min': 0, 'placeholder': '0', 'style': 'font-size:13px;'}),
            'uploaded': forms.CheckboxInput(attrs={'class': 'form-checkbox', 'style': 'width:18px;height:18px;'})
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'ward' in self.data:
            try:
                ward_id = int(self.data.get('ward'))
                self.fields['vra'].queryset = VRA.objects.filter(ward_id=ward_id, active=True)
            except (ValueError, TypeError):
                pass
        elif self.instance.pk and self.instance.ward:
            self.fields['vra'].queryset = VRA.objects.filter(ward=self.instance.ward, active=True)
        # Pre-select the current explicit type when editing
        if self.instance.pk:
            self.fields['entry_type'].initial = self.instance.entry_type

    def clean(self):
        cleaned_data = super().clean()

        registered_male = cleaned_data.get('registered_male') or 0
        registered_female = cleaned_data.get('registered_female') or 0
        venue = (cleaned_data.get('venue') or '').strip()
        entry_type_choice = cleaned_data.get('entry_type')  # '', 'VENUE', or 'REGISTRATION'
        entry_date = cleaned_data.get('entry_date')
        kiems_kit = cleaned_data.get('kiems_kit')
        phase = cleaned_data.get('phase')
        vra = cleaned_data.get('vra')

        has_registrations = (registered_male > 0 or registered_female > 0)

        # Explicit choice wins; otherwise fall back to the old auto-detect behavior
        if entry_type_choice in ('VENUE', 'REGISTRATION'):
            effective_type = entry_type_choice
        else:
            effective_type = 'REGISTRATION' if has_registrations else 'VENUE'

        if effective_type == 'REGISTRATION':
            if not venue:
                raise forms.ValidationError('Venue is required when registering voters.')
            if not has_registrations:
                raise forms.ValidationError(
                    'Enter Male/Female registration numbers, or set Entry Type to "Venue Mapping Only".'
                )
        elif effective_type == 'VENUE' and not venue:
            raise forms.ValidationError('Venue is required for a venue mapping entry.')

        cleaned_data['resolved_entry_type'] = effective_type

        if entry_date and kiems_kit and phase and vra:
            existing = DailyKIEMSEntry.objects.filter(
                kiems_kit=kiems_kit, phase=phase, entry_date=entry_date, vra=vra
            )
            if self.instance and self.instance.pk:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise forms.ValidationError(
                    f'An entry already exists for Kit "{kiems_kit.kit_name}", '
                    f'Phase "{phase.name}", Date "{entry_date}", and VRA "{vra.name}".'
                )

        return cleaned_data


class DailyEntryFilterForm(forms.Form):
    phase = forms.ModelChoiceField(
        queryset=Phase.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'})
    )
    ward = forms.ModelChoiceField(
        queryset=Ward.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'})
    )
    kit = forms.ModelChoiceField(
        queryset=KIEMSKit.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'})
    )
    vra = forms.ModelChoiceField(
        queryset=VRA.objects.all(),
        required=False,
        widget=forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'})
    )
    date_from = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-input', 'style': 'font-size:13px;'}),
        required=False
    )
    date_to = forms.DateField(
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-input', 'style': 'font-size:13px;'}),
        required=False
    )
    uploaded = forms.ChoiceField(
        choices=[('', 'All'), ('True', 'Uploaded'), ('False', 'Not Uploaded')],
        required=False,
        widget=forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'})
    )
    entry_type = forms.ChoiceField(
        choices=[
            ('', 'All Types'),
            ('REGISTRATION', 'Registration Entries'),
            ('VENUE', 'Venue Mappings')
        ],
        required=False,
        widget=forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'})
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Add empty labels for clarity
        self.fields['phase'].empty_label = 'All Phases'
        self.fields['ward'].empty_label = 'All Wards'
        self.fields['kit'].empty_label = 'All Kits'
        self.fields['vra'].empty_label = 'All VRAs'


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

    def clean_file(self):
        """Validate file extension"""
        file = self.cleaned_data.get('file')
        if file:
            ext = file.name.split('.')[-1].lower()
            if ext not in ['csv', 'xlsx', 'xls']:
                raise forms.ValidationError('File must be CSV or Excel format.')
        return file


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

    model_type = forms.ChoiceField(
        choices=MODEL_CHOICES,
        initial='entry',
        widget=forms.Select(attrs={'class': 'form-input'})
    )
    format = forms.ChoiceField(
        choices=FORMAT_CHOICES,
        initial='csv',
        widget=forms.Select(attrs={'class': 'form-input'})
    )


# Optional: Separate form for venue mapping (if you want a dedicated form)
class VenueMappingForm(forms.ModelForm):
    """Dedicated form for pre-mapping venues without registrations"""

    class Meta:
        model = DailyKIEMSEntry
        fields = ['kiems_kit', 'phase', 'ward', 'vra', 'entry_date', 'venue']
        widgets = {
            'kiems_kit': forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}),
            'phase': forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}),
            'ward': forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}),
            'vra': forms.Select(attrs={'class': 'form-input', 'style': 'font-size:13px;'}),
            'entry_date': forms.DateInput(attrs={'type': 'date', 'class': 'form-input', 'style': 'font-size:13px;'}),
            'venue': forms.TextInput(
                attrs={'class': 'form-input', 'placeholder': 'Venue location', 'style': 'font-size:13px;'}),
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

    def clean(self):
        """Ensure this is a venue mapping (no registrations)"""
        cleaned_data = super().clean()
        venue = cleaned_data.get('venue', '').strip()

        if not venue:
            raise forms.ValidationError('Venue is required for mapping.')

        # Check for duplicate entries
        entry_date = cleaned_data.get('entry_date')
        kiems_kit = cleaned_data.get('kiems_kit')
        phase = cleaned_data.get('phase')
        vra = cleaned_data.get('vra')

        if entry_date and kiems_kit and phase and vra:
            existing = DailyKIEMSEntry.objects.filter(
                kiems_kit=kiems_kit,
                phase=phase,
                entry_date=entry_date,
                vra=vra
            )

            if self.instance and self.instance.pk:
                existing = existing.exclude(pk=self.instance.pk)

            if existing.exists():
                raise forms.ValidationError(
                    f'An entry already exists for this Kit, Phase, Date, and VRA combination.'
                )

        return cleaned_data