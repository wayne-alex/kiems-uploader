from django.urls import path
from . import views

urlpatterns = [
    path("kiems/", views.kiems_entry_view, name="kiems_entry"),
    path("kiems/resolve-vra/", views.resolve_vra, name="resolve_vra"),
    path("kiems/bind-ward/", views.bind_ward, name="bind_ward"),
    path("kiems/kits-with-entries/", views.kits_with_entries, name="kits_with_entries"),
    path("kiems/submit-entries/", views.submit_daily_entries, name="submit_daily_entries"),
]