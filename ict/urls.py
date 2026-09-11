from django.urls import path

from . import views

app_name = "ict"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("login/", views.ict_login, name="login_ict"),
    path("logout/", views.ict_logout, name="logout"),

    # Wards
    path("wards/", views.ward_list, name="ward_list"),
    path("wards/add/", views.ward_create, name="ward_create"),
    path("wards/<int:pk>/", views.ward_detail, name="ward_detail"),
    path("wards/<int:pk>/edit/", views.ward_edit, name="ward_edit"),

    # Staff
    path("staff/", views.staff_list, name="staff_list"),
    path("staff/vra/add/", views.vra_create, name="vra_create"),
    path("staff/vra/<int:pk>/edit/", views.vra_edit, name="vra_edit"),
    path("staff/vra/<int:pk>/toggle/", views.vra_toggle, name="vra_toggle"),
    path("staff/clerk/add/", views.clerk_create, name="clerk_create"),
    path("staff/clerk/<int:pk>/edit/", views.clerk_edit, name="clerk_edit"),
    path("staff/clerk/<int:pk>/toggle/", views.clerk_toggle, name="clerk_toggle"),

    # Kits
    path("kits/", views.kit_list, name="kit_list"),
    path("kits/add/", views.kit_create, name="kit_create"),
    path("kits/<int:pk>/", views.kit_detail, name="kit_detail"),
    path("kits/<int:pk>/edit/", views.kit_edit, name="kit_edit"),

    # Devices
    path("devices/", views.device_list, name="device_list"),
    path("devices/<int:pk>/", views.device_detail, name="device_detail"),
    path("devices/<int:pk>/authorize/", views.device_authorize, name="device_authorize"),
    path("devices/<int:pk>/revoke/", views.device_revoke, name="device_revoke"),
    path("devices/<int:pk>/delete/", views.device_delete, name="device_delete"),

    # Entries

    path("entries/", views.entry_list, name="entry_list"),
    path("entries/<int:pk>/edit/", views.entry_edit, name="entry_edit"),
    path("entries/export/excel/", views.entry_export_excel, name="entry_export_excel"),
    path("entries/export/csv/", views.entry_export_csv, name="entry_export_csv"),
    path("entries/download-report/", views.entry_download_report, name="entry_download_report"),
    path("entries/download-report/preview/", views.entry_download_report_preview, name="entry_download_report_preview"),
    path("entries/import-csv/", views.entry_import_csv, name="entry_import_csv"),

    # Notifications
    path("notifications/", views.notification_list, name="notification_list"),
    path("notifications/settings/", views.notification_settings, name="notification_settings"),
    path("notifications/bot-status/", views.whatsapp_bot_status, name="whatsapp_bot_status"),
    path("notifications/groups/", views.whatsapp_groups_live, name="whatsapp_groups_live"),
    path("notifications/select-group/", views.whatsapp_select_group, name="whatsapp_select_group"),
    path("notifications/send-report/", views.whatsapp_send_report, name="whatsapp_send_report"),
    path("notifications/report-preview/", views.whatsapp_report_preview, name="whatsapp_report_preview"),

    # Audit
    path("audit/", views.audit_log_list, name="audit_log_list"),

    #     system
    path("status/", views.system_status, name="system_status"),
    path("status/run-tick/", views.system_status_run_tick, name="system_status_run_tick"),
    path("status/<uuid:state_id>/retry/", views.system_status_retry, name="system_status_retry"),
    path("status/reevaluate/<int:constituency_id>/<str:report_date>/",
         views.system_status_reevaluate, name="system_status_reevaluate"),
    path("status/health-report/", views.system_health_report_preview, name="system_health_report_preview"),
    path("status/health-report/download/", views.system_health_report_download, name="system_health_report_download"),

    #     Cron Job
    path("cron/send-reports/", views.cron_send_reports, name="cron_send_reports"),
]
