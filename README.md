# KIEMS Backend

Django monolith serving three tightly integrated surfaces:

- **Public API** (`/api/`, `/kiems/`, `/movement/`) — JSON endpoints for the
  Svelte PWA used by field agents.
- **ICT Portal** (`/ict/`) — back office for constituency ICT officers.
- **Superadmin Panel** (`/superadmin/`) — cross-constituency oversight.

Supports async WhatsApp delivery, offline-safe writes, PDF/Excel exports, and
Form A inventory reconciliation.

---

## Table of contents

- [Screenshots](#screenshots)
- [Architecture](#architecture)
- [Tech stack](#tech-stack)
- [Repository layout](#repository-layout)
- [Domain model](#domain-model)
- [Form A / Acknowledgment](#form-a--acknowledgment)
- [Idempotency & offline safety](#idempotency--offline-safety)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Environment variables](#environment-variables)
- [Running](#running)
- [First-time bootstrap](#first-time-bootstrap)
- [API reference](#api-reference)
- [Admin workflows](#admin-workflows)
- [WhatsApp bot integration](#whatsapp-bot-integration)
- [Cron jobs](#cron-jobs)
- [Deployment](#deployment)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

---

## Architecture

```
                  ┌──────────────────────┐
                  │  Svelte PWA (field)  │
                  │  (separate repo)     │
                  └──────────┬───────────┘
                             │ HTTPS + Idempotency-Key
                             ▼
┌────────────────────────────────────────────────────┐
│  Django monolith                                   │
│  ├── api/          JSON API for PWA               │
│  ├── home/         shared domain + PWA page views │
│  ├── ict/          ICT officer portal             │
│  ├── superadmin/   cross-constituency admin       │
│  └── services/     WhatsApp queue, reports, etc.  │
└──────────┬─────────────────────────┬───────────────┘
           │                         │
           ▼                         ▼
   ┌───────────────┐         ┌──────────────────┐
   │ PostgreSQL    │         │ WhatsApp bot     │
   │ (source of    │         │ (Node, separate) │
   │  truth)       │         │                  │
   └───────────────┘         └──────────────────┘
```

- **DB:** PostgreSQL via Django ORM.
- **Idempotency:** every client write carries `Idempotency-Key`; server
  stores responses in `api.models.IdempotencyRecord` for replay safety.
- **WhatsApp is async:** writes enqueue to `OutgoingWhatsAppMessage`; a
  background drain (per-request thread + cron) delivers. Request latency is
  unaffected by bot availability.

---

## Tech stack

| Component | Version | Purpose |
|---|---|---|
| Python | 3.11+ (3.14 tested) | Runtime |
| Django | 4.2+ / 5.x | Web framework |
| PostgreSQL | 14+ | Primary DB |
| reportlab | 4.x | PDF fallback when PDF.co offline |
| openpyxl | 3.x | Excel exports |
| requests | 2.x | Outbound HTTP (bot, voter proxy, PDF.co) |

Optional:

- **PDF.co** account for cloud PDF rendering
- **WhatsApp bot** (Node 20 + Baileys) for group notifications

---

## Repository layout

```
kiems/
├── api/                    # JSON API for the PWA
│   ├── models.py           # Announcement, Incident, IdempotencyRecord, ...
│   ├── views.py            # Endpoints
│   ├── urls.py             # /api/* routes
│   ├── utils.py            # with_idempotency, helpers
│   ├── parsers/            # Voter portal HTML scraper
│   └── reference/          # Static help/training docs
│
├── home/                   # Shared domain + legacy page views
│   ├── models.py           # Ward, VRA, Clerk, Device, DailyKIEMSEntry, FormA*
│   ├── views.py            # PWA page views, binding, kits, clerk tools
│   ├── services/
│   │   ├── whatsapp.py
│   │   ├── whatsapp_queue.py
│   │   ├── daily_report.py
│   │   ├── movement_schedule.py
│   │   └── form_a_sync.py
│   ├── templates/
│   └── management/commands/
│
├── ict/                    # ICT officer portal
│   ├── views.py
│   ├── urls.py
│   ├── forms.py
│   ├── decorators.py       # @ict_required
│   └── templates/ict/
│
├── superadmin/             # Cross-constituency admin
│
├── kiems/                  # Django project (settings, root urls)
│
├── docs/
│   └── screenshots/
│
├── requirements.txt
└── manage.py
```

---

## Domain model

### Constituency hierarchy

```
Constituency
└── Ward (many)
    ├── VRA       (one active per ward)
    ├── Clerk     (one active per ward)
    ├── KIEMSKit  (many)
    └── RegistrationCentre
```

### Device binding

A `Device` (keyed by browser fingerprint) is auto-authorized on first
registration. Binding is exclusive: one active VRA per ward → one device.
Revoking in the ICT Devices page sets `is_burned=False` and clears the VRA's
`device_token`.

### Daily entry

`DailyKIEMSEntry` — one row per `(kiems_kit, phase, entry_date, vra)`.
Unique constraint prevents duplicates.

- `entry_type = "REGISTRATION"` — actual voter counts
- `entry_type = "VENUE"` — venue premap only (no counts)

### Movement schedule

`MovementSchedule` — one row per `(kiems_kit, phase, schedule_date)`.
Forward-looking only.

---

## Form A / Acknowledgment

Two physical materials:

| Item | Description | Serial |
|---|---|---|
| **Form A** | Loose forms, batched | Optional |
| **Acknowledgment booklet** | Serialed booklet, exactly 50 forms | Required range |

### Models

```
FormAReceipt     ← office records a batch arriving in a ward
  ├── form_count, booklet_count (ACK only), serial_start, serial_end
  ├── received_by (ICT officer), received_at, phase, ward
  └── is_archived

FormAAllocation  ← split receipt across kits
  ├── receipt, kiems_kit, vra, forms_allocated
  └── unique (receipt, kiems_kit)

FormAUsage       ← what was consumed
  ├── receipt, kiems_kit, vra, entry_date, forms_used
  └── kind: USED | SPOILED

FormASpoilage    ← per-serial spoilage (ACK only)
  └── receipt, serial_number, kiems_kit, reason, entry_date
```

### Lifecycle

```
1. ICT records receipt        → FormAReceipt (200 forms, 4 booklets)
2. ICT allocates to kits      → FormAAllocation (kit A: 100, kit B: 100)
3. VRA reports a spoilage     → FormASpoilage (serial 000042, TORN)
4. VRA submits registrations  → FormAUsage auto-created per entry
5. Balance recomputes live    → remaining = 200 - used - spoiled
```

### Entry → Form A reconciliation

`home/services/form_a_sync.py::sync_form_a_for_entry()` runs on every
`DailyKIEMSEntry` create / edit / delete.

- **Delta-based:** callers pass `previous_total`; the function applies
  `delta = new - previous`.
- **Receipt selection:**
  1. Kit-level `FormAAllocation` (preferred)
  2. Ward's oldest non-exhausted receipt (fallback pool)
  3. If neither, entry still saves — response carries `form_a_warnings`
- **Idempotent:** re-submitting the same entry yields `delta = 0`.

### VRA-facing API

```
GET  /api/form-a/mine/                    — serialed receipts in VRA's ward
GET  /api/form-a/kits/                    — kits in VRA's ward
POST /api/form-a/allocate/                — VRA allocates to a kit
POST /api/form-a/report-spoil/            — VRA reports a spoiled serial
GET  /api/form-a/spoilages-mine/          — today's spoilages
GET  /api/form-a/receipts/<uuid>/allocations/
```

### ICT-facing screens

```
GET  /ict/forms/                          — receipts list with balances
GET  /ict/forms/receive/                  — record a new receipt
GET  /ict/forms/<uuid>/                   — detail: allocations, usages, spoilages
GET  /ict/forms/<uuid>/edit/              — edit receipt
POST /ict/forms/<uuid>/allocate/          — ICT allocates on behalf of a VRA
POST /ict/forms/allocations/<uuid>/delete/
GET  /ict/forms/spoilages/                — all spoilages filtered by ward/date
```

---

## Idempotency & offline safety

- Client sends `Idempotency-Key: <uuid>` on every write.
- Server stores `(key, scope) → response` in `IdempotencyRecord`.
- Replay returns the stored body with `replayed: true`.
- Works with the PWA's offline queue: any write can be retried safely.

Entry-submission payloads are bounded by `(kiems_kit, phase, entry_date, vra)`
at the DB layer, so even if idempotency were bypassed, no duplicate rows can
exist.

---

## Prerequisites

- Python 3.11+ (3.14 tested)
- PostgreSQL 14+
- Git
- (optional) WhatsApp bot with a paired phone
- (optional) PDF.co account

---

## Setup

```bash
git clone <repo-url>
cd kiems

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt

cp .env.example .env
# Edit .env — see Environment variables

python manage.py migrate
python manage.py createsuperuser
python manage.py runserver 0.0.0.0:8000
```

ICT portal: <http://127.0.0.1:8000/ict/>

---

## Environment variables

Create `.env` in the project root:

```bash
# ── Django ──
SECRET_KEY=change-me-in-production
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1,10.0.0.0/8
DATABASE_URL=postgres://user:pass@localhost:5432/kiems
CSRF_TRUSTED_ORIGINS=http://localhost:5173,http://127.0.0.1:8000

# ── PWA (for QR code / download page) ──
PWA_URL=http://localhost:5173

# ── WhatsApp bot ──
WHATSAPP_BOT_URL=http://localhost:3000

# ── Voter lookup proxy (optional) ──
IEBC_VERIFY_URL=https://verify.iebc.or.ke/verify
IEBC_TIMEOUT=12
IEBC_CACHE_SECONDS=3600
VOTER_LOOKUP_ENABLED=True
VOTER_LOOKUP_RL_PER_MIN=30

# ── Cron secret ──
CRON_SECRET=generate-a-long-random-string

# ── PDF rendering (optional) ──
PDF_CO_API_KEY=
PDF_CO_API_URL=https://api.pdf.co/v1
```

All settings read via `getattr(settings, ..., default)` so the project runs
without optional keys.

---

## Running

```bash
python manage.py runserver 0.0.0.0:8000
```

Surfaces:

| URL | Purpose |
|---|---|
| `/ict/` | ICT officer portal |
| `/superadmin/` | Superadmin panel |
| `/admin/` | Django admin |
| `/api/` | JSON endpoints (PWA) |
| `/kiems/` | PWA page shell + binding |
| `/movement/` | PWA movement page |
| `/` | Download-app landing |

---

## First-time bootstrap

1. **Constituency** — create in Django admin
2. **Wards** — ICT portal → Wards
3. **VRAs + Clerks** — ICT portal → Staff
4. **KIEMS kits** — ICT portal → Kits
5. **Active phase** — Django admin → Phases
6. **Form A receipts** — ICT portal → Form A → Record receipt
7. **WhatsApp bot** — ICT portal → Notifications → scan QR
8. **Register a device** — open the PWA, follow the binding prompt

---

## API reference

All endpoints return `{"ok": bool, ...}`. All POSTs accept
`Idempotency-Key` header.

### Authentication

- **PWA:** by device fingerprint (`fingerprint` in body/query); server
  resolves to `Device → VRA` / `Clerk`.
- **ICT / Superadmin:** Django session auth.

### Entry submission

```
POST /kiems/submit-entries/
Content-Type: application/x-www-form-urlencoded
Idempotency-Key: <uuid>

fingerprint=abc123
date=2026-11-21
kit_id[]=1&kit_id[]=2
venue[]=Amani+Hall&venue[]=Uhuru+Hall
registered_male[]=10&registered_male[]=12
registered_female[]=8&registered_female[]=10
```

Response:

```json
{
  "ok": true,
  "saved": 2,
  "form_a_warnings": [
    {"kit_id": 2, "kit_name": "Kit B", "warning": "No Form A receipt available..."}
  ]
}
```

### Movement save

```
POST /movement/save/
Content-Type: application/json
Idempotency-Key: <uuid>

{
  "ward_id": 1,
  "fingerprint": "abc123",
  "schedule_date": "2026-11-22",
  "entries": [
    {"kit_id": 1, "venue": "Amani Hall", "notes": ""}
  ]
}
```

### Form A — VRA endpoints

```
POST /api/form-a/allocate/
POST /api/form-a/deallocate/
POST /api/form-a/report-spoil/
GET  /api/form-a/mine/?fingerprint=
GET  /api/form-a/kits/?fingerprint=
GET  /api/form-a/spoilages-mine/?fingerprint=&date=
GET  /api/form-a/receipts/<uuid>/allocations/?fingerprint=
```

### Form A — ICT endpoints

```
GET  /ict/forms/
GET  /ict/forms/receive/       POST /ict/forms/receive/
GET  /ict/forms/<uuid>/
GET  /ict/forms/<uuid>/edit/   POST /ict/forms/<uuid>/edit/
POST /ict/forms/<uuid>/archive/
POST /ict/forms/<uuid>/allocate/
POST /ict/forms/allocations/<uuid>/delete/
GET  /ict/forms/spoilages/
```

### Incidents

```
POST /api/incidents/           (multipart — supports photo)
```

### Misc endpoints

```
GET  /api/stats/day/?fingerprint=&date=
GET  /api/announcements/?ward_id=&page=
GET  /api/announcements/<uuid>/
GET  /api/reference/
GET  /api/reference/<slug>/
GET  /api/stations/?q=&ward_id=
POST /api/verify-voter/        {fingerprint, id_number, year_of_birth}
GET  /api/voter-lookup/health/
POST /api/push/subscribe/
POST /api/sync/event/
GET  /api/csrf/
```

### Device binding (unauthenticated)

```
POST /kiems/register-device/
GET  /kiems/check-device-status/?fingerprint=
GET  /kiems/resolve-vra/?fingerprint=
GET  /kiems/resolve-clerk/?fingerprint=
POST /kiems/bind-ward/
POST /kiems/bind-clerk/
```

---

## Admin workflows

### ICT officer

| Task | Location |
|---|---|
| Manage wards | `/ict/wards/` |
| Manage VRAs & clerks | `/ict/staff/` |
| Manage KIEMS kits | `/ict/kits/` |
| Authorize / revoke devices | `/ict/devices/` |
| Daily entries (view, edit, export) | `/ict/entries/` |
| Movement schedules | `/ict/movement/` |
| Form A receipts & allocations | `/ict/forms/` |
| WhatsApp group & reports | `/ict/notifications/` |
| Audit log | `/ict/audit/` |
| System status | `/ict/status/` |

### Superadmin

| Task | Location |
|---|---|
| Cross-constituency dashboard | `/superadmin/` |
| Phase management | `/superadmin/phases/` |
| Global settings | `/superadmin/settings/` |
| All audits | `/superadmin/audits/` |

---

## WhatsApp bot integration

The bot is a **separate Node.js service** (see its own README). Django calls:

```
GET  {WHATSAPP_BOT_URL}/status   → {isReady, status, phoneNumber}
GET  {WHATSAPP_BOT_URL}/groups   → {data: [{id, name, participants}]}
POST {WHATSAPP_BOT_URL}/send     → {groupId, message}
```

### Queue model

Writes enqueue to `OutgoingWhatsAppMessage` (`status=QUEUED`) and drain in
the background. `home/services/whatsapp_queue.py` handles:

- **Claim:** `SELECT` + conditional `UPDATE WHERE status=QUEUED` so multiple
  workers can't double-send
- **Send:** 8s timeout, one attempt per drain tick
- **Backoff:** `30s → 2m → 10m → 1h → 4h` between retries
- **Give up:** after 5 attempts, status → `GAVE_UP` (visible in
  `/ict/status/`)

### Offline behavior

If the bot is down:

1. Django writes `OutgoingWhatsAppMessage(status=QUEUED)`
2. Request latency is unaffected
3. Cron `/ict/cron/send-reports/` drains the queue every 15 min
4. Retries with exponential backoff
5. After 5 failed attempts, status becomes `GAVE_UP`

### Manual controls

- **Test connection:** `/ict/notifications/` shows live bot status
- **Re-pair:** scan QR from the same page
- **Force drain:** `/ict/status/` → **Run tick** button
- **Retry an individual message:** `/ict/status/` → row action

---

## Cron jobs

Register with an external scheduler (cron-job.org, EasyCron, etc.):

| Endpoint | Frequency | Purpose |
|---|---|---|
| `/ict/cron/send-reports/` | every 15 min | Daily report tick + WhatsApp queue drain |
| `/ict/cron/drain-whatsapp/` | every 2 min | Dedicated WhatsApp queue retry (optional) |
| `/api/voter-lookup/health/` | every 30 min | Voter portal liveness |

Every endpoint requires `Authorization: Bearer $CRON_SECRET`.

Example crontab entry (Linux):

```cron
*/15 * * * * curl -s -H "Authorization: Bearer $CRON_SECRET" https://api.example.com/ict/cron/send-reports/ >/dev/null
```

---

## Deployment

### Gunicorn + Nginx

```bash
pip install gunicorn whitenoise
python manage.py collectstatic --noinput

gunicorn kiems.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers 4 \
    --timeout 60 \
    --access-logfile - \
    --error-logfile -
```

Nginx:

```nginx
server {
    listen 80;
    server_name api.example.com;

    client_max_body_size 20M;

    location /static/ {
        alias /var/www/kiems/staticfiles/;
        expires 30d;
    }

    location /media/ {
        alias /var/www/kiems/media/;
        expires 7d;
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

### Systemd service

```ini
[Unit]
Description=KIEMS Backend
After=network.target postgresql.service

[Service]
Type=notify
User=kiems
Group=kiems
WorkingDirectory=/var/www/kiems
EnvironmentFile=/var/www/kiems/.env
ExecStart=/var/www/kiems/.venv/bin/gunicorn \
    --bind 0.0.0.0:8000 \
    --workers 4 \
    --timeout 60 \
    kiems.wsgi:application
ExecReload=/bin/kill -s HUP $MAINPID
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kiems
sudo systemctl status kiems
```

### Backups

```bash
# DB
pg_dump -U kiems kiems | gzip > kiems-$(date +%F).sql.gz

# Media (incident photos)
tar czf media-$(date +%F).tar.gz /var/www/kiems/media/
```

Run daily via cron, rotate ≥7 days. Test a restore quarterly.

### Production checklist

- [ ] `DEBUG=False`
- [ ] `SECRET_KEY` is a fresh 50-char random string
- [ ] `ALLOWED_HOSTS` set to the deployed domain
- [ ] `CSRF_TRUSTED_ORIGINS` includes the PWA origin
- [ ] `DATABASE_URL` points to Postgres with SSL (`?sslmode=require`)
- [ ] `CRON_SECRET` is set and matches the cron scheduler config
- [ ] Static files collected (`collectstatic`)
- [ ] HTTPS enforced (Nginx redirect + HSTS header)
- [ ] Firewall allows 80/443 only
- [ ] `WHATSAPP_BOT_URL` points at a running bot
- [ ] Log rotation configured (`logrotate` for `/var/log/kiems/`)

---

## Troubleshooting

### "Device not authorized"

Check `Device.is_burned = True` and `is_active = True` in `/ict/devices/`.
If revoked, re-authorize via the ICT portal.

### WhatsApp messages not arriving

1. `curl {WHATSAPP_BOT_URL}/status` — is the bot ready?
2. Check `/ict/status/` for `FAILED` / `GAVE_UP` `OutgoingWhatsAppMessage` rows
3. Manual drain:
   ```bash
   python manage.py shell -c "from home.services.whatsapp_queue import drain_whatsapp_queue; print(drain_whatsapp_queue())"
   ```

### "No Form A receipt available"

The kit's ward has no active receipt with available forms. Fix in
`/ict/forms/receive/` → record a receipt → allocate to the kit, or leave
unallocated for the ward pool to absorb it.

### Form A balance is wrong

```bash
python manage.py shell -c "
from home.models import FormAReceipt
r = FormAReceipt.objects.get(pk='<uuid>')
print('form_count:', r.form_count)
print('used:', r.used_count)
print('spoiled:', r.spoiled_count)
print('remaining:', r.remaining)
"
```

Audit the usage rows via `/ict/forms/<uuid>/` and correct any mis-entered
values.

### Migration conflicts

```bash
python manage.py makemigrations --merge
```

Or, if the conflict is trivial, delete the conflicting migration and
regenerate.

### 500 on entry submit with "No VRA available"

The clerk tool tried to save a venue mapping but no VRA exists for the kit's
ward. Fix by creating an active VRA in `/ict/staff/`.

### `ModuleNotFoundError` on startup

- Ensure `requirements.txt` is installed in the active venv
- Check `settings.py` imports for missing optional packages (e.g. `reportlab`)

---

## Contributing

1. Fork the repo.
2. Create a feature branch: `git checkout -b feature/my-thing`
3. Commit with conventional prefixes: `feat:`, `fix:`, `chore:`, `docs:`
4. Push and open a PR against `main`.
5. Ensure `python manage.py check` passes before requesting review.

### Commit style

```
feat(form-a): allocate receipts to kits
fix(sync): prevent duplicate WhatsApp delivery on retry
docs(readme): add deployment checklist
```

### Code style

- **Formatter:** `black` (line length 100)
- **Imports:** `isort` (profile `black`)
- **Linter:** `flake8` or `ruff`

Run all three before committing:

```bash
black .
isort .
ruff check .    # or: flake8 .
python manage.py check
```

### Tests

```bash
python manage.py test
```

Individual apps:

```bash
python manage.py test home.tests
python manage.py test api.tests
python manage.py test ict.tests
```

### Form A reconciliation smoke test

```bash
python manage.py shell -c "
from datetime import date
from home.models import *
from home.services.form_a_sync import sync_form_a_for_entry

phase = Phase.objects.filter(active=True).first()
kit = KIEMSKit.objects.filter(status=True).first()
vra = VRA.objects.filter(ward=kit.ward, active=True).first()

receipt = FormAReceipt.objects.create(
    form_type='ACKNOWLEDGMENT', phase=phase, ward=kit.ward,
    form_count=200, booklet_count=4, serial_start='000001', serial_end='000200',
)
FormAAllocation.objects.create(
    receipt=receipt, kiems_kit=kit, vra=vra, forms_allocated=200,
)
print('Before:', receipt.remaining)  # 200

entry, _ = DailyKIEMSEntry.objects.update_or_create(
    kiems_kit=kit, phase=phase, entry_date=date.today(), vra=vra,
    defaults={'ward': kit.ward, 'venue': 'Test', 'registered_male': 4,
              'registered_female': 6, 'entry_type': 'REGISTRATION'},
)
sync_form_a_for_entry(entry, previous_total=0, acting_vra=vra)
print('After submit:', receipt.remaining)  # 190
"
```

---

## License

MIT License — see [LICENSE](./LICENSE) for the full text.

Copyright (c) 2026 wayne-alex

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```