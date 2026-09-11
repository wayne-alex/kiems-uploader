from django import forms
from django.contrib.auth.models import User
from django.db import transaction

from home.models import (
    Ward, VRA, Clerk, KIEMSKit, Phase, DailyKIEMSEntry, Constituency
)
from ict.models import ICTOfficerProfile


class ICTOfficerForm(forms.Form):
    # User fields
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "username"}),
    )
    first_name = forms.CharField(
        max_length=150, required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    last_name = forms.CharField(
        max_length=150, required=False,
        widget=forms.TextInput(attrs={"class": "form-control"}),
    )
    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(attrs={"class": "form-control"}),
    )
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
        help_text="Leave blank to keep current password (edit mode).",
    )
    confirm_password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(attrs={"class": "form-control"}),
    )

    # Profile fields
    constituency = forms.ModelChoiceField(
        queryset=Constituency.objects.all().order_by("name"),
        widget=forms.Select(attrs={"class": "form-control"}),
    )
    phone_number = forms.CharField(
        max_length=20, required=False,
        widget=forms.TextInput(attrs={"class": "form-control", "placeholder": "+254..."}),
    )
    active = forms.BooleanField(
        required=False, initial=True,
        widget=forms.CheckboxInput(attrs={"class": "form-check-input"}),
    )

    def __init__(self, *args, instance=None, **kwargs):
        self.instance = instance  # ICTOfficerProfile or None
        super().__init__(*args, **kwargs)
        if instance:
            u = instance.user
            self.fields["username"].initial = u.username
            self.fields["first_name"].initial = u.first_name
            self.fields["last_name"].initial = u.last_name
            self.fields["email"].initial = u.email
            self.fields["constituency"].initial = instance.constituency
            self.fields["phone_number"].initial = instance.phone_number
            self.fields["active"].initial = instance.active
            # Password optional on edit
            self.fields["password"].required = False

    def clean(self):
        cleaned = super().clean()
        pwd = cleaned.get("password")
        confirm = cleaned.get("confirm_password")
        if pwd or confirm:
            if pwd != confirm:
                raise forms.ValidationError("Passwords do not match.")
            if len(pwd) < 6:
                raise forms.ValidationError("Password must be at least 6 characters.")
        if not self.instance and not pwd:
            raise forms.ValidationError("Password is required when creating a new ICT officer.")
        return cleaned

    def clean_username(self):
        username = self.cleaned_data["username"]
        qs = User.objects.filter(username=username)
        if self.instance:
            qs = qs.exclude(pk=self.instance.user.pk)
        if qs.exists():
            raise forms.ValidationError("That username is already taken.")
        return username

    @transaction.atomic
    def save(self):
        data = self.cleaned_data
        if self.instance:
            user = self.instance.user
            profile = self.instance
        else:
            user = User()
            profile = ICTOfficerProfile(user=user)

        user.username = data["username"]
        user.first_name = data.get("first_name", "")
        user.last_name = data.get("last_name", "")
        user.email = data.get("email", "")
        user.is_staff = False  # ICT officers are NOT Django staff
        if data.get("password"):
            user.set_password(data["password"])
        user.save()

        profile.user = user
        profile.constituency = data["constituency"]
        profile.phone_number = data.get("phone_number", "")
        profile.active = data.get("active", True)
        profile.save()
        return profile


class ConstituencyForm(forms.ModelForm):
    class Meta:
        model = Constituency
        fields = ["name", "code", "active"]
        widgets = {
            "name": forms.TextInput(attrs={
                "class": "form-input",
                "placeholder": "e.g. Starehe",
                "autocomplete": "off",
            }),
            "code": forms.TextInput(attrs={
                "class": "form-input",
                "placeholder": "e.g. 001 (optional)",
                "autocomplete": "off",
            }),
        }
        labels = {
            "name": "Constituency Name",
            "code": "Constituency Code",
            "active": "Active",
        }
        help_texts = {
            "code": "Optional. Must be unique if provided.",
            "active": "Inactive constituencies are hidden from operational dashboards.",
        }

class WardForm(forms.ModelForm):
    class Meta:
        model = Ward
        fields = ["name", "code", "constituency"]   # <-- add here
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-input"}),
            "code": forms.TextInput(attrs={"class": "form-input"}),
            "constituency": forms.Select(attrs={"class": "form-input"}),
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
