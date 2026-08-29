from django.urls import path
from . import views

urlpatterns = [
    # Main entry view
    path("kiems/", views.kiems_entry_view, name="kiems_entry"),

    # Device registration & authentication
    path('kiems/register-device/', views.register_device, name='register_device'),
    path('kiems/check-device-status/', views.check_device_status, name='check_device_status'),
    path('kiems/auto-bind-vra/', views.auto_bind_vra, name='auto_bind_vra'),
    path('kiems/auto-bind-clerk/', views.auto_bind_clerk, name='auto_bind_clerk'),

    # VRA operations
    path("kiems/resolve-vra/", views.resolve_vra, name="resolve_vra"),
    path("kiems/resolve-clerk/", views.resolve_clerk, name="resolve_clerk"),
    path("kiems/bind-ward/", views.bind_ward, name="bind_ward"),
    path("kiems/bind-clerk/", views.bind_clerk, name="bind_clerk"),
    path("kiems/kits-with-entries/", views.kits_with_entries, name="kits_with_entries"),
    path("kiems/submit-entries/", views.submit_daily_entries, name="submit_daily_entries"),

    # Clerk mapping API endpoints
    path('mapping/', views.clerk_venue_mapping_view, name='clerk_venue_mapping'),
    path('ward-list/', views.ward_list, name='ward_list'),
    path('kit-list/', views.kit_list, name='kit_list'),
    path('clerk-records/', views.clerk_records, name='clerk_records'),
    path('save-clerk-venues/', views.save_clerk_venues, name='save_clerk_venues'),
    path('register-device/', views.register_device, name='register_device'),
    path('auto-bind-clerk/', views.auto_bind_clerk, name='auto_bind_clerk'),
]