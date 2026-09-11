from django.contrib.auth.mixins import LoginRequiredMixin, AccessMixin


class ConstituencyScopedMixin(LoginRequiredMixin, AccessMixin):
    """Filters every queryset by the logged-in ICT officer's constituency."""

    def dispatch(self, request, *args, **kwargs):
        profile = getattr(request.user, "ict_profile", None)
        if not profile or not profile.active:
            return self.handle_no_permission()
        request.ict_profile = profile
        request.constituency = profile.constituency
        return super().dispatch(request, *args, **kwargs)

    def get_constituency(self):
        return self.request.constituency