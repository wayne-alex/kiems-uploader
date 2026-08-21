# superadmin/admin_site.py
from django.contrib.admin import AdminSite

class SuperAdminSite(AdminSite):
    site_header = "SuperAdmin Dashboard"
    site_title = "SuperAdmin Portal"
    index_title = "Welcome to SuperAdmin Dashboard"
    site_url = "/superadmin/"