import csv
import io
from datetime import datetime, timedelta

import openpyxl
from django.contrib import messages
from django.contrib.auth import logout, authenticate, login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User
from django.core.paginator import Paginator
from django.db.models import Count, Sum, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.utils import timezone
from django.views.decorators.http import require_POST, require_GET
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Spacer, HRFlowable, Table, TableStyle
from reportlab.platypus.para import Paragraph

from home.models import (
    Ward, VRA, Clerk, KIEMSKit, Phase, DailyKIEMSEntry
)
from .forms import (
    WardForm, VRAForm, ClerkForm, KIEMSKitForm, PhaseForm,
    DailyKIEMSEntryForm, DailyEntryFilterForm, ImportForm, ExportForm
)


# Removed: from .models import AuditLog


def is_superadmin(user):
    return user.is_superuser


def login_view(request):
    """
    Login view for SuperAdmin panel with enhanced security features
    """
    # Redirect if already logged in
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
        print(email)

        # Validate inputs
        if not email or not password:
            messages.error(request, 'Please provide both email and password.')
            return render(request, 'superadmin/login.html', {'email': email})

        # Attempt authentication
        username = User.objects.filter(email=email).first()

        user = authenticate(request, username=username, password=password)

        if user is not None:
            if user.is_superuser:
                # Login successful
                login(request, user)
                messages.success(request, f'Welcome back, {user.get_full_name() or user.username}!')
                return redirect('superadmin:dashboard')
            else:
                messages.error(request, 'You do not have administrator privileges.')
        else:
            # Failed login attempt
            messages.error(request, 'Invalid email or password. Please try again.')
            # Audit logging removed

        return render(request, 'superadmin/login.html', {'email': email})

    # GET request
    return render(request, 'superadmin/login.html')


def logout_view(request):
    """
    Logout view for SuperAdmin panel
    """
    if request.user.is_authenticated:
        # Audit logging removed
        messages.info(request, 'You have been logged out successfully.')
        logout(request)

    return redirect('superadmin:login')


def password_reset_request(request):
    """
    Password reset request view
    """
    if request.method == 'POST':
        email = request.POST.get('email', '').strip()

        if email:
            # Check if user exists with this email
            from django.contrib.auth.models import User
            try:
                user = User.objects.get(email=email)
                if user.is_superuser:
                    # In production, send password reset email
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
    """SuperAdmin Dashboard"""
    active_phase = Phase.objects.filter(active=True).first()

    # Key Metrics
    metrics = {
        'total_wards': Ward.objects.count(),
        'total_vras': VRA.objects.filter(active=True).count(),
        'total_clerks': Clerk.objects.filter(active=True).count(),
        'total_kits': KIEMSKit.objects.filter(status=True).count(),
        'total_phases': Phase.objects.count(),
        'total_entries': DailyKIEMSEntry.objects.count(),
        'total_registered': DailyKIEMSEntry.objects.aggregate(Sum('total_registered'))['total_registered__sum'] or 0,
        'total_transferred': DailyKIEMSEntry.objects.aggregate(Sum('total_transferred'))['total_transferred__sum'] or 0,
        'total_deleted': DailyKIEMSEntry.objects.aggregate(Sum('total_deleted'))['total_deleted__sum'] or 0,
    }

    # Recent entries
    recent_entries = DailyKIEMSEntry.objects.select_related(
        'kiems_kit', 'phase', 'ward', 'vra'
    ).order_by('-created_at')[:10]

    # Today's activity
    today = timezone.now().date()
    today_activity = DailyKIEMSEntry.objects.filter(
        entry_date=today
    ).aggregate(
        registered=Sum('total_registered'),
        transferred=Sum('total_transferred'),
        deleted=Sum('total_deleted')
    )

    # Kits by ward
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
    """List all phases"""
    phases = Phase.objects.all().order_by('-start_date')
    return render(request, 'superadmin/phase_list.html', {'phases': phases})


@login_required
@user_passes_test(is_superadmin)
def phase_create(request):
    """Create a new phase"""
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
    """Edit a phase"""
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
    """Delete a phase"""
    phase = get_object_or_404(Phase, pk=pk)
    phase_name = phase.name
    phase.delete()
    messages.success(request, f'Phase "{phase_name}" deleted successfully!')
    return redirect('superadmin:phase_list')


# ==================== WARD CRUD ====================

@login_required
@user_passes_test(is_superadmin)
def ward_list(request):
    """List all wards"""
    wards = Ward.objects.all().order_by('name')
    return render(request, 'superadmin/ward_list.html', {'wards': wards})


@login_required
@user_passes_test(is_superadmin)
def ward_create(request):
    """Create a new ward"""
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
    """Edit a ward"""
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
    """Delete a ward"""
    ward = get_object_or_404(Ward, pk=pk)
    ward_name = ward.name
    ward.delete()
    messages.success(request, f'Ward "{ward_name}" deleted successfully!')
    return redirect('superadmin:ward_list')




# ==================== STAFF CRUD ====================

@login_required
@user_passes_test(is_superadmin)
def staff_list(request):
    """List all clerks and VRAs"""
    clerks = Clerk.objects.select_related('ward').all().order_by('ward__name', 'name')
    vras = VRA.objects.select_related('ward').all().order_by('ward__name', 'name')

    # Combine both with a type indicator
    staff_list = []
    for clerk in clerks:
        staff_list.append({
            'id': clerk.id,
            'type': 'clerk',
            'name': clerk.name,
            'ward': clerk.ward,
            'active': clerk.active,
            'created_at': clerk.created_at,
            'details': {
                'role': 'Clerk',
                'type_label': 'Clerk'
            }
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

    # Sort combined list by ward name, then name
    staff_list.sort(key=lambda x: (x['ward'].name if x['ward'] else '', x['name']))

    # Statistics
    stats = {
        'total_clerks': clerks.count(),
        'total_vras': vras.count(),
        'active_clerks': clerks.filter(active=True).count(),
        'active_vras': vras.filter(active=True).count(),
        'total_staff': staff_list.__len__(),
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
    """Create a new clerk or VRA"""
    staff_type = request.GET.get('type', request.POST.get('type', 'clerk'))

    if request.method == 'POST':
        staff_type = request.POST.get('type', 'clerk')

        # Debug: print what type we're getting
        print(f"Creating staff of type: {staff_type}")

        if staff_type == 'clerk':
            form = ClerkForm(request.POST)
            if form.is_valid():
                clerk = form.save()
                messages.success(request, f'Clerk "{clerk.name}" created successfully!')
                return redirect('superadmin:staff_list')
            else:
                # Print form errors for debugging
                print(f"Clerk form errors: {form.errors}")
        else:  # VRA
            form = VRAForm(request.POST)
            if form.is_valid():
                vra = form.save()
                messages.success(request, f'VRA "{vra.name}" created successfully!')
                return redirect('superadmin:staff_list')
            else:
                # Print form errors for debugging
                print(f"VRA form errors: {form.errors}")
    else:
        # GET request - instantiate the correct form based on type
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
    """Edit a clerk or VRA"""
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
    else:  # VRA
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
def staff_edit(request, pk, staff_type):
    """Edit a clerk or VRA"""
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
    else:  # VRA
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
    """Delete a clerk or VRA"""
    if staff_type == 'clerk':
        staff = get_object_or_404(Clerk, pk=pk)
        staff_name = staff.name
        staff.delete()
        messages.success(request, f'Clerk "{staff_name}" deleted successfully!')
    else:  # VRA
        staff = get_object_or_404(VRA, pk=pk)
        staff_name = staff.name
        staff.delete()
        messages.success(request, f'VRA "{staff_name}" deleted successfully!')

    return redirect('superadmin:staff_list')


@login_required
@user_passes_test(is_superadmin)
def clerk_list_old(request):
    """List all clerks (legacy)"""
    clerks = Clerk.objects.select_related('ward').all().order_by('ward__name', 'name')
    return render(request, 'superadmin/clerk_list.html', {'clerks': clerks})

# ==================== KIEMS KIT CRUD ====================

@login_required
@user_passes_test(is_superadmin)
def kit_list(request):
    """List all KIEMS kits"""
    kits = KIEMSKit.objects.select_related('ward').prefetch_related('assigned_clerks').all().order_by('ward__name',
                                                                                                      'kit_name')
    return render(request, 'superadmin/kit_list.html', {'kits': kits})


@login_required
@user_passes_test(is_superadmin)
def kit_create(request):
    """Create a new KIEMS kit"""
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
    """Edit a KIEMS kit"""
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
    """Delete a KIEMS kit"""
    kit = get_object_or_404(KIEMSKit, pk=pk)
    kit_name = kit.kit_name
    kit.delete()
    messages.success(request, f'KIEMS Kit "{kit_name}" deleted successfully!')
    return redirect('superadmin:kit_list')


# ==================== DAILY ENTRY CRUD ====================

@login_required
@user_passes_test(is_superadmin)
def entry_list(request):
    """List all daily entries with filters"""
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

    # Calculate totals with gender breakdown
    total_registered = entries.aggregate(Sum('total_registered'))['total_registered__sum'] or 0
    total_male = entries.aggregate(Sum('registered_male'))['registered_male__sum'] or 0
    total_female = entries.aggregate(Sum('registered_female'))['registered_female__sum'] or 0
    total_transferred = entries.aggregate(Sum('total_transferred'))['total_transferred__sum'] or 0
    total_deleted = entries.aggregate(Sum('total_deleted'))['total_deleted__sum'] or 0

    # Today's statistics with gender breakdown
    today = timezone.now().date()
    today_entries = DailyKIEMSEntry.objects.filter(entry_date=today)
    today_stats = {
        'total_entries': today_entries.count(),
        'unique_kits': today_entries.values('kiems_kit').distinct().count(),
        'total_registered': today_entries.aggregate(Sum('total_registered'))['total_registered__sum'] or 0,
        'registered_male': today_entries.aggregate(Sum('registered_male'))['registered_male__sum'] or 0,
        'registered_female': today_entries.aggregate(Sum('registered_female'))['registered_female__sum'] or 0,
        'total_transferred': today_entries.aggregate(Sum('total_transferred'))['total_transferred__sum'] or 0,
        'total_deleted': today_entries.aggregate(Sum('total_deleted'))['total_deleted__sum'] or 0,
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
        'total_deleted': total_deleted,
        'today_stats': today_stats,
        'filter_params': filter_params,
    }
    return render(request, 'superadmin/entry_list.html', context)


@login_required
@user_passes_test(is_superadmin)
def entry_create(request):
    """Create a new daily entry"""
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
    """Edit a daily entry"""
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
    """Delete a daily entry"""
    entry = get_object_or_404(DailyKIEMSEntry, pk=pk)
    entry.delete()
    messages.success(request, 'Daily entry deleted successfully!')
    return redirect('superadmin:entry_list')


# ==================== IMPORT/EXPORT ====================

@login_required
@user_passes_test(is_superadmin)
@require_GET
def export_data(request):
    """Export data in CSV or Excel format with filters"""
    form = ExportForm(request.GET or None)

    if not form.is_valid():
        messages.error(request, 'Invalid export parameters')
        return redirect('superadmin:entry_list')

    model_type = form.cleaned_data['model_type']
    export_format = form.cleaned_data['format']

    # Get filter parameters from request
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

    # Map model types to data with filter support
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
                e.total_deleted,
                'Yes' if e.uploaded else 'No'
            ] for e in q]
        }
    }

    data = model_map.get(model_type)
    if not data:
        messages.error(request, 'Invalid model type')
        return redirect('superadmin:entry_list')

    # Apply filters for entry model
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

    # Add filter info to filename
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
    """Import data from CSV or Excel file"""
    if request.method == 'POST':
        form = ImportForm(request.POST, request.FILES)
        if form.is_valid():
            file = request.FILES['file']
            model_type = form.cleaned_data['model_type']

            try:
                # Parse file
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

                # Import logic based on model type
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
                                        'total_deleted': int(row.get('Deleted', 0)),
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
    """API endpoint for chart data"""
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
            deleted=Sum('total_deleted')
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
def generate_report(request):
    """Generate comprehensive PDF report with gender breakdown"""

    # Get filter parameters from request
    date_from = request.GET.get('date_from')
    date_to = request.GET.get('date_to')
    phase_id = request.GET.get('phase')
    ward_id = request.GET.get('ward')
    kit_id = request.GET.get('kit')
    vra_id = request.GET.get('vra')
    uploaded = request.GET.get('uploaded')

    # Default to today if no dates provided
    today = timezone.now().date()
    if not date_from:
        date_from = today - timezone.timedelta(days=30)
    else:
        try:
            date_from = timezone.datetime.strptime(date_from, '%Y-%m-%d').date()
        except:
            date_from = today - timezone.timedelta(days=30)

    if not date_to:
        date_to = today
    else:
        try:
            date_to = timezone.datetime.strptime(date_to, '%Y-%m-%d').date()
        except:
            date_to = today

    # Get entries with filters
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

    # Calculate totals with gender breakdown
    total_registered = entries.aggregate(Sum('total_registered'))['total_registered__sum'] or 0
    total_male = entries.aggregate(Sum('registered_male'))['registered_male__sum'] or 0
    total_female = entries.aggregate(Sum('registered_female'))['registered_female__sum'] or 0
    total_transferred = entries.aggregate(Sum('total_transferred'))['total_transferred__sum'] or 0
    total_deleted = entries.aggregate(Sum('total_deleted'))['total_deleted__sum'] or 0
    total_entries = entries.count()

    # Group by ward with gender breakdown
    ward_summary = entries.values('ward__name').annotate(
        registered=Sum('total_registered'),
        male=Sum('registered_male'),
        female=Sum('registered_female'),
        transferred=Sum('total_transferred'),
        deleted=Sum('total_deleted'),
        count=Count('id')
    ).order_by('-registered')

    # Group by phase with gender breakdown
    phase_summary = entries.values('phase__name').annotate(
        registered=Sum('total_registered'),
        male=Sum('registered_male'),
        female=Sum('registered_female'),
        transferred=Sum('total_transferred'),
        deleted=Sum('total_deleted'),
        count=Count('id')
    ).order_by('-registered')

    # Group by kit with gender breakdown
    kit_summary = entries.values('kiems_kit__kit_name').annotate(
        registered=Sum('total_registered'),
        male=Sum('registered_male'),
        female=Sum('registered_female'),
        transferred=Sum('total_transferred'),
        deleted=Sum('total_deleted'),
        count=Count('id')
    ).order_by('-registered')[:20]

    # Create PDF response
    response = HttpResponse(content_type='application/pdf')
    filename = f"KIEMS_Report_{date_from.strftime('%Y%m%d')}_{date_to.strftime('%Y%m%d')}.pdf"
    response['Content-Disposition'] = f'attachment; filename="{filename}"'

    # Create PDF document
    doc = SimpleDocTemplate(response, pagesize=letter,
                            rightMargin=0.5 * inch, leftMargin=0.5 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.5 * inch)

    styles = getSampleStyleSheet()
    story = []

    # Custom styles
    title_style = ParagraphStyle(
        'Title',
        parent=styles['Heading1'],
        fontSize=20,
        alignment=TA_CENTER,
        spaceAfter=8,
        textColor=colors.HexColor('#1a5a2a')
    )

    subtitle_style = ParagraphStyle(
        'Subtitle',
        parent=styles['Normal'],
        fontSize=12,
        alignment=TA_CENTER,
        spaceAfter=12,
        textColor=colors.grey
    )

    header_style = ParagraphStyle(
        'Header',
        parent=styles['Heading2'],
        fontSize=14,
        spaceAfter=8,
        textColor=colors.HexColor('#1a5a2a'),
        fontName='Helvetica-Bold'
    )

    section_header = ParagraphStyle(
        'SectionHeader',
        parent=styles['Heading3'],
        fontSize=12,
        spaceAfter=6,
        textColor=colors.HexColor('#1a5a2a'),
        fontName='Helvetica-Bold'
    )

    # IEBC Green color scheme
    iebc_green = colors.HexColor('#1a5a2a')
    iebc_light_green = colors.HexColor('#e8f5e9')
    iebc_gold = colors.HexColor('#c9a84c')

    # Header with Logo
    try:
        from reportlab.platypus import Image
        from reportlab.lib.utils import ImageReader
        import os
        from django.conf import settings

        # Try to load logo from static
        logo_path = os.path.join(settings.STATIC_ROOT, 'images/logoiebc.png')
        if os.path.exists(logo_path):
            img = Image(logo_path, width=1.2 * inch, height=1.2 * inch)
            img.hAlign = 'CENTER'
            story.append(img)
        else:
            # Alternative logo text
            story.append(Paragraph("🏛️ IEBC", styles['Heading1']))
    except:
        pass

    # Title
    story.append(Paragraph("INDEPENDENT ELECTORAL AND BOUNDARIES COMMISSION", title_style))
    story.append(Paragraph("KIEMS Daily Report - Comprehensive Summary", subtitle_style))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph(f"Period: {date_from.strftime('%B %d, %Y')} - {date_to.strftime('%B %d, %Y')}",
                           styles['Normal']))
    story.append(Paragraph(f"Report Generated: {timezone.now().strftime('%B %d, %Y %H:%M')}",
                           styles['Normal']))
    story.append(Spacer(1, 0.2 * inch))
    story.append(HRFlowable(width="100%", thickness=2, color=iebc_green))
    story.append(Spacer(1, 0.2 * inch))

    # Executive Summary
    story.append(Paragraph("EXECUTIVE SUMMARY", header_style))

    summary_data = [
        ['Metric', 'Value'],
        ['Total Entries', str(total_entries)],
        ['Total Registered', str(total_registered)],
        ['Male', str(total_male)],
        ['Female', str(total_female)],
        ['Gender Ratio (M:F)', f"{total_male}:{total_female}" if total_female > 0 else "N/A"],
        ['Total Transferred', str(total_transferred)],
        ['Total Deleted', str(total_deleted)],
        ['Net Change', str(total_registered + total_transferred - total_deleted)],
    ]

    summary_table = Table(summary_data, colWidths=[2.5 * inch, 1.5 * inch])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), iebc_green),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, 1), (-1, -1), iebc_light_green),
        ('GRID', (0, 0), (-1, -1), 1, colors.grey),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 0.2 * inch))

    # Gender Breakdown Section
    story.append(Paragraph("GENDER BREAKDOWN", header_style))

    gender_data = [
        ['Gender', 'Count', 'Percentage'],
        ['Male', str(total_male), f"{(total_male / total_registered * 100):.1f}%" if total_registered > 0 else "0%"],
        ['Female', str(total_female),
         f"{(total_female / total_registered * 100):.1f}%" if total_registered > 0 else "0%"],
        ['Total', str(total_registered), '100%'],
    ]

    gender_table = Table(gender_data, colWidths=[1.5 * inch, 1.5 * inch, 1.5 * inch])
    gender_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), iebc_green),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('BACKGROUND', (0, 1), (-1, -2), iebc_light_green),
        ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#c8e6c9')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    story.append(gender_table)
    story.append(Spacer(1, 0.2 * inch))

    # Ward Summary
    if ward_summary:
        story.append(Paragraph("WARD SUMMARY", header_style))

        ward_data = [['Ward', 'Entries', 'Male', 'Female', 'Other', 'Total', 'Transferred', 'Deleted']]
        total_ward_entries = 0
        total_ward_male = 0
        total_ward_female = 0
        total_ward_other = 0
        total_ward_registered = 0

        for w in ward_summary:
            ward_data.append([
                w['ward__name'] or 'Unknown',
                str(w['count']),
                str(w['male'] or 0),
                str(w['female'] or 0),
                str(w['other'] or 0),
                str(w['registered'] or 0),
                str(w['transferred'] or 0),
                str(w['deleted'] or 0)
            ])
            total_ward_entries += w['count']
            total_ward_male += w['male'] or 0
            total_ward_female += w['female'] or 0
            total_ward_other += w['other'] or 0
            total_ward_registered += w['registered'] or 0

        # Add total row
        ward_data.append([
            'TOTAL',
            str(total_ward_entries),
            str(total_ward_male),
            str(total_ward_female),
            str(total_ward_other),
            str(total_ward_registered),
            '',
            ''
        ])

        ward_table = Table(ward_data, colWidths=[1.2 * inch, 0.6 * inch, 0.6 * inch, 0.6 * inch, 0.6 * inch, 0.6 * inch,
                                                 0.6 * inch, 0.6 * inch])
        ward_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), iebc_green),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
            ('BACKGROUND', (0, 1), (-1, -2), colors.whitesmoke),
            ('BACKGROUND', (0, -1), (-1, -1), iebc_light_green),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('FONTSIZE', (0, 1), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ]))
        story.append(ward_table)
        story.append(Spacer(1, 0.15 * inch))

    # Phase Summary
    if phase_summary:
        story.append(Paragraph("PHASE SUMMARY", header_style))

        phase_data = [['Phase', 'Entries', 'Male', 'Female', 'Other', 'Total', 'Transferred', 'Deleted']]
        for p in phase_summary:
            phase_data.append([
                p['phase__name'] or 'Unknown',
                str(p['count']),
                str(p['male'] or 0),
                str(p['female'] or 0),
                str(p['other'] or 0),
                str(p['registered'] or 0),
                str(p['transferred'] or 0),
                str(p['deleted'] or 0)
            ])

        phase_table = Table(phase_data,
                            colWidths=[1.2 * inch, 0.6 * inch, 0.6 * inch, 0.6 * inch, 0.6 * inch, 0.6 * inch,
                                       0.6 * inch, 0.6 * inch])
        phase_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), iebc_green),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
            ('BACKGROUND', (0, 1), (-1, -1), colors.whitesmoke),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('FONTSIZE', (0, 1), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        story.append(phase_table)
        story.append(Spacer(1, 0.15 * inch))

    # Kit Summary (Top 20)
    if kit_summary:
        story.append(Paragraph("TOP 20 KIT PERFORMANCE", header_style))

        kit_data = [['Kit', 'Male', 'Female', 'Other', 'Total']]
        for k in kit_summary:
            kit_data.append([
                k['kiems_kit__kit_name'] or 'Unknown',
                str(k['male'] or 0),
                str(k['female'] or 0),
                str(k['registered'] or 0)
            ])

        kit_table = Table(kit_data, colWidths=[1.5 * inch, 0.8 * inch, 0.8 * inch, 0.8 * inch, 0.8 * inch])
        kit_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), iebc_green),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 5),
            ('BACKGROUND', (0, 1), (-1, -1), colors.whitesmoke),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('FONTSIZE', (0, 1), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 3),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
        ]))
        story.append(kit_table)

    # Footer
    story.append(Spacer(1, 0.3 * inch))
    story.append(HRFlowable(width="100%", thickness=1, color=iebc_green))
    story.append(Spacer(1, 0.1 * inch))

    footer_style = ParagraphStyle(
        'Footer',
        parent=styles['Normal'],
        fontSize=8,
        textColor=colors.grey,
        alignment=TA_CENTER
    )

    story.append(Paragraph("This report is generated automatically by the KIEMS System", footer_style))
    story.append(Paragraph(f"© Independent Electoral and Boundaries Commission {timezone.now().year}", footer_style))
    story.append(Paragraph(f"Generated: {timezone.now().strftime('%Y-%m-%d %H:%M:%S')}", footer_style))

    # Build PDF
    doc.build(story)
    return response


@login_required
@user_passes_test(is_superadmin)
@require_GET
def get_clerks_by_ward(request):
    """API endpoint to get clerks for a specific ward"""
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
    """API endpoint to get VRAs for a specific ward"""
    ward_id = request.GET.get('ward_id')
    if not ward_id:
        return JsonResponse({'vras': [], 'error': 'No ward specified'}, status=400)

    try:
        vras = VRA.objects.filter(ward_id=ward_id, active=True).values('id', 'name').order_by('name')
        return JsonResponse({'vras': list(vras)})
    except Exception as e:
        return JsonResponse({'vras': [], 'error': str(e)}, status=500)