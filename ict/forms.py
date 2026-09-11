from django import forms
from django.contrib.auth import authenticate

from home.models import (
    Ward, VRA, Clerk, KIEMSKit, DailyKIEMSEntry,
    WhatsAppGroup, WhatsAppSetting,
)


class WardForm(forms.ModelForm):
    class Meta:
        model = Ward
        fields = ["name", "code"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control", "placeholder": "Ward name"}),
            "code": forms.TextInput(attrs={"class": "form-control", "placeholder": "Optional code"}),
        }


class VRAForm(forms.ModelForm):
    class Meta:
        model = VRA
        fields = ["name", "ward", "active", "device_token", "device_fingerprint"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "ward": forms.Select(attrs={"class": "form-control"}),
            "device_token": forms.TextInput(attrs={"class": "form-control"}),
            "device_fingerprint": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, constituency=None, **kwargs):
        super().__init__(*args, **kwargs)
        if constituency:
            self.fields["ward"].queryset = Ward.objects.filter(constituency=constituency)


class ClerkForm(forms.ModelForm):
    class Meta:
        model = Clerk
        fields = ["name", "ward", "active", "device_token", "device_fingerprint"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "form-control"}),
            "ward": forms.Select(attrs={"class": "form-control"}),
            "device_token": forms.TextInput(attrs={"class": "form-control"}),
            "device_fingerprint": forms.TextInput(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, constituency=None, **kwargs):
        super().__init__(*args, **kwargs)
        if constituency:
            self.fields["ward"].queryset = Ward.objects.filter(constituency=constituency)


class KIEMSKitForm(forms.ModelForm):
    class Meta:
        model = KIEMSKit
        fields = ["kit_name", "serial_no", "status", "ward", "assigned_clerks"]
        widgets = {
            "kit_name": forms.TextInput(attrs={"class": "form-control"}),
            "serial_no": forms.TextInput(attrs={"class": "form-control"}),
            "ward": forms.Select(attrs={"class": "form-control"}),
            "assigned_clerks": forms.SelectMultiple(attrs={"class": "form-control", "size": 6}),
        }

    def __init__(self, *args, constituency=None, **kwargs):
        super().__init__(*args, **kwargs)
        if constituency:
            self.fields["ward"].queryset = Ward.objects.filter(constituency=constituency)
            self.fields["assigned_clerks"].queryset = Clerk.objects.filter(
                ward__constituency=constituency, active=True
            )


class DailyEntryOfficeForm(forms.ModelForm):
    class Meta:
        model = DailyKIEMSEntry
        fields = ["total_transferred", "total_updated"]
        widgets = {
            "total_transferred": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "total_updated": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }


class WhatsAppSettingForm(forms.ModelForm):
    class Meta:
        model = WhatsAppSetting
        fields = ["default_group", "notify_vra", "notify_edit", "notify_daily", "notify_grand_total"]
        widgets = {
            "default_group": forms.Select(attrs={"class": "form-control"}),
        }

    def __init__(self, *args, constituency=None, **kwargs):
        super().__init__(*args, **kwargs)
        if constituency:
            self.fields["default_group"].queryset = WhatsAppGroup.objects.filter(
                constituency=constituency, is_active=True
            ) | WhatsAppGroup.objects.filter(constituency__isnull=True, is_active=True)

class ICTOfficerLoginForm(forms.Form):
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            "id": "id_username",
            "placeholder": "e.g. jkiprop",
            "autocomplete": "username",
            "autofocus": True,
        }),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            "id": "id_password",
            "placeholder": "Enter your password",
            "autocomplete": "current-password",
        }),
    )
    remember = forms.BooleanField(required=False)

    def __init__(self, request=None, *args, **kwargs):
        self.request = request
        self.user = None
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        username = cleaned.get("username")
        password = cleaned.get("password")

        if username and password:
            user = authenticate(self.request, username=username, password=password)
            if user is None:
                raise forms.ValidationError("Your username or password was incorrect.")
            if not user.is_active:
                raise forms.ValidationError("This account is inactive.")

            # ICT officers must have a profile
            profile = getattr(user, "ict_profile", None)
            if profile is None:
                raise forms.ValidationError(
                    "This login is for ICT Officers only. "
                    "Please use the correct portal."
                )
            if not profile.active:
                raise forms.ValidationError(
                    "Your ICT account has been deactivated. "
                    "Contact the SuperAdmin."
                )

            self.user = user

        return cleaned