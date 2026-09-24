from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import timedelta

from home.models import MovementScheduleState
from home.services.movement_schedule import format_grand_movement_message
from home.services.whatsapp import send_to_constituency


class Command(BaseCommand):
    help = "Send grand movement-schedule reports for any READY state."

    def handle(self, *args, **opts):
        ready = MovementScheduleState.objects.filter(status="READY").select_related("constituency")
        for state in ready:
            try:
                state.status = "SENDING"
                state.save(update_fields=["status"])

                msg = format_grand_movement_message(state.constituency, state.schedule_date)
                ok, err, _ = send_to_constituency(state.constituency, msg)

                if ok:
                    state.status = "SENT"
                    state.sent_at = timezone.now()
                else:
                    state.status = "FAILED"
                    state.last_error = err or "unknown"
                state.attempts += 1
                state.save()
                self.stdout.write(f"{state} → {state.status}")
            except Exception as e:
                state.status = "FAILED"
                state.last_error = str(e)
                state.save()
                self.stderr.write(f"{state} error: {e}")