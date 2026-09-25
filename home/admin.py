from django.contrib import admin

from home.models import Ward, VRA, Clerk, Phase, KIEMSKit, DailyKIEMSEntry, MovementSchedule, MovementScheduleState, \
    CronHeartbeat, DailyReportState, MovementScheduleLog, AuditLog, County

# Register your models here.
admin.site.register(Ward)
admin.site.register(VRA)
admin.site.register(Clerk)
admin.site.register(Phase)
admin.site.register(KIEMSKit)
admin.site.register(DailyKIEMSEntry)
admin.site.register(MovementSchedule)
admin.site.register(MovementScheduleState)
admin.site.register(MovementScheduleLog)
admin.site.register(AuditLog)
admin.site.register(DailyReportState)
admin.site.register(CronHeartbeat)
admin.site.register(County)

from .models import WhatsAppGroup, WhatsAppSetting

@admin.register(WhatsAppGroup)
class WhatsAppGroupAdmin(admin.ModelAdmin):
    list_display = ['name', 'group_id', 'is_active', 'created_at']
    search_fields = ['name', 'group_id']
    list_filter = ['is_active']

@admin.register(WhatsAppSetting)
class WhatsAppSettingAdmin(admin.ModelAdmin):
    list_display = ['user', 'default_group', 'notify_vra', 'notify_edit', 'notify_daily', 'notify_grand_total']
    search_fields = ['user__username', 'user__email']
    list_filter = ['notify_vra', 'notify_edit', 'notify_daily', 'notify_grand_total']