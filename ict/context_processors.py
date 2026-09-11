from home.models import DailyReportState


def report_alerts(request):
    """Inject a count of FAILED report states for the sidebar badge."""
    try:
        profile = getattr(request.user, "ict_profile", None)
        if not profile:
            return {}
        count = DailyReportState.objects.filter(
            constituency=profile.constituency,
            status="FAILED",
        ).count()
        return {"report_alert_count": count}
    except Exception:
        return {}