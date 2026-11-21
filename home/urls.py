from django.urls import path

from . import views

urlpatterns = [
    # ── Main entry / page renders ──────────────────────────────
    path("kiems/", views.kiems_entry_view, name="kiems_entry"),
    path("movement/", views.clerk_venue_mapping_view, name="clerk_venue_mapping"),
    path("", views.download_app_view, name="download_app"),

    # ── Device registration & auth ─────────────────────────────
    path("kiems/register-device/", views.register_device, name="register_device"),
    path("register-device/", views.register_device, name="register_device_alt"),
    path("kiems/check-device-status/", views.check_device_status, name="check_device_status"),
    path("kiems/auto-bind-vra/", views.auto_bind_vra, name="auto_bind_vra"),
    path("kiems/auto-bind-clerk/", views.auto_bind_clerk, name="auto_bind_clerk"),
    path("auto-bind-clerk/", views.auto_bind_clerk, name="auto_bind_clerk_alt"),

    # ── VRA / Clerk resolution ─────────────────────────────────
    path("kiems/resolve-vra/", views.resolve_vra, name="resolve_vra"),
    path("kiems/resolve-clerk/", views.resolve_clerk, name="resolve_clerk"),
    path("kiems/bind-ward/", views.bind_ward, name="bind_ward"),
    path("kiems/bind-clerk/", views.bind_clerk, name="bind_clerk"),

    # ── Reads ──────────────────────────────────────────────────
    path("kiems/kits-with-entries/", views.kits_with_entries, name="kits_with_entries"),
    path("api/wards-by-constituency/", views.wards_by_constituency, name="wards_by_constituency"),
    path("api/constituencies/", views.constituencies_list, name="constituencies_list"),
    path("ward-list/", views.ward_list, name="ward_list"),
    path("kit-list/", views.kit_list, name="kit_list"),
    path("clerk-records/", views.clerk_records, name="clerk_records"),
    path("clerk-movement-records/", views.clerk_movement_records, name="clerk_movement_records"),

    # ── Movement (read + clerk writes) ────────────────────────
    path("movement/kits/", views.movement_kits, name="movement_kits"),
    path("movement/summary/", views.movement_summary, name="movement_summary"),
    path("save-clerk-venues/", views.save_clerk_venues, name="save_clerk_venues"),
    path("save-clerk-movement-venues/", views.save_clerk_movement_venues, name="save_clerk_movement_venues"),

]
