import csv
import io
from datetime import datetime, timedelta

import openpyxl
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout, authenticate, login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Count, Sum, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET

# Optional imports with graceful fallback
try:
    import weasyprint

    WEASYPRINT_AVAILABLE = True
except ImportError:
    WEASYPRINT_AVAILABLE = False

try:
    from reportlab.graphics.shapes import Drawing, Rect
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as pdfcanvas

    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False
    # Define dummy classes to avoid NameError
    colors = type('colors', (), {})
    pdfcanvas = None
    Drawing = None
    Rect = None
    letter = (612, 792)
    inch = 72
    ImageReader = None

from home.models import (
    Ward, VRA, Clerk, KIEMSKit, Phase, DailyKIEMSEntry
)
from .forms import (
    WardForm, VRAForm, ClerkForm, KIEMSKitForm, PhaseForm,
    DailyKIEMSEntryForm, DailyEntryFilterForm, ImportForm, ExportForm
)


# ==================== HELPER FUNCTIONS ====================

def is_superadmin(user):
    return user.is_superuser


# ==================== AUTH VIEWS ====================

def login_view(request):
    if request.user.is_authenticated:
        if request.user.is_superuser:
            return redirect('superadmin:dashboard')
        else:
            messages.error(request, 'You do not have permission to access the admin panel.')
            logout(request)
            return redirect('superadmin:login')

    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')

        if not email or not password:
            messages.error(request, 'Please provide both email and password.')
            return render(request, 'superadmin/login.html', {'email': email})

        username = User.objects.filter(email=email).first()
        user = authenticate(request, username=username, password=password)

        if user is not None:
            if user.is_superuser:
                login(request, user)
                messages.success(request, f'Welcome back, {user.get_full_name() or user.username}!')
                return redirect('superadmin:dashboard')
            else:
                messages.error(request, 'You do not have administrator privileges.')
        else:
            messages.error(request, 'Invalid email or password. Please try again.')

        return render(request, 'superadmin/login.html', {'email': email})

    return render(request, 'superadmin/login.html')


def logout_view(request):
    if request.user.is_authenticated:
        messages.info(request, 'You have been logged out successfully.')
        logout(request)
    return redirect('superadmin:login')


def password_reset_request(request):
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()
        if email:
            try:
                user = User.objects.get(email=email)
                if user.is_superuser:
                    messages.success(request, 'Password reset instructions have been sent to your email.')
                    return redirect('superadmin:login')
                else:
                    messages.error(request, 'This email is not associated with an admin account.')
            except User.DoesNotExist:
                messages.error(request, 'No account found with this email address.')
        else:
            messages.error(request, 'Please provide your email address.')
    return render(request, 'superadmin/password_reset.html')


# ==================== DASHBOARD ====================

@login_required
@user_passes_test(is_superadmin)
def dashboard(request):
    active_phase = Phase.objects.filter(active=True).first()

    metrics = {
        'total_wards': Ward.objects.count(),
        'total_vras': VRA.objects.filter(active=True).count(),
        'total_clerks': Clerk.objects.filter(active=True).count(),
        'total_kits': KIEMSKit.objects.filter(status=True).count(),
        'inactive_kits': KIEMSKit.objects.filter(status=False).count(),
        'total_phases': Phase.objects.count(),
        'total_entries': DailyKIEMSEntry.objects.count(),
        'total_registered': DailyKIEMSEntry.objects.aggregate(Sum('total_registered'))['total_registered__sum'] or 0,
        'total_transferred': DailyKIEMSEntry.objects.aggregate(Sum('total_transferred'))['total_transferred__sum'] or 0,
        'total_updated': DailyKIEMSEntry.objects.aggregate(Sum('total_updated'))['total_updated__sum'] or 0,
    }

    recent_entries = DailyKIEMSEntry.objects.select_related(
        'kiems_kit', 'phase', 'ward', 'vra'
    ).order_by('-created_at')[:10]

    today = timezone.now().date()
    today_activity = DailyKIEMSEntry.objects.filter(
        entry_date=today
    ).aggregate(
        registered=Sum('total_registered'),
        transferred=Sum('total_transferred'),
        deleted=Sum('total_updated')
    )

    kits_by_ward = KIEMSKit.objects.values('ward__name').annotate(
        total=Count('id'),
        active=Count('id', filter=Q(status=True))
    ).order_by('-total')[:5]

    context = {
        'active_phase': active_phase,
        'metrics': metrics,
        'recent_entries': recent_entries,
        'today_activity': today_activity,
        'kits_by_ward': kits_by_ward,
    }
    return render(request, 'superadmin/dashboard.html', context)


# ==================== PHASE CRUD ====================

@login_required
@user_passes_test(is_superadmin)
def phase_list(request):
    phases = Phase.objects.all().order_by('-start_date')
    return render(request, 'superadmin/phase_list.html', {'phases': phases})


@login_required
@user_passes_test(is_superadmin)
def phase_create(request):
    if request.method == 'POST':
        form = PhaseForm(request.POST)
        if form.is_valid():
            phase = form.save()
            messages.success(request, f'Phase "{phase.name}" created successfully!')
            return redirect('superadmin:phase_list')
    else:
        form = PhaseForm()
    return render(request, 'superadmin/phase_form.html', {'form': form, 'title': 'Create Phase'})


@login_required
@user_passes_test(is_superadmin)
def phase_edit(request, pk):
    phase = get_object_or_404(Phase, pk=pk)
    if request.method == 'POST':
        form = PhaseForm(request.POST, instance=phase)
        if form.is_valid():
            form.save()
            messages.success(request, f'Phase "{phase.name}" updated successfully!')
            return redirect('superadmin:phase_list')
    else:
        form = PhaseForm(instance=phase)
    return render(request, 'superadmin/phase_form.html', {'form': form, 'title': 'Edit Phase'})


@login_required
@user_passes_test(is_superadmin)
@require_POST
def phase_delete(request, pk):
    phase = get_object_or_404(Phase, pk=pk)
    phase_name = phase.name
    phase.delete()
    messages.success(request, f'Phase "{phase_name}" deleted successfully!')
    return redirect('superadmin:phase_list')


# ==================== WARD CRUD ====================

@login_required
@user_passes_test(is_superadmin)
def ward_list(request):
    wards = Ward.objects.all().order_by('name')
    return render(request, 'superadmin/ward_list.html', {'wards': wards})


@login_required
@user_passes_test(is_superadmin)
def ward_create(request):
    if request.method == 'POST':
        form = WardForm(request.POST)
        if form.is_valid():
            ward = form.save()
            messages.success(request, f'Ward "{ward.name}" created successfully!')
            return redirect('superadmin:ward_list')
    else:
        form = WardForm()
    return render(request, 'superadmin/ward_form.html', {'form': form, 'title': 'Create Ward'})


@login_required
@user_passes_test(is_superadmin)
def ward_edit(request, pk):
    ward = get_object_or_404(Ward, pk=pk)
    if request.method == 'POST':
        form = WardForm(request.POST, instance=ward)
        if form.is_valid():
            form.save()
            messages.success(request, f'Ward "{ward.name}" updated successfully!')
            return redirect('superadmin:ward_list')
    else:
        form = WardForm(instance=ward)
    return render(request, 'superadmin/ward_form.html', {'form': form, 'title': 'Edit Ward'})


@login_required
@user_passes_test(is_superadmin)
@require_POST
def ward_delete(request, pk):
    ward = get_object_or_404(Ward, pk=pk)
    ward_name = ward.name
    ward.delete()
    messages.success(request, f'Ward "{ward_name}" deleted successfully!')
    return redirect('superadmin:ward_list')


# ==================== STAFF CRUD ====================

@login_required
@user_passes_test(is_superadmin)
def staff_list(request):
    clerks = Clerk.objects.select_related('ward').all().order_by('ward__name', 'name')
    vras = VRA.objects.select_related('ward').all().order_by('ward__name', 'name')

    staff_list = []
    for clerk in clerks:
        staff_list.append({
            'id': clerk.id,
            'type': 'clerk',
            'name': clerk.name,
            'ward': clerk.ward,
            'active': clerk.active,
            'created_at': clerk.created_at,
            'details': {'role': 'Clerk', 'type_label': 'Clerk'}
        })

    for vra in vras:
        staff_list.append({
            'id': vra.id,
            'type': 'vra',
            'name': vra.name,
            'ward': vra.ward,
            'active': vra.active,
            'created_at': vra.created_at,
            'details': {
                'role': 'VRA',
                'type_label': 'VRA',
                'device_token': vra.device_token,
                'device_fingerprint': vra.device_fingerprint
            }
        })

    staff_list.sort(key=lambda x: (x['ward'].name if x['ward'] else '', x['name']))

    stats = {
        'total_clerks': clerks.count(),
        'total_vras': vras.count(),
        'active_clerks': clerks.filter(active=True).count(),
        'active_vras': vras.filter(active=True).count(),
        'total_staff': len(staff_list),
    }

    return render(request, 'superadmin/staff_list.html', {
        'staff_list': staff_list,
        'stats': stats,
        'clerks_count': clerks.count(),
        'vras_count': vras.count(),
    })


@login_required
@user_passes_test(is_superadmin)
def staff_create(request):
    staff_type = request.GET.get('type', request.POST.get('type', 'clerk'))

    if request.method == 'POST':
        staff_type = request.POST.get('type', 'clerk')

        if staff_type == 'clerk':
            form = ClerkForm(request.POST)
            if form.is_valid():
                clerk = form.save()
                messages.success(request, f'Clerk "{clerk.name}" created successfully!')
                return redirect('superadmin:staff_list')
        else:
            form = VRAForm(request.POST)
            if form.is_valid():
                vra = form.save()
                messages.success(request, f'VRA "{vra.name}" created successfully!')
                return redirect('superadmin:staff_list')
    else:
        if staff_type == 'clerk':
            form = ClerkForm()
        else:
            form = VRAForm()

    return render(request, 'superadmin/staff_form.html', {
        'form': form,
        'staff_type': staff_type,
        'title': f'Create {staff_type.upper()}'
    })


@login_required
@user_passes_test(is_superadmin)
def staff_edit(request, pk, staff_type):
    if staff_type == 'clerk':
        staff = get_object_or_404(Clerk, pk=pk)
        if request.method == 'POST':
            form = ClerkForm(request.POST, instance=staff)
            if form.is_valid():
                form.save()
                messages.success(request, f'Clerk "{staff.name}" updated successfully!')
                return redirect('superadmin:staff_list')
        else:
            form = ClerkForm(instance=staff)
    else:
        staff = get_object_or_404(VRA, pk=pk)
        if request.method == 'POST':
            form = VRAForm(request.POST, instance=staff)
            if form.is_valid():
                form.save()
                messages.success(request, f'VRA "{staff.name}" updated successfully!')
                return redirect('superadmin:staff_list')
        else:
            form = VRAForm(instance=staff)

    return render(request, 'superadmin/staff_form.html', {
        'form': form,
        'staff': staff,
        'staff_type': staff_type,
        'title': f'Edit {staff_type.upper()}'
    })


@login_required
@user_passes_test(is_superadmin)
@require_POST
def staff_delete(request, pk, staff_type):
    if staff_type == 'clerk':
        staff = get_object_or_404(Clerk, pk=pk)
        staff_name = staff.name
        staff.delete()
        messages.success(request, f'Clerk "{staff_name}" deleted successfully!')
    else:
        staff = get_object_or_404(VRA, pk=pk)
        staff_name = staff.name
        staff.delete()
        messages.success(request, f'VRA "{staff_name}" deleted successfully!')
    return redirect('superadmin:staff_list')


# ==================== KIEMS KIT CRUD ====================

@login_required
@user_passes_test(is_superadmin)
def kit_list(request):
    kits = KIEMSKit.objects.select_related('ward').prefetch_related('assigned_clerks').all().order_by('ward__name',
                                                                                                      'kit_name')
    return render(request, 'superadmin/kit_list.html', {'kits': kits})


@login_required
@user_passes_test(is_superadmin)
def kit_create(request):
    if request.method == 'POST':
        form = KIEMSKitForm(request.POST)
        if form.is_valid():
            kit = form.save()
            messages.success(request, f'KIEMS Kit "{kit.kit_name}" created successfully!')
            return redirect('superadmin:kit_list')
    else:
        form = KIEMSKitForm()
    return render(request, 'superadmin/kit_form.html', {'form': form, 'title': 'Create KIEMS Kit'})


@login_required
@user_passes_test(is_superadmin)
def kit_edit(request, pk):
    kit = get_object_or_404(KIEMSKit, pk=pk)
    if request.method == 'POST':
        form = KIEMSKitForm(request.POST, instance=kit)
        if form.is_valid():
            form.save()
            messages.success(request, f'KIEMS Kit "{kit.kit_name}" updated successfully!')
            return redirect('superadmin:kit_list')
    else:
        form = KIEMSKitForm(instance=kit)
    return render(request, 'superadmin/kit_form.html', {'form': form, 'title': 'Edit KIEMS Kit'})


@login_required
@user_passes_test(is_superadmin)
@require_POST
def kit_delete(request, pk):
    kit = get_object_or_404(KIEMSKit, pk=pk)
    kit_name = kit.kit_name
    kit.delete()
    messages.success(request, f'KIEMS Kit "{kit_name}" deleted successfully!')
    return redirect('superadmin:kit_list')


# ==================== DAILY ENTRY CRUD ====================

@login_required
@user_passes_test(is_superadmin)
def entry_list(request):
    entries = DailyKIEMSEntry.objects.select_related(
        'kiems_kit', 'phase', 'ward', 'vra'
    ).all()

    form = DailyEntryFilterForm(request.GET or None)
    filter_params = {}

    if form.is_valid():
        if form.cleaned_data.get('phase'):
            entries = entries.filter(phase=form.cleaned_data['phase'])
            filter_params['phase'] = form.cleaned_data['phase'].id
        if form.cleaned_data.get('ward'):
            entries = entries.filter(ward=form.cleaned_data['ward'])
            filter_params['ward'] = form.cleaned_data['ward'].id
        if form.cleaned_data.get('kit'):
            entries = entries.filter(kiems_kit=form.cleaned_data['kit'])
            filter_params['kit'] = form.cleaned_data['kit'].id
        if form.cleaned_data.get('vra'):
            entries = entries.filter(vra=form.cleaned_data['vra'])
            filter_params['vra'] = form.cleaned_data['vra'].id
        if form.cleaned_data.get('date_from'):
            entries = entries.filter(entry_date__gte=form.cleaned_data['date_from'])
            filter_params['date_from'] = form.cleaned_data['date_from'].isoformat()
        if form.cleaned_data.get('date_to'):
            entries = entries.filter(entry_date__lte=form.cleaned_data['date_to'])
            filter_params['date_to'] = form.cleaned_data['date_to'].isoformat()
        if form.cleaned_data.get('uploaded') == 'True':
            entries = entries.filter(uploaded=True)
            filter_params['uploaded'] = 'True'
        elif form.cleaned_data.get('uploaded') == 'False':
            entries = entries.filter(uploaded=False)
            filter_params['uploaded'] = 'False'

    total_registered = entries.aggregate(Sum('total_registered'))['total_registered__sum'] or 0
    total_male = entries.aggregate(Sum('registered_male'))['registered_male__sum'] or 0
    total_female = entries.aggregate(Sum('registered_female'))['registered_female__sum'] or 0
    total_transferred = entries.aggregate(Sum('total_transferred'))['total_transferred__sum'] or 0
    total_updated = entries.aggregate(Sum('total_updated'))['total_updated__sum'] or 0

    today = timezone.now().date()
    today_entries = DailyKIEMSEntry.objects.filter(entry_date=today)
    today_stats = {
        'total_entries': today_entries.count(),
        'unique_kits': today_entries.values('kiems_kit').distinct().count(),
        'total_registered': today_entries.aggregate(Sum('total_registered'))['total_registered__sum'] or 0,
        'registered_male': today_entries.aggregate(Sum('registered_male'))['registered_male__sum'] or 0,
        'registered_female': today_entries.aggregate(Sum('registered_female'))['registered_female__sum'] or 0,
        'total_transferred': today_entries.aggregate(Sum('total_transferred'))['total_transferred__sum'] or 0,
        'total_updated': today_entries.aggregate(Sum('total_updated'))['total_updated__sum'] or 0,
    }

    paginator = Paginator(entries, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        'page_obj': page_obj,
        'form': form,
        'is_filtered': bool(request.GET and any(request.GET.values())),
        'total_registered': total_registered,
        'total_male': total_male,
        'total_female': total_female,
        'total_transferred': total_transferred,
        'total_updated': total_updated,
        'today_stats': today_stats,
        'filter_params': filter_params,
    }
    return render(request, 'superadmin/entry_list.html', context)


@login_required
@user_passes_test(is_superadmin)
def entry_create(request):
    if request.method == 'POST':
        form = DailyKIEMSEntryForm(request.POST)
        if form.is_valid():
            entry = form.save()
            messages.success(request, 'Daily entry created successfully!')
            return redirect('superadmin:entry_list')
    else:
        form = DailyKIEMSEntryForm()
    return render(request, 'superadmin/entry_form.html', {'form': form, 'title': 'Create Daily Entry'})


@login_required
@user_passes_test(is_superadmin)
def entry_edit(request, pk):
    entry = get_object_or_404(DailyKIEMSEntry, pk=pk)
    if request.method == 'POST':
        form = DailyKIEMSEntryForm(request.POST, instance=entry)
        if form.is_valid():
            form.save()
            messages.success(request, 'Daily entry updated successfully!')
            return redirect('superadmin:entry_list')
    else:
        form = DailyKIEMSEntryForm(instance=entry)
    return render(request, 'superadmin/entry_form.html', {'form': form, 'title': 'Edit Daily Entry'})


@login_required
@user_passes_test(is_superadmin)
@require_POST
def entry_delete(request, pk):
    entry = get_object_or_404(DailyKIEMSEntry, pk=pk)
    entry.delete()
    messages.success(request, 'Daily entry deleted successfully!')
    return redirect('superadmin:entry_list')


# ==================== IMPORT/EXPORT ====================

@login_required
@user_passes_test(is_superadmin)
@require_GET
def export_data(request):
    form = ExportForm(request.GET or None)

    if not form.is_valid():
        messages.error(request, 'Invalid export parameters')
        return redirect('superadmin:entry_list')

    model_type = form.cleaned_data['model_type']
    export_format = form.cleaned_data['format']

    filter_params = {}
    if request.GET.get('phase'):
        filter_params['phase'] = request.GET.get('phase')
    if request.GET.get('ward'):
        filter_params['ward'] = request.GET.get('ward')
    if request.GET.get('kit'):
        filter_params['kit'] = request.GET.get('kit')
    if request.GET.get('vra'):
        filter_params['vra'] = request.GET.get('vra')
    if request.GET.get('date_from'):
        filter_params['date_from'] = request.GET.get('date_from')
    if request.GET.get('date_to'):
        filter_params['date_to'] = request.GET.get('date_to')
    if request.GET.get('uploaded'):
        filter_params['uploaded'] = request.GET.get('uploaded')

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"export_{model_type}_{timestamp}"

    model_map = {
        'ward': {
            'queryset': Ward.objects.all(),
            'headers': ['Name', 'Code'],
            'rows': lambda q: [[w.name, w.code] for w in q]
        },
        'vra': {
            'queryset': VRA.objects.select_related('ward').all(),
            'headers': ['Name', 'Ward', 'Active', 'Device Token'],
            'rows': lambda q: [[v.name, v.ward.name, v.active, v.device_token] for v in q]
        },
        'clerk': {
            'queryset': Clerk.objects.select_related('ward').all(),
            'headers': ['Name', 'Ward', 'Active'],
            'rows': lambda q: [[c.name, c.ward.name, c.active] for c in q]
        },
        'kiemskit': {
            'queryset': KIEMSKit.objects.select_related('ward').prefetch_related('assigned_clerks').all(),
            'headers': ['Kit Name', 'Serial No', 'Status', 'Ward', 'Assigned Clerks'],
            'rows': lambda q: [[k.kit_name, k.serial_no, 'Active' if k.status else 'Inactive',
                                k.ward.name, ', '.join([c.name for c in k.assigned_clerks.all()])] for k in q]
        },
        'phase': {
            'queryset': Phase.objects.all(),
            'headers': ['Name', 'Start Date', 'End Date', 'Active'],
            'rows': lambda q: [[p.name, p.start_date, p.end_date, p.active] for p in q]
        },
        'entry': {
            'queryset': DailyKIEMSEntry.objects.select_related(
                'kiems_kit', 'phase', 'ward', 'vra'
            ),
            'headers': [
                'Kit', 'Phase', 'Ward', 'VRA', 'Date', 'Venue',
                'Male', 'Female', 'Other', 'Total Registered',
                'Transferred', 'Deleted', 'Uploaded'
            ],
            'rows': lambda q: [[
                e.kiems_kit.kit_name,
                e.phase.name,
                e.ward.name,
                e.vra.name,
                e.entry_date,
                e.venue,
                e.registered_male,
                e.registered_female,
                e.registered_other,
                e.total_registered,
                e.total_transferred,
                e.total_updated,
                'Yes' if e.uploaded else 'No'
            ] for e in q]
        }
    }

    data = model_map.get(model_type)
    if not data:
        messages.error(request, 'Invalid model type')
        return redirect('superadmin:entry_list')

    queryset = data['queryset']
    if model_type == 'entry' and filter_params:
        if filter_params.get('phase'):
            queryset = queryset.filter(phase_id=filter_params['phase'])
        if filter_params.get('ward'):
            queryset = queryset.filter(ward_id=filter_params['ward'])
        if filter_params.get('kit'):
            queryset = queryset.filter(kiems_kit_id=filter_params['kit'])
        if filter_params.get('vra'):
            queryset = queryset.filter(vra_id=filter_params['vra'])
        if filter_params.get('date_from'):
            queryset = queryset.filter(entry_date__gte=filter_params['date_from'])
        if filter_params.get('date_to'):
            queryset = queryset.filter(entry_date__lte=filter_params['date_to'])
        if filter_params.get('uploaded') == 'True':
            queryset = queryset.filter(uploaded=True)
        elif filter_params.get('uploaded') == 'False':
            queryset = queryset.filter(uploaded=False)

    headers = data['headers']
    rows = data['rows'](queryset)

    if filter_params:
        filename += "_filtered"

    if export_format == 'csv':
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{filename}.csv"'
        writer = csv.writer(response)
        writer.writerow(headers)
        writer.writerows(rows)
        return response

    elif export_format == 'xlsx':
        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = f'attachment; filename="{filename}.xlsx"'
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(headers)
        for row in rows:
            ws.append(row)
        wb.save(response)
        return response


@login_required
@user_passes_test(is_superadmin)
def import_data(request):
    if request.method == 'POST':
        form = ImportForm(request.POST, request.FILES)
        if form.is_valid():
            file = request.FILES['file']
            model_type = form.cleaned_data['model_type']

            try:
                if file.name.endswith('.csv'):
                    decoded = file.read().decode('utf-8')
                    reader = csv.DictReader(io.StringIO(decoded))
                    rows = list(reader)
                elif file.name.endswith(('.xlsx', '.xls')):
                    wb = openpyxl.load_workbook(file)
                    ws = wb.active
                    headers = [cell.value for cell in ws[1]]
                    rows = []
                    for row in ws.iter_rows(min_row=2, values_only=True):
                        rows.append(dict(zip(headers, row)))
                else:
                    messages.error(request, 'Unsupported file format. Please use CSV or Excel.')
                    return redirect('superadmin:import_data')

                success_count = 0
                error_count = 0

                if model_type == 'ward':
                    for row in rows:
                        try:
                            Ward.objects.get_or_create(
                                name=row.get('Name') or row.get('name'),
                                defaults={'code': row.get('Code') or row.get('code', '')}
                            )
                            success_count += 1
                        except Exception:
                            error_count += 1
                elif model_type == 'vra':
                    for row in rows:
                        try:
                            ward = Ward.objects.filter(name=row.get('Ward') or row.get('ward')).first()
                            if ward:
                                VRA.objects.get_or_create(
                                    name=row.get('Name') or row.get('name'),
                                    ward=ward,
                                    defaults={
                                        'active': row.get('Active', 'True') in ['True', 'true', '1', 'Yes'],
                                        'device_token': row.get('Device Token') or row.get('device_token', ''),
                                    }
                                )
                                success_count += 1
                            else:
                                error_count += 1
                        except Exception:
                            error_count += 1
                elif model_type == 'clerk':
                    for row in rows:
                        try:
                            ward = Ward.objects.filter(name=row.get('Ward') or row.get('ward')).first()
                            if ward:
                                Clerk.objects.get_or_create(
                                    name=row.get('Name') or row.get('name'),
                                    ward=ward,
                                    defaults={'active': row.get('Active', 'True') in ['True', 'true', '1', 'Yes']}
                                )
                                success_count += 1
                            else:
                                error_count += 1
                        except Exception:
                            error_count += 1
                elif model_type == 'kiemskit':
                    for row in rows:
                        try:
                            ward = Ward.objects.filter(name=row.get('Ward') or row.get('ward')).first()
                            if ward:
                                KIEMSKit.objects.get_or_create(
                                    serial_no=row.get('Serial No') or row.get('serial_no'),
                                    defaults={
                                        'kit_name': row.get('Kit Name') or row.get('kit_name'),
                                        'status': row.get('Status', 'Active') in ['Active', 'active', 'True', 'true',
                                                                                  '1'],
                                        'ward': ward,
                                    }
                                )
                                success_count += 1
                            else:
                                error_count += 1
                        except Exception:
                            error_count += 1
                elif model_type == 'phase':
                    for row in rows:
                        try:
                            Phase.objects.get_or_create(
                                name=row.get('Name') or row.get('name'),
                                defaults={
                                    'start_date': row.get('Start Date') or row.get('start_date'),
                                    'end_date': row.get('End Date') or row.get('end_date'),
                                    'active': row.get('Active', 'True') in ['True', 'true', '1', 'Yes'],
                                }
                            )
                            success_count += 1
                        except Exception:
                            error_count += 1
                elif model_type == 'entry':
                    for row in rows:
                        try:
                            kit = KIEMSKit.objects.filter(kit_name=row.get('Kit') or row.get('kit')).first()
                            phase = Phase.objects.filter(name=row.get('Phase') or row.get('phase')).first()
                            ward = Ward.objects.filter(name=row.get('Ward') or row.get('ward')).first()
                            vra = VRA.objects.filter(name=row.get('VRA') or row.get('vra'), ward=ward).first()

                            if kit and phase and ward and vra:
                                DailyKIEMSEntry.objects.get_or_create(
                                    kiems_kit=kit,
                                    phase=phase,
                                    entry_date=row.get('Date') or row.get('date'),
                                    vra=vra,
                                    defaults={
                                        'ward': ward,
                                        'venue': row.get('Venue') or row.get('venue', ''),
                                        'total_registered': int(row.get('Registered', 0)),
                                        'total_transferred': int(row.get('Transferred', 0)),
                                        'total_updated': int(row.get('Deleted', 0)),
                                        'uploaded': row.get('Uploaded', 'False') in ['True', 'true', '1', 'Yes'],
                                    }
                                )
                                success_count += 1
                            else:
                                error_count += 1
                        except Exception:
                            error_count += 1

                messages.success(request, f'Import completed: {success_count} records imported, {error_count} errors.')
                return redirect('superadmin:dashboard')

            except Exception as e:
                messages.error(request, f'Import failed: {str(e)}')
                return redirect('superadmin:import_data')
    else:
        form = ImportForm()

    return render(request, 'superadmin/import_data.html', {'form': form})


# ==================== CHART DATA API ====================

@login_required
@user_passes_test(is_superadmin)
@require_GET
def get_chart_data(request):
    chart_type = request.GET.get('type', 'daily')
    days = int(request.GET.get('days', 30))

    data = {'labels': [], 'datasets': []}

    if chart_type == 'daily':
        end_date = timezone.now().date()
        start_date = end_date - timedelta(days=days)

        daily_data = DailyKIEMSEntry.objects.filter(
            entry_date__gte=start_date,
            entry_date__lte=end_date
        ).values('entry_date').annotate(
            registered=Sum('total_registered'),
            transferred=Sum('total_transferred'),
            deleted=Sum('total_updated')
        ).order_by('entry_date')

        data['labels'] = [d['entry_date'].strftime('%Y-%m-%d') for d in daily_data]
        data['datasets'] = [
            {
                'label': 'Registered',
                'data': [d['registered'] or 0 for d in daily_data],
                'borderColor': '#34c759',
                'backgroundColor': 'rgba(52, 199, 89, 0.1)',
                'fill': True
            },
            {
                'label': 'Transferred',
                'data': [d['transferred'] or 0 for d in daily_data],
                'borderColor': '#5856d6',
                'backgroundColor': 'rgba(88, 86, 214, 0.1)',
                'fill': True
            },
            {
                'label': 'Deleted',
                'data': [d['deleted'] or 0 for d in daily_data],
                'borderColor': '#ff3b30',
                'backgroundColor': 'rgba(255, 59, 48, 0.1)',
                'fill': True
            }
        ]

    elif chart_type == 'phase':
        phase_data = DailyKIEMSEntry.objects.values('phase__name').annotate(
            total=Sum('total_registered')
        ).order_by('-total')

        data['labels'] = [d['phase__name'] for d in phase_data]
        data['datasets'] = [
            {
                'label': 'Registrations by Phase',
                'data': [d['total'] or 0 for d in phase_data],
                'backgroundColor': ['#34c759', '#5856d6', '#ff9500', '#ff3b30', '#ff2d55', '#af52de']
            }
        ]

    elif chart_type == 'ward':
        ward_data = DailyKIEMSEntry.objects.values('ward__name').annotate(
            total=Sum('total_registered')
        ).order_by('-total')[:10]

        data['labels'] = [d['ward__name'] or 'Unknown' for d in ward_data]
        data['datasets'] = [
            {
                'label': 'Registrations by Ward',
                'data': [d['total'] or 0 for d in ward_data],
                'backgroundColor': 'rgba(144, 238, 144, 0.6)',
                'borderColor': '#90EE90',
                'borderWidth': 1
            }
        ]

    elif chart_type == 'kit':
        kit_data = DailyKIEMSEntry.objects.values('kiems_kit__kit_name').annotate(
            total=Sum('total_registered')
        ).order_by('-total')[:10]

        data['labels'] = [d['kiems_kit__kit_name'] for d in kit_data]
        data['datasets'] = [
            {
                'label': 'Registrations by Kit',
                'data': [d['total'] or 0 for d in kit_data],
                'backgroundColor': ['#34c759', '#5856d6', '#ff9500', '#ff3b30', '#af52de',
                                    '#ff2d55', '#5ac8fa', '#ffcc00', '#ff6b6b', '#6c5ce7']
            }
        ]

    return JsonResponse(data)


@login_required
@user_passes_test(is_superadmin)
@require_GET
def get_clerks_by_ward(request):
    ward_id = request.GET.get('ward_id')
    if not ward_id:
        return JsonResponse({'clerks': [], 'error': 'No ward specified'}, status=400)

    try:
        clerks = Clerk.objects.filter(ward_id=ward_id, active=True).values('id', 'name').order_by('name')
        return JsonResponse({'clerks': list(clerks)})
    except Exception as e:
        return JsonResponse({'clerks': [], 'error': str(e)}, status=500)


@login_required
@user_passes_test(is_superadmin)
@require_GET
def get_vras_by_ward(request):
    ward_id = request.GET.get('ward_id')
    if not ward_id:
        return JsonResponse({'vras': [], 'error': 'No ward specified'}, status=400)

    try:
        vras = VRA.objects.filter(ward_id=ward_id, active=True).values('id', 'name').order_by('name')
        return JsonResponse({'vras': list(vras)})
    except Exception as e:
        return JsonResponse({'vras': [], 'error': str(e)}, status=500)


# ==================== REPORT GENERATION ====================

def _parse_report_filters(request):
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    phase_id = request.GET.get('phase')
    ward_id = request.GET.get('ward')
    kit_id = request.GET.get('kit')
    vra_id = request.GET.get('vra')
    uploaded = request.GET.get('uploaded')

    today = timezone.now().date()
    default_start = today - timedelta(days=30)

    try:
        date_from = datetime.strptime(date_from, '%Y-%m-%d').date() if date_from else default_start
    except (ValueError, TypeError):
        date_from = default_start

    try:
        date_to = datetime.strptime(date_to, '%Y-%m-%d').date() if date_to else today
    except (ValueError, TypeError):
        date_to = today

    return {
        'date_from': date_from,
        'date_to': date_to,
        'phase_id': phase_id,
        'ward_id': ward_id,
        'kit_id': kit_id,
        'vra_id': vra_id,
        'uploaded': uploaded,
    }


def _filter_labels(date_from, date_to, phase_id, ward_id, kit_id, vra_id, uploaded):
    labels = [f"{date_from.strftime('%d %b %Y')} - {date_to.strftime('%d %b %Y')}"]

    if phase_id:
        name = Phase.objects.filter(id=phase_id).values_list('name', flat=True).first()
        if name:
            labels.append(f"Phase: {name}")

    if ward_id:
        name = Ward.objects.filter(id=ward_id).values_list('name', flat=True).first()
        if name:
            labels.append(f"Ward: {name}")

    if kit_id:
        name = KIEMSKit.objects.filter(id=kit_id).values_list('kit_name', flat=True).first()
        if name:
            labels.append(f"Kit: {name}")

    if vra_id:
        name = VRA.objects.filter(id=vra_id).values_list('name', flat=True).first()
        if name:
            labels.append(f"VRA: {name}")

    if uploaded in ('True', 'False'):
        labels.append(f"Status: {'Uploaded' if uploaded == 'True' else 'Pending'}")

    return labels


def generate_report_html(request):
    f = _parse_report_filters(request)
    date_from, date_to = f['date_from'], f['date_to']
    phase_id, ward_id, kit_id, vra_id, uploaded = (
        f['phase_id'], f['ward_id'], f['kit_id'], f['vra_id'], f['uploaded']
    )

    entries = DailyKIEMSEntry.objects.filter(
        entry_date__gte=date_from,
        entry_date__lte=date_to
    ).select_related('kiems_kit', 'phase', 'ward', 'vra').order_by('entry_date')

    if phase_id:
        entries = entries.filter(phase_id=phase_id)
    if ward_id:
        entries = entries.filter(ward_id=ward_id)
    if kit_id:
        entries = entries.filter(kiems_kit_id=kit_id)
    if vra_id:
        entries = entries.filter(vra_id=vra_id)
    if uploaded == 'True':
        entries = entries.filter(uploaded=True)
    elif uploaded == 'False':
        entries = entries.filter(uploaded=False)

    total_registered = entries.aggregate(Sum('total_registered'))['total_registered__sum'] or 0
    total_male = entries.aggregate(Sum('registered_male'))['registered_male__sum'] or 0
    total_female = entries.aggregate(Sum('registered_female'))['registered_female__sum'] or 0
    total_transferred = entries.aggregate(Sum('total_transferred'))['total_transferred__sum'] or 0
    total_entries = entries.count()

    ward_summary = entries.values('ward__name').annotate(
        registered=Sum('total_registered'), male=Sum('registered_male'),
        female=Sum('registered_female'), transferred=Sum('total_transferred'),
        count=Count('id'),
    ).order_by('-registered')

    phase_summary = entries.values('phase__name').annotate(
        registered=Sum('total_registered'), male=Sum('registered_male'),
        female=Sum('registered_female'), transferred=Sum('total_transferred'),
        count=Count('id'),
    ).order_by('-registered')

    kit_summary = entries.values('kiems_kit__kit_name').annotate(
        registered=Sum('total_registered'), male=Sum('registered_male'),
        female=Sum('registered_female'), count=Count('id'),
    ).order_by('-registered')[:20]

    filter_labels = _filter_labels(date_from, date_to, phase_id, ward_id, kit_id, vra_id, uploaded)
    scope = " | ".join(filter_labels[1:]) or "All wards, phases and kits"

    html_string = render_to_string('superadmin/report_template.html', {
        'total_entries': total_entries,
        'total_registered': total_registered,
        'total_male': total_male,
        'total_female': total_female,
        'total_transferred': total_transferred,
        'ward_summary': ward_summary,
        'phase_summary': phase_summary,
        'kit_summary': kit_summary,
        'filter_labels': filter_labels,
        'scope': scope,
        'generated_at': timezone.now().strftime('%d %b %Y, %H:%M'),
        'date_from': date_from,
        'date_to': date_to,
        'brand_logo_url': getattr(settings, 'BRAND_LOGO_URL', '')
    })

    return HttpResponse(html_string)


def generate_report_pdf(request, as_attachment=False):
    try:
        if not WEASYPRINT_AVAILABLE:
            messages.error(request, 'PDF generation is not available. Please install weasyprint.')
            return redirect('superadmin:entry_list')

        html_response = generate_report_html(request)
        html_string = html_response.content.decode('utf-8')

        f = _parse_report_filters(request)
        date_from, date_to = f['date_from'], f['date_to']

        pdf_bytes = weasyprint.HTML(string=html_string, base_url=request.build_absolute_uri()).write_pdf()

        response = HttpResponse(pdf_bytes, content_type='application/pdf')
        filename = f"KIEMS_Report_{date_from.strftime('%Y%m%d')}_{date_to.strftime('%Y%m%d')}.pdf"

        if as_attachment:
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
        else:
            response['Content-Disposition'] = f'inline; filename="{filename}"'

        return response
    except Exception as e:
        messages.error(request, f'PDF generation failed: {str(e)}')
        return redirect('superadmin:entry_list')


@login_required
@user_passes_test(is_superadmin)
def generate_report(request):
    return generate_report_pdf(request, as_attachment=True)


@login_required
@user_passes_test(is_superadmin)
def generate_report_preview(request):
    return generate_report_html(request)


@login_required
@user_passes_test(is_superadmin)
def generate_report_download(request):
    return generate_report_pdf(request, as_attachment=True)


# ==================== KIT REPORT API ====================

@login_required
@user_passes_test(is_superadmin)
def kit_report_api(request, kit_id):
    kit = get_object_or_404(KIEMSKit, id=kit_id)
    entries = DailyKIEMSEntry.objects.filter(kiems_kit=kit).select_related('vra').order_by('-entry_date')

    data = {
        "kit_name": kit.kit_name,
        "serial_no": kit.serial_no,
        "clerks": list(kit.assigned_clerks.values_list('name', flat=True)),
        "vras": list(entries.values_list('vra__name', flat=True).distinct()),
        "entries": [
            {
                "date": e.entry_date.strftime("%b %d, %Y"),
                "vra": e.vra.name,
                "male": e.registered_male,
                "female": e.registered_female,
                "total": e.total_registered,
                "transferred": e.total_transferred,
                "uploaded": e.uploaded,
            }
            for e in entries
        ],
        "totals": {
            "male": sum(e.registered_male for e in entries),
            "female": sum(e.registered_female for e in entries),
            "total": sum(e.total_registered for e in entries),
            "transferred": sum(e.total_transferred for e in entries),
        },
    }
    return JsonResponse(data)


# ==================== FILTERED PREVIEW ====================

@login_required
@user_passes_test(is_superadmin)
def filtered_preview(request):
    entries = DailyKIEMSEntry.objects.select_related(
        'kiems_kit', 'phase', 'ward', 'vra'
    ).all()

    filter_params = {}
    query_string = ""

    if request.GET.get('phase'):
        entries = entries.filter(phase_id=request.GET.get('phase'))
        filter_params['phase'] = request.GET.get('phase')

    if request.GET.get('ward'):
        entries = entries.filter(ward_id=request.GET.get('ward'))
        filter_params['ward'] = request.GET.get('ward')

    if request.GET.get('kit'):
        entries = entries.filter(kiems_kit_id=request.GET.get('kit'))
        filter_params['kit'] = request.GET.get('kit')

    if request.GET.get('vra'):
        entries = entries.filter(vra_id=request.GET.get('vra'))
        filter_params['vra'] = request.GET.get('vra')

    if request.GET.get('date_from'):
        entries = entries.filter(entry_date__gte=request.GET.get('date_from'))
        filter_params['date_from'] = request.GET.get('date_from')

    if request.GET.get('date_to'):
        entries = entries.filter(entry_date__lte=request.GET.get('date_to'))
        filter_params['date_to'] = request.GET.get('date_to')

    if request.GET.get('uploaded') == 'True':
        entries = entries.filter(uploaded=True)
        filter_params['uploaded'] = 'True'
    elif request.GET.get('uploaded') == 'False':
        entries = entries.filter(uploaded=False)
        filter_params['uploaded'] = 'False'

    query_string = '&'.join([f'{k}={v}' for k, v in filter_params.items()])

    totals = {
        'registered': entries.aggregate(Sum('total_registered'))['total_registered__sum'] or 0,
        'male': entries.aggregate(Sum('registered_male'))['registered_male__sum'] or 0,
        'female': entries.aggregate(Sum('registered_female'))['registered_female__sum'] or 0,
        'transferred': entries.aggregate(Sum('total_transferred'))['total_transferred__sum'] or 0,
        'updated': entries.aggregate(Sum('total_updated'))['total_updated__sum'] or 0,
    }

    filter_parts = []
    if filter_params.get('phase'):
        phase = Phase.objects.filter(id=filter_params['phase']).first()
        if phase: filter_parts.append(f'Phase: {phase.name}')
    if filter_params.get('ward'):
        ward = Ward.objects.filter(id=filter_params['ward']).first()
        if ward: filter_parts.append(f'Ward: {ward.name}')
    if filter_params.get('kit'):
        kit = KIEMSKit.objects.filter(id=filter_params['kit']).first()
        if kit: filter_parts.append(f'Kit: {kit.kit_name}')
    if filter_params.get('vra'):
        vra = VRA.objects.filter(id=filter_params['vra']).first()
        if vra: filter_parts.append(f'VRA: {vra.name}')
    if filter_params.get('date_from'):
        filter_parts.append(f'From: {filter_params["date_from"]}')
    if filter_params.get('date_to'):
        filter_parts.append(f'To: {filter_params["date_to"]}')
    if filter_params.get('uploaded') == 'True':
        filter_parts.append('Status: Uploaded')
    elif filter_params.get('uploaded') == 'False':
        filter_parts.append('Status: Pending')

    filter_summary = ' &middot; '.join(filter_parts) if filter_parts else 'No filters applied - showing all entries'

    context = {
        'entries': entries.order_by('-entry_date'),
        'totals': totals,
        'filter_summary': filter_summary,
        'query_string': query_string,
        'generated_at': timezone.now().strftime('%Y-%m-%d %H:%M'),
    }

    return render(request, 'superadmin/filtered_preview.html', context)


# ==================== PERFORMANCE DASHBOARD ====================

@login_required
@user_passes_test(is_superadmin)
def performance_dashboard(request):
    period = request.GET.get('period', 'weekly')

    today = timezone.now().date()
    date_from = None

    if period == 'daily':
        date_from = today
        period_label = 'Today'
    elif period == 'weekly':
        date_from = today - timedelta(days=7)
        period_label = 'Last 7 Days'
    elif period == 'monthly':
        date_from = today - timedelta(days=30)
        period_label = 'Last 30 Days'
    else:
        period_label = 'All Time'

    entries = DailyKIEMSEntry.objects.select_related('kiems_kit', 'ward')

    if date_from:
        entries = entries.filter(entry_date__gte=date_from)

    kit_data = entries.values('kiems_kit_id', 'kiems_kit__kit_name', 'kiems_kit__serial_no', 'ward__name').annotate(
        total_registered=Sum('total_registered'),
        total_male=Sum('registered_male'),
        total_female=Sum('registered_female'),
        entry_count=Count('id'),
        total_transferred=Sum('total_transferred'),
    ).order_by('-total_registered')

    total_registered = sum(k['total_registered'] or 0 for k in kit_data)
    max_registered = kit_data[0]['total_registered'] if kit_data else 0

    total_male = sum(k['total_male'] or 0 for k in kit_data)
    total_female = sum(k['total_female'] or 0 for k in kit_data)
    gender_total = total_male + total_female

    kit_rankings = []
    for kit in kit_data:
        total = kit['total_registered'] or 0
        kit_rankings.append({
            'kit_name': kit['kiems_kit__kit_name'],
            'serial_no': kit['kiems_kit__serial_no'],
            'ward_name': kit['ward__name'],
            'total_registered': total,
            'total_male': kit['total_male'] or 0,
            'total_female': kit['total_female'] or 0,
            'entry_count': kit['entry_count'] or 0,
            'total_transferred': kit['total_transferred'] or 0,
            'pct_of_total': (total / total_registered * 100) if total_registered > 0 else 0,
            'pct_of_max': (total / max_registered * 100) if max_registered > 0 else 0,
            'avg_per_entry': (total / kit['entry_count']) if kit['entry_count'] > 0 else 0,
        })

    top_kit = kit_rankings[0] if kit_rankings else None

    daily_start = today - timedelta(days=7)
    daily_entries = DailyKIEMSEntry.objects.filter(entry_date__gte=daily_start).values('entry_date').annotate(
        entry_count=Count('id'),
        male=Sum('registered_male'),
        female=Sum('registered_female'),
        total_registered=Sum('total_registered'),
        transferred=Sum('total_transferred'),
    ).order_by('-entry_date')

    daily_performance = []
    for day in daily_entries:
        top_kit_day = DailyKIEMSEntry.objects.filter(entry_date=day['entry_date']).values(
            'kiems_kit__kit_name').annotate(
            total=Sum('total_registered')
        ).order_by('-total').first()

        daily_performance.append({
            'date': day['entry_date'],
            'entry_count': day['entry_count'] or 0,
            'male': day['male'] or 0,
            'female': day['female'] or 0,
            'total_registered': day['total_registered'] or 0,
            'transferred': day['transferred'] or 0,
            'top_kit': top_kit_day['kiems_kit__kit_name'] if top_kit_day else None,
            'top_kit_count': top_kit_day['total'] if top_kit_day else 0,
        })

    total_kits = kit_data.count()
    avg_per_kit = total_registered / total_kits if total_kits > 0 else 0

    gender_male_pct = int((total_male / gender_total * 100)) if gender_total > 0 else 50
    gender_female_pct = int((total_female / gender_total * 100)) if gender_total > 0 else 50

    gender_ratio = f"{gender_male_pct}/{gender_female_pct}" if gender_total > 0 else "0/0"

    context = {
        'period': period,
        'period_label': period_label,
        'kit_rankings': kit_rankings,
        'top_kit': top_kit,
        'total_registered': total_registered,
        'total_kits': total_kits,
        'avg_per_kit': int(avg_per_kit),
        'gender_ratio': gender_ratio,
        'gender_male_pct': gender_male_pct,
        'gender_female_pct': gender_female_pct,
        'daily_performance': daily_performance,
    }

    return render(request, 'superadmin/performance_dashboard.html', context)
