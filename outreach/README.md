# Outreach — personal cold-email app

A local web app for running your outreach pipeline: contacts, templates,
previews, sending through your Gmail, and follow-up tracking. One Python
file + SQLite, runs on your machine only, nothing leaves except the
emails you explicitly send.

> This is an internal tool, not part of the portfolio site. The folder is
> self-contained — move it to its own repo whenever you want.

## Run it

```bash
cd outreach
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"

.venv/bin/python app.py
# → open http://127.0.0.1:5000
```

The app works without the Gmail variables — you just can't send until
they're set. To get an App Password: turn on 2-Step Verification, then
create one at https://myaccount.google.com/apppasswords. Credentials are
read from the environment only and never stored in the database.

All data lives in `outreach.db` next to `app.py` (gitignored, so contact
names and emails never end up in git).

## The workflow

1. **Contacts** — add people one at a time or import a CSV
   (`name, first_name, email, org, role, personal_line, notes`; only
   email required). The `personal_line` is the one sentence about *their*
   work that proves the email isn't a blast — write it per person, it's
   what gets replies. `first_name` overrides the greeting for cases like
   "Prof. Smith".
2. **Compose** — pick a template, see every eligible contact with their
   fully rendered email, uncheck anyone, and hit send. "Send one test to
   myself" delivers the first preview to your own inbox first.
3. **Dashboard** — pipeline counts, plus a "follow-ups due" queue:
   contacted people with no reply after N days (default 5, adjustable in
   Settings). One click composes the follow-up round; one click marks
   someone as replied.
4. **Contact pages** — every email ever sent to a person, their stage,
   editable details.

Stages move automatically: sending an initial email moves someone from
*To contact* → *Contacted*, a second send → *Followed up*. You flip
people to *Replied* or *Closed* yourself, and the app never emails
anyone in those stages again. It also never sends the same template to
the same person twice, so re-sending a batch is always safe.

## Guardrails built in

- Batches cap at 25 emails, with a 2–5 s pause between sends — small,
  personalized batches keep you out of Gmail's bulk-sender throttling
  and get better reply rates anyway.
- Everything is previewed exactly as it will send; a template with a
  typo'd `{placeholder}` fails loudly instead of sending broken emails.
- The app binds to 127.0.0.1 only — it's yours, not on the network.
- This is for individual professional outreach (jobs, labs, research).
  Commercial bulk email has legal requirements (CAN-SPAM, GDPR) this
  deliberately doesn't implement.
