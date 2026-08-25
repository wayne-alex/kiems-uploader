from django.urls import path
from . import views

urlpatterns = [
    # Main entry view
    path("kiems/", views.kiems_entry_view, name="kiems_entry"),

    # Device registration & authentication
    path('kiems/register-device/', views.register_device, name='register_device'),
    path('kiems/check-device-status/', views.check_device_status, name='check_device_status'),
    path('kiems/auto-bind-vra/', views.auto_bind_vra, name='auto_bind_vra'),

    # VRA operations
    path("kiems/resolve-vra/", views.resolve_vra, name="resolve_vra"),
    path("kiems/bind-ward/", views.bind_ward, name="bind_ward"),
    path("kiems/kits-with-entries/", views.kits_with_entries, name="kits_with_entries"),
    path("kiems/submit-entries/", views.submit_daily_entries, name="submit_daily_entries"),
]