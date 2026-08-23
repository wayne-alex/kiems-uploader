from django.urls import path

from . import views

app_name = 'superadmin'

urlpatterns = [
    # Login/Logout URLs
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('password-reset/', views.password_reset_request, name='password_reset'),
    # Dashboard
    path('dashboard/', views.dashboard, name='dashboard'),
    path('phases/', views.phase_list, name='phase_list'),
    path('phases/create/', views.phase_create, name='phase_create'),
    path('phases/<uuid:pk>/edit/', views.phase_edit, name='phase_edit'),
    path('phases/<uuid:pk>/delete/', views.phase_delete, name='phase_delete'),

    # Wards
    path('wards/', views.ward_list, name='ward_list'),
    path('wards/create/', views.ward_create, name='ward_create'),
    path('wards/<int:pk>/edit/', views.ward_edit, name='ward_edit'),
    path('wards/<int:pk>/delete/', views.ward_delete, name='ward_delete'),

    # Staff management (combined)
    path('staff/', views.staff_list, name='staff_list'),
    path('staff/create/', views.staff_create, name='staff_create'),
    path('staff/edit/<int:pk>/<str:staff_type>/', views.staff_edit, name='staff_edit'),
    path('staff/delete/<int:pk>/<str:staff_type>/', views.staff_delete, name='staff_delete'),
    path('staff/export/', views.export_staff, name='export_staff'),

    # KIEMS Kits
    path('kits/', views.kit_list, name='kit_list'),
    path('kits/create/', views.kit_create, name='kit_create'),
    path('kits/<int:pk>/edit/', views.kit_edit, name='kit_edit'),
    path('kits/<int:pk>/delete/', views.kit_delete, name='kit_delete'),

    # Daily Entries
    path('entries/', views.entry_list, name='entry_list'),
    path('entries/create/', views.entry_create, name='entry_create'),
    path('entries/<int:pk>/edit/', views.entry_edit, name='entry_edit'),
    path('entries/<int:pk>/delete/', views.entry_delete, name='entry_delete'),
    path('entries/generate_report/', views.generate_report, name='generate_report'),
    path('entries/generate_report/preview/', views.generate_report_preview, name='generate_report_preview'),
    path('entries/generate_report/download/', views.generate_report_download, name='generate_report_download'),

    # Import/Export
    path('export/', views.export_data, name='export_data'),
    path('import/', views.import_data, name='import_data'),

    # API
    path('api/chart-data/', views.get_chart_data, name='chart_data'),
    path('api/get-clerks-by-ward/', views.get_clerks_by_ward, name='get_clerks_by_ward'),
    path('api/get-vras-by-ward/', views.get_vras_by_ward, name='get_vras_by_ward'),
    path('api/kit-report/<int:kit_id>/', views.kit_report_api, name='kit_report_api'),
    path('filtered-preview/', views.filtered_preview, name='filtered_preview'),
    path('performance/', views.performance_dashboard, name='performance_dashboard'),

    # WhatsApp Bot URLs
    path('whatsapp/', views.whatsapp_status, name='whatsapp_status'),
    path('api/whatsapp/status/', views.whatsapp_bot_status, name='whatsapp_bot_status'),
    path('api/whatsapp/groups/', views.whatsapp_groups, name='whatsapp_groups'),
    path('api/whatsapp/settings/', views.whatsapp_save_settings, name='whatsapp_settings'),
    path('api/whatsapp/settings/get/', views.whatsapp_get_settings, name='whatsapp_get_settings'),
    path('api/whatsapp/test/', views.whatsapp_test, name='whatsapp_test'),
    path('api/whatsapp/send-daily-report/', views.send_daily_report_manual, name='send_daily_report_manual'),
]
