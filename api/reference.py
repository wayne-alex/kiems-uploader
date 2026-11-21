# api/reference.py
"""
Static reference content for the Training and Help tiles.

Each doc: { slug, title, category, body_markdown, updated }
Categories: "training" | "help"

Edit this file to change content; the app fetches it via /api/reference/.
"""

REFERENCE = {
    # ── TRAINING ────────────────────────────────────────
    "training-count": {
        "slug": "training-count",
        "title": "How to count voters",
        "category": "training",
        "updated": "2026-11-07",
        "summary": "Male/female breakdown, when to enter 0, what transferred means.",
        "body_markdown": """
## The basic rule

Every voter you register is either **male** or **female**. Enter the
number in the correct box. The app adds them up for you — you don't
need to add.

If you register **0 voters** at a kit on a given day, still tap
**Save** with both counts at 0. That tells us "I checked this kit
today, nothing happened." It's different from never having touched
the kit.

## What "transferred" means

A **transferred** voter is someone who moved their registration from
another station to yours. They count toward your total for the day.
Tick "transferred" if that's what happened.

The office enters this number from their side — you just tell them
verbally if a voter mentions it.

## What "updated" and "deleted" mean

**Updated**: a voter changed their details (name spelling, ID number).
Does not change the count.

**Deleted**: a voter was removed from the register (death, official
deletion). Also does not change the count.

## The tally sheet

If you're keeping paper tallies, they should match what you type.
If they don't match, re-count before submitting. The app will
verify against the kit's cumulative screen.

## If you get it wrong

You can edit today's entry until **midnight**. After that, the
office takes over and any changes must go through them. Call your
ICT officer if you realise you made a mistake the next day.
""",
    },

    "training-verify": {
        "slug": "training-verify",
        "title": "Verify kits with confidence",
        "category": "training",
        "updated": "2026-11-07",
        "summary": "What the cumulative screen is, what drift means, when to proceed.",
        "body_markdown": """
## What the verification does

The kit's screen shows a **cumulative total** — the number of
registrations that kit has processed since it was commissioned.
Not today's number. The total ever.

Your app knows how many registrations have already been reported
for that kit. Verification compares what the kit is showing now
against what we expect it to show — if everything you've entered
so far was correct.

## Three results, three actions

**✓ Matches our records**
You're good. The number on the kit is what we expected. Proceed.

**⚠ Small drift** (a few votes off)
Likely a typo somewhere, or a missing day. Re-check the kit
screen. If it's correct, add a short note ("kit screen replaced
after power cut") and proceed. Small drift is fine.

**✗ Big drift** (large difference)
Stop. Something is wrong. Re-check the kit screen carefully.
If the number really is that different, add a note explaining
what you see and proceed. The office will follow up.

## When to verify

Verify **every kit, every day**, right after you enter the daily
counts. It takes 30 seconds and catches most data-entry errors.

## If a verify returns "No baseline"

First time that kit has been verified today. Nothing to compare
against yet. Proceed — future verifications will use today's
baseline.

## What "drift" doesn't mean

It doesn't mean anything is broken. It usually means one of:
- Yesterday's count was entered wrong
- A voter was transferred but not recorded
- The kit screen shows a different starting baseline

In all three cases, the correct action is: write a note,
proceed, tell your ICT officer.
""",
    },

    "training-movement": {
        "slug": "training-movement",
        "title": "Plan movement correctly",
        "category": "training",
        "updated": "2026-11-07",
        "summary": "What a movement plan is, why it's for tomorrow, how it reaches ICT.",
        "body_markdown": """
## What movement planning is

Every day, you tell the office where each kit will be stationed
**tomorrow**. This is different from today's registration — today
you count voters, tomorrow you plan where kits go.

The plan answers: *"tomorrow, which kit will be at which venue?"*

## Why tomorrow, not today

Tomorrow's plan needs to reach the office **tonight**. They send
it to the constituency WhatsApp group at the end of the day so
everyone knows where to deploy in the morning. By the time you
enter today's numbers, tomorrow is already decided.

You can plan up to 90 days ahead if you need to (for multi-day
operations). But the daily plan for tomorrow is the important one.

## The venue

The venue is a specific place name — "Mwangaza Primary School",
"Kirigiti Shopping Centre". Include the landmark if the name is
uncommon. Don't just say "the school" — the office needs to send
someone there.

Use the same name every time you visit the same place. The app
learns names as you type them, so if you start typing "Mwang",
it'll suggest "Mwangaza Primary School" from your past plans.

## What the office sees

Your plan goes into the constituency WhatsApp group at the end
of the day. Every ward's plan for tomorrow is there. The ICT
officer can see at a glance who's going where.

If you change tomorrow's plan after submitting, it re-sends —
they'll see the update.

## If a plan changes at the last minute

Call your ICT officer. Don't rely on the app — they may have
already deployed based on the old plan.

## Past dates

You can't plan for yesterday or today. Those are locked. If you
need to correct a past plan, tell the ICT officer.
""",
    },

    "training-incidents": {
        "slug": "training-incidents",
        "title": "Report an incident",
        "category": "training",
        "updated": "2026-11-07",
        "summary": "The 6 incident types, when to add a photo, how severity works.",
        "body_markdown": """
## What counts as an incident

Anything that stops you from working, or that the office needs to
know about immediately:

- **Kit fault** — printer jammed, screen broken, battery flat
- **Kit missing** — the kit isn't where it should be
- **Security** — a safety concern at your station
- **Network** — no signal, can't submit
- **Supplies** — out of forms, no ballot boxes
- **Other** — anything else that needs attention

## Adding a photo

A photo helps the office see the problem without calling you. Take
one if you can — a picture of the jammed printer, the empty box,
the closed road.

Photos are compressed before upload (~150KB) so they work even on
slow networks. If you're offline, the incident + photo queue up
and send when you're back online.

## Severity

**Low** — informational. "The printer is slow but working."
**Medium** — needs attention today. "The printer jammed, I'm using backup."
**High** — blocking work. "The kit won't turn on."

Pick honestly. Low-severity incidents are still worth reporting —
they add up and help the office spot patterns.

## If a kit is truly broken

Report it as **High** severity with a photo. Use "Kit fault" type.
Include the kit's serial number in the description. Then call your
ICT officer — the app queues the report, but a phone call gets you
a replacement faster.

## Incidents go first

Incidents sync **before** your registration counts. If you report
an incident while offline and then enter counts, the incident
still reaches the office first when you reconnect. That's
intentional — the office needs to know about problems fast.
""",
    },

    # ── HELP ────────────────────────────────────────────
    "help-contacts": {
        "slug": "help-contacts",
        "title": "Contact your ICT officer",
        "category": "help",
        "updated": "2026-11-07",
        "summary": "Escalation tree, phone numbers, when to call vs WhatsApp.",
        "body_markdown": """
## Order of escalation

1. **Your ward ICT officer** — first call for anything routine
2. **Your constituency ICT officer** — if the ward officer is
   unavailable for more than 30 minutes
3. **The national helpdesk** — only if both above are unreachable

## Phone vs WhatsApp

**Call** if:
- The kit is broken and you can't work
- A voter is disputing their registration and you need a
  decision on the spot
- Anything security-related

**WhatsApp** if:
- You need to change tomorrow's movement plan
- You found a data error from yesterday
- You have a question about a procedure

**Use the app** (Incidents tile) if:
- You're reporting a fault that doesn't block work
- You're noting a network issue
- The incident would benefit from a photo

## Keep your call short

The ICT officer is managing many wards. When you call, have ready:
- Your ward name
- Kit name and serial number
- What the problem is in one sentence
- What you've already tried

That gets you an answer in 30 seconds instead of 3 minutes.

## If nobody picks up

1. WhatsApp the same person (they may be on another call)
2. Wait 15 minutes
3. Try the constituency office
4. If urgent and no response in 30 min, use the national
   helpdesk number in your onboarding pack

Don't spam calls. If they're not answering, they're helping
someone else.
""",
    },

    "help-kit-trouble": {
        "slug": "help-kit-trouble",
        "title": "When the kit won't work",
        "category": "help",
        "updated": "2026-11-07",
        "summary": "Troubleshooting steps before filing a fault.",
        "body_markdown": """
## Before you call — try this

Most kit problems are fixable in the field.

### Kit won't turn on
1. Check the power button (sometimes labelled "PWR" or shown as
   a circle with a line)
2. Hold it for **3 full seconds**
3. If nothing, check the battery is seated
4. Plug it into power for 5 minutes, try again

### Screen is frozen
1. Hold the power button for **10 seconds** to force restart
2. Wait 30 seconds for boot
3. If it boots to the same screen, unplug and replug the battery

### Printer jams
1. Open the printer compartment (usually a flip-up cover)
2. Remove the paper roll
3. Look for torn paper stuck in the feed
4. Use a pen to gently rotate the feed roller to clear
5. Re-seat the paper roll with the loose end facing **up**

### Kit doesn't recognise voter IDs
1. Check the kit is showing "READY" not "ERROR"
2. Clean the fingerprint scanner with a soft cloth
3. If still failing, restart the kit

## When to file a fault

File a kit fault if:
- You've tried the above and it still won't work
- The fault blocks you from registering voters
- You see a hardware problem (cracked screen, missing buttons,
  a burning smell — turn it off immediately and call)

## What to include

When you file the fault:
- **Type**: Kit fault
- **Severity**: High if blocking, Medium if degraded
- **Kit**: pick from the list
- **Description**: what you tried and what happened
- **Photo**: a clear shot of the problem

## What happens next

Your ICT officer gets the incident. They'll either:
- Send someone to fix it
- Send a replacement kit
- Tell you to move to the nearest working kit

Don't leave the station to bring the kit in yourself — wait for
the office to decide.
""",
    },

    "help-faq": {
        "slug": "help-faq",
        "title": "Frequently asked questions",
        "category": "help",
        "updated": "2026-11-07",
        "summary": "The 12 questions VRAs ask most.",
        "body_markdown": """
## What if I forgot to enter yesterday's numbers?

You can't backdate entries in the app. Call your ICT officer —
they can enter yesterday's numbers from their side. Do not try
to submit today's numbers as if they were yesterday's.

## What if I entered the wrong count?

You can edit today's count until midnight. Go back to
**Registration**, tap the kit, change the number, save again.
The kit's cumulative check will show if you've drifted from what
the kit expects.

## What if I entered the wrong venue?

Same — edit before midnight. The movement plan for tomorrow
can also be edited anytime before midnight. After midnight, both
are locked.

## What if I get "Big drift" on verify?

Re-check the kit's screen carefully. If the screen really shows
that number, add a note explaining what you see, and proceed.
The office will follow up. Don't argue with the number.

## Why is my submission not syncing?

Tap the badge at the top of the screen. It shows how many
changes are queued. If it says "3 pending" and you're online,
wait 30 seconds — the app tries again automatically. If it
stays stuck for more than 5 minutes, check your network.

## Can I use the app on two phones?

No. Each ward is locked to one device. If your phone breaks,
call the office — they'll transfer the ward to a new phone.

## Do I need to be online to work?

No. Everything works offline:
- Registration (queues and syncs)
- Movement (queues and syncs)
- Incidents (queues and syncs)
- Reports, Announcements, Training, Help (all cached)

The only things that need internet are Voter Lookup (opens IEBC
website) and first-time login/binding.

## How do I know my data reached the office?

After submitting, watch the badge at the top. When it goes back
to "Synced" (green check), everything reached the server. If it
stays amber with a number, some changes are still queued.

## What if I lose my phone?

Call your ICT officer immediately. They'll:
1. Revoke your device authorization
2. Issue a new device to your ward

Your queued changes on the lost phone are lost. This is why
syncing every 30 minutes matters.

## Can I see other wards' numbers?

No — you only see your own ward's data. The office sees all wards
through a different system.

## Why can't I plan movement for yesterday?

Past dates are locked. Only today and future dates are editable
in movement planning. Historical plans are read-only.

## How often should I check announcements?

At least once in the morning and once before closing. The banner
on the home screen shows the latest unread item. Important
announcements (like "all wards submit by 17:00 today") are marked
high-priority.
""",
    },

    "help-glossary": {
        "slug": "help-glossary",
        "title": "Glossary of terms",
        "category": "help",
        "updated": "2026-11-07",
        "summary": "KIEMS, VRA, CAW, EVID, BVR, and other acronyms.",
        "body_markdown": """
## KIEMS
**Kenya Integrated Elections Management System.** The kit you carry
and the software it runs. Yours is one of many.

## VRA
**Voter Registration Assistant.** You. Field staff who register
voters at the station.

## Clerk
Ward Clerk or Assistant Clerk. Also field staff, but on a
different career track. Sometimes you'll see "clerk" mentioned in
the app; it means a role similar to yours.

## CAW
**County Assembly Ward.** The smallest electoral unit. One or more
polling stations. Your ward is a CAW.

## Ward
A geographic and electoral unit within a constituency. Each ward
has its own voter register.

## Constituency
A cluster of wards. Each constituency has its own ICT officer and
its own WhatsApp coordination group.

## County
A cluster of constituencies. Kenya has 47 counties.

## Phase
A time window during an election cycle. "2027 General Election
Phase 1" might run from June to September. Every phase has its
own voter register baseline.

## EVID
**Electronic Voter Identification System.** The device that
identifies voters on election day. Not the same as the
registration kit — you carry the registration kit.

## BVR
**Biometric Voter Registration.** The technology that captures
fingerprints and photos for voter registration. Every kit runs BVR.

## CVR
**Continuous Voter Registration.** The ongoing registration
exercise. When CVR is active, you register new voters every day.
When it's not, you register only during designated windows.

## ECVR
**Enhanced Continuous Voter Registration.** A larger, more
aggressive version of CVR, usually run 12–18 months before a
general election.

## RTS
**Results Transmission System.** Used on election day to send
results from polling stations to the national tallying centre.
Not part of your workflow — but you'll hear the term.

## Transferred
A voter who moved their registration from another polling station
to yours. Counts toward your daily total.

## Deleted
A voter removed from the register (usually by the office, not
in the field). Does not count toward your total.

## Drift
The difference between what a kit's screen shows and what the
system expects, based on reports. Small drift is normal. Large
drift means something needs checking.

## Baseline
For verification purposes: the cumulative total on a kit's screen
at a known point in time. Used to compare against new reports.

## Movement plan
A per-kit record of where each kit will be stationed on a
specific future date. Submitted daily for tomorrow.

## Idempotent
Technical term you'll see in error messages. Means "submitting
twice doesn't double the effect." The app handles this — you
don't need to.
""",
    },
}