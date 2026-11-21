from django import forms
from django.contrib.auth import authenticate

from api.models import FormAReceipt
from home.models import (
    Ward, VRA, Clerk, KIEMSKit, DailyKIEMSEntry,
    WhatsAppGroup, WhatsAppSetting, Phase, )


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


class DailyEntryCreateForm(forms.ModelForm):
    """
    Office: create a manual REGISTRATION entry.
    - Ward is derived from the chosen kit — you don't pick both.
    - VRA is required (each row belongs to one VRA).
    - Clerk is optional and not shown by default.
    - Transferred and Updated are optional.
    """

    ward = forms.ModelChoiceField(
        queryset=Ward.objects.none(),
        widget=forms.Select(attrs={"class": "form-control", "id": "id_ward"}),
    )
    kiems_kit = forms.ModelChoiceField(
        queryset=KIEMSKit.objects.none(),
        widget=forms.Select(attrs={"class": "form-control", "id": "id_kiems_kit"}),
    )
    vra = forms.ModelChoiceField(
        queryset=VRA.objects.none(),
        widget=forms.Select(attrs={"class": "form-control", "id": "id_vra"}),
    )

    class Meta:
        model = DailyKIEMSEntry
        fields = [
            "entry_date",
            "ward",
            "kiems_kit",
            "vra",
            "venue",
            "registered_male",
            "registered_female",
            "total_transferred",
            "total_updated",
        ]
        widgets = {
            "entry_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "venue": forms.TextInput(attrs={"class": "form-control", "placeholder": "Venue name"}),
            "registered_male": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "registered_female": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "total_transferred": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
            "total_updated": forms.NumberInput(attrs={"class": "form-control", "min": 0}),
        }

    def __init__(self, *args, constituency=None, **kwargs):
        super().__init__(*args, **kwargs)
        if constituency:
            self.fields["ward"].queryset = Ward.objects.filter(
                constituency=constituency, active=True
            ).order_by("name")
            self.fields["kiems_kit"].queryset = KIEMSKit.objects.filter(
                ward__constituency=constituency, status=True
            ).order_by("kit_name")
            self.fields["vra"].queryset = VRA.objects.filter(
                ward__constituency=constituency, active=True
            ).order_by("name")

    def clean(self):
        cleaned = super().clean()
        kit = cleaned.get("kiems_kit")
        ward = cleaned.get("ward")
        vra = cleaned.get("vra")

        if kit and ward and kit.ward_id != ward.id:
            self.add_error("kiems_kit", "Kit does not belong to the selected ward.")
        if kit and vra and vra.ward_id != kit.ward_id:
            self.add_error("vra", "VRA does not belong to the kit's ward.")

        # Require at least one number
        male = cleaned.get("registered_male") or 0
        female = cleaned.get("registered_female") or 0
        transferred = cleaned.get("total_transferred") or 0
        if male == 0 and female == 0 and transferred == 0:
            raise forms.ValidationError(
                "Enter at least one of: male, female, or transferred."
            )

        # Prevent duplicate (kit, date, vra)
        phase = Phase.objects.filter(active=True).first()
        if phase and kit and vra and cleaned.get("entry_date"):
            exists = DailyKIEMSEntry.objects.filter(
                kiems_kit=kit,
                phase=phase,
                entry_date=cleaned["entry_date"],
                vra=vra,
                entry_type="REGISTRATION",
            )
            if self.instance.pk:
                exists = exists.exclude(pk=self.instance.pk)
            if exists.exists():
                raise forms.ValidationError(
                    "A registration already exists for this kit, VRA, and date."
                )
        return cleaned


class FormAReceiptForm(forms.ModelForm):
    """Single form for both FORM_A and ACK receipts. The template
    toggles the visible fields based on `form_type`."""

    class Meta:
        model = FormAReceipt
        fields = [
            "form_type", "ward", "phase",
            "form_count", "booklet_count",
            "serial_start", "serial_end", "note",
        ]
        widgets = {
            "note": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, constituency=None, **kwargs):
        super().__init__(*args, **kwargs)
        if constituency:
            self.fields["ward"].queryset = Ward.objects.filter(
                constituency=constituency, active=True
            ).order_by("name")
        self.fields["phase"].queryset = Phase.objects.filter(active=True).order_by("-start_date")

        # Mark serial fields optional at the model level; we enforce per-type
        self.fields["serial_start"].required = False
        self.fields["serial_end"].required = False
        self.fields["booklet_count"].required = False
        self.fields["form_count"].required = False

        for f in self.fields.values():
            f.widget.attrs.setdefault("class", "form-control")

    def clean(self):
        cleaned = super().clean()
        form_type = cleaned.get("form_type")

        if form_type == FormAReceipt.ACK:
            bc = cleaned.get("booklet_count")
            if not bc or bc < 1:
                self.add_error("booklet_count", "Booklet count must be at least 1.")
            elif cleaned.get("form_count") and cleaned["form_count"] != bc * 50:
                # normalize — we compute form_count from booklet_count
                cleaned["form_count"] = bc * 50
            else:
                cleaned["form_count"] = bc * 50

            if not cleaned.get("serial_start") or not cleaned.get("serial_end"):
                self.add_error("serial_start", "Serial numbers are required for acknowledgments.")
        else:
            fc = cleaned.get("form_count")
            if not fc or fc < 1:
                self.add_error("form_count", "Form count must be at least 1.")

            s, e = cleaned.get("serial_start"), cleaned.get("serial_end")
            if bool(s) ^ bool(e):
                self.add_error("serial_end", "Provide both serial numbers or neither.")
            cleaned["booklet_count"] = None

        return cleaned


class FormAReceiptEditForm(forms.ModelForm):
    """Editable fields after creation. form_type is immutable here."""

    class Meta:
        model = FormAReceipt
        fields = ["form_count", "booklet_count", "serial_start", "serial_end", "note"]
        widgets = {
            "note": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["serial_start"].required = False
        self.fields["serial_end"].required = False
        self.fields["booklet_count"].required = False
        for f in self.fields.values():
            f.widget.attrs.setdefault("class", "form-control")

    def clean(self):
        cleaned = super().clean()
        receipt = self.instance

        if receipt.form_type == FormAReceipt.ACK:
            bc = cleaned.get("booklet_count")
            if not bc or bc < 1:
                self.add_error("booklet_count", "Booklet count must be at least 1.")
            else:
                cleaned["form_count"] = bc * 50

            if not cleaned.get("serial_start") or not cleaned.get("serial_end"):
                self.add_error("serial_start", "Serial numbers are required for acknowledgments.")
        else:
            fc = cleaned.get("form_count")
            if not fc or fc < 1:
                self.add_error("form_count", "Form count must be at least 1.")
            cleaned["booklet_count"] = None

            s, e = cleaned.get("serial_start"), cleaned.get("serial_end")
            if bool(s) ^ bool(e):
                self.add_error("serial_end", "Provide both serial numbers or neither.")

        return cleaned
