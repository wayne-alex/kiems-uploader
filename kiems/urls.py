from django.contrib import admin
from django.urls import path, include, re_path

from api import views as api_views
from api.views_iebc_proxy import iebc_proxy

urlpatterns = [
    re_path(r"^iebc-proxy(?:/(?P<path>.*))?$", iebc_proxy, name="iebc_proxy"),
    path('admin/', admin.site.urls),
    path('', include('home.urls')),
    path('superadmin/', include('superadmin.urls')),
    path('ict/', include('ict.urls')),
    path("kiems/submit-entries/", api_views.submit_entries, name="api_submit_entries"),
    path("movement/save/", api_views.movement_save, name="api_movement_save"),
    path("api/csrf/", api_views.csrf, name="api_csrf"),
    path('api/', include('api.urls')),
]
