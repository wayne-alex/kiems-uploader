from functools import wraps
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied


def ict_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), "ict:login_ict")

        profile = getattr(request.user, "ict_profile", None)
        if not profile or not profile.active:
            raise PermissionDenied("ICT access required.")

        request.ict_profile = profile
        request.constituency = profile.constituency
        return view_func(request, *args, **kwargs)
    return _wrapped