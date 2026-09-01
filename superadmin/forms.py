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
        """Validate and determine entry type based on registrations"""
        cleaned_data = super().clean()

        registered_male = cleaned_data.get('registered_male', 0)
        registered_female = cleaned_data.get('registered_female', 0)
        venue = cleaned_data.get('venue', '').strip()
        entry_date = cleaned_data.get('entry_date')
        kiems_kit = cleaned_data.get('kiems_kit')
        phase = cleaned_data.get('phase')
        ward = cleaned_data.get('ward')
        vra = cleaned_data.get('vra')

        # Check if there are any registrations
        has_registrations = (registered_male > 0 or registered_female > 0)

        # If there are registrations, venue is required
        if has_registrations and not venue:
            raise forms.ValidationError('Venue is required when registering voters.')

        # If there's a venue but no registrations, it's a venue mapping
        if venue and not has_registrations:
            # This is a venue mapping - allow it
            # We'll set the entry_type in the view after save
            pass

        # If no venue and no registrations, that's an error
        if not venue and not has_registrations:
            raise forms.ValidationError(
                'Either provide a venue (for venue mapping) or enter voter registrations.'
            )

        # Check for duplicate entries (same kit, phase, date, vra)
        if entry_date and kiems_kit and phase and vra:
            existing = DailyKIEMSEntry.objects.filter(
                kiems_kit=kiems_kit,
                phase=phase,
                entry_date=entry_date,
                vra=vra
            )

            # Exclude current instance when editing
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