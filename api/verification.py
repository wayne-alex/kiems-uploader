from django.db.models import Sum

from .models import KitBaseline, TallyVerification


# Tune these
DRIFT_SMALL_THRESHOLD = 5         # abs delta that still counts as "small"
DRIFT_PCT_THRESHOLD = 0.02        # 2% of expected
BIG_DRIFT_MIN = 20                # absolute minimum before BIG_DRIFT


def compute_expected_total(kiems_kit, phase, entry_date):
    """
    expected_total = baseline + Σ all DailyKIEMSEntry.total_registered
                     for this kit+phase where entry_date < entry_date_arg
    """
    baseline = KitBaseline.objects.filter(
        kiems_kit=kiems_kit, phase=phase,
    ).first()

    base = baseline.baseline_total if baseline else 0

    agg = (
        kiems_kit.daily_entries
        .filter(phase=phase, entry_date__lt=entry_date)
        .aggregate(s=Sum("total_registered"))
    )
    submitted = agg["s"] or 0

    return base + submitted, base, submitted, bool(baseline)


def classify(observed_total, expected_total):
    delta = observed_total - expected_total
    if expected_total == 0 and observed_total == 0:
        return TallyVerification.RESULT_MATCH
    absd = abs(delta)
    # tolerant band
    if absd <= DRIFT_SMALL_THRESHOLD:
        return TallyVerification.RESULT_MATCH
    pct = absd / max(expected_total, 1)
    if absd >= BIG_DRIFT_MIN and pct >= DRIFT_PCT_THRESHOLD:
        return TallyVerification.RESULT_BIG_DRIFT
    return TallyVerification.RESULT_DRIFT