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

    # VRAs
    path('vras/', views.vra_list, name='vra_list'),
    path('vras/create/', views.vra_create, name='vra_create'),
    path('vras/<int:pk>/edit/', views.vra_edit, name='vra_edit'),
    path('vras/<int:pk>/delete/', views.vra_delete, name='vra_delete'),

    # Clerks
    path('clerks/', views.clerk_list, name='clerk_list'),
    path('clerks/create/', views.clerk_create, name='clerk_create'),
    path('clerks/<int:pk>/edit/', views.clerk_edit, name='clerk_edit'),
    path('clerks/<int:pk>/delete/', views.clerk_delete, name='clerk_delete'),

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

    # Import/Export
    path('export/', views.export_data, name='export_data'),
    path('import/', views.import_data, name='import_data'),

    # API
    path('api/chart-data/', views.get_chart_data, name='chart_data'),
]