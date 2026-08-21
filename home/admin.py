from django.contrib import admin

from home.models import Ward, VRA, Clerk, Phase, KIEMSKit, DailyKIEMSEntry

# Register your models here.
admin.site.register(Ward)
admin.site.register(VRA)
admin.site.register(Clerk)
admin.site.register(Phase)
admin.site.register(KIEMSKit)
admin.site.register(DailyKIEMSEntry)
