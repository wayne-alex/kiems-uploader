from django.urls import path

from . import views

urlpatterns = [
    # ── Form A / Acknowledgment ──
    path("form-a/mine/", views.form_a_mine, name="api_form_a_mine"),
    path("form-a/kits/", views.form_a_kits_for_ward, name="api_form_a_kits"),
    path("form-a/spoilages-mine/", views.form_a_spoilages_mine, name="api_form_a_spoilages_mine"),
    path("form-a/report-spoil/", views.form_a_report_spoil, name="api_form_a_report_spoil"),
    path("form-a/allocate/", views.form_a_allocate, name="api_form_a_allocate"),
    path("form-a/deallocate/", views.form_a_deallocate, name="api_form_a_deallocate"),
    path("form-a/receipts/<uuid:receipt_id>/allocations/",
         views.form_a_receipt_allocations, name="api_form_a_receipt_allocations"),
    path("form-a/summary/", views.form_a_summary, name="api_form_a_summary"),
    path("form-a/daily/", views.form_a_daily, name="api_form_a_daily"),
    path("form-a/receive/", views.form_a_receive, name="api_form_a_receive"),
    path("form-a/usage/", views.form_a_usage, name="api_form_a_usage"),
    path("form-a/spoil/", views.form_a_spoil, name="api_form_a_spoil"),
    path("form-a/kits-ledger/", views.form_a_kits_ledger, name="api_form_a_kits_ledger"),

    # ── Incidents ──
    path("incidents/", views.submit_incident, name="api_submit_incident"),

    # ── Voter lookup (IEBC proxy) ──
    path("verify-voter/", views.verify_voter, name="api_verify_voter"),
    path("voter-lookup/health/", views.voter_lookup_health, name="api_voter_lookup_health"),

    # ── Push / sync / CSRF ──
    path("push/subscribe/", views.push_subscribe, name="api_push_subscribe"),
    path("sync/event/", views.sync_event, name="api_sync_event"),
    path("csrf/", views.csrf, name="api_csrf"),

    # ── Stats ──
    path("stats/day/", views.stats_day, name="api_stats_day"),

    # ── Announcements ──
    path("announcements/", views.list_announcements, name="api_announcements"),
    path("announcements/<uuid:announcement_id>/",
         views.announcement_detail, name="api_announcement_detail"),

    # ── Reference (training / help) ──
    path("reference/", views.reference_manifest, name="api_reference_manifest"),
    path("reference/<str:slug>/", views.reference_doc, name="api_reference_doc"),

    # ── Stations ──
    path("stations/", views.stations_search, name="api_stations_search"),

    # ── Entry submit (fast path) ──
    path("kiems/submit-entries/", views.submit_entries, name="api_submit_entries"),
    path("movement/save/", views.movement_save, name="api_movement_save"),
]
