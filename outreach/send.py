#!/usr/bin/env python3
"""Cold-email mail merge for personal outreach (jobs, labs, recruiters).

Standard library only. Dry-run by default; nothing is sent until you pass
--send. Reads contacts from a CSV, renders a template per contact, sends
through Gmail SMTP, and records every send in sent_log.csv so the same
person is never emailed twice with the same template.

Quick start:
    python3 send.py                          # dry run: prints rendered emails
    python3 send.py --send                   # actually send (asks to confirm)
    python3 send.py --template followup --send --min-days-since 5

Credentials come from environment variables:
    GMAIL_ADDRESS       your Gmail address
    GMAIL_APP_PASSWORD  a Gmail App Password (see README.md)
"""

import argparse
import csv
import os
import random
import smtplib
import sys
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTACTS_CSV = HERE / "contacts.csv"
LOG_CSV = HERE / "sent_log.csv"
TEMPLATES_DIR = HERE / "templates"

LOG_FIELDS = ["email", "template", "sent_at", "subject", "replied"]

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


def load_template(name):
    path = TEMPLATES_DIR / f"{name}.txt"
    if not path.exists():
        available = ", ".join(p.stem for p in sorted(TEMPLATES_DIR.glob("*.txt")))
        sys.exit(f"Template '{name}' not found. Available: {available}")
    raw = path.read_text(encoding="utf-8")
    if "\n" not in raw:
        sys.exit(f"Template {path} must have a subject line, a blank line, then the body.")
    subject, _, body = raw.partition("\n\n")
    subject = subject.removeprefix("Subject:").strip()
    return subject, body.strip() + "\n"


def load_contacts():
    if not CONTACTS_CSV.exists():
        sys.exit(
            f"No {CONTACTS_CSV.name} found. Copy contacts.example.csv to "
            f"contacts.csv and fill in real people."
        )
    with CONTACTS_CSV.open(newline="", encoding="utf-8") as f:
        rows = [
            {k.strip(): (v or "").strip() for k, v in row.items()}
            for row in csv.DictReader(f)
        ]
    contacts = [r for r in rows if r.get("email")]
    for c in contacts:
        if "@" not in c["email"]:
            sys.exit(f"Bad email address in contacts.csv: {c['email']!r}")
    return contacts


def load_log():
    if not LOG_CSV.exists():
        return []
    with LOG_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_log(entry):
    new_file = not LOG_CSV.exists()
    with LOG_CSV.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(entry)


def days_since(iso_timestamp):
    then = datetime.fromisoformat(iso_timestamp)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).days


def select_recipients(contacts, log, template, min_days_since):
    """Contacts eligible for this template, given the send history."""
    sent_this_template = {e["email"].lower() for e in log if e["template"] == template}
    last_sent = {}
    replied = set()
    for e in log:
        addr = e["email"].lower()
        last_sent[addr] = e["sent_at"]
        if e.get("replied", "").lower() in ("y", "yes", "true", "1"):
            replied.add(addr)

    selected, skipped = [], []
    for c in contacts:
        addr = c["email"].lower()
        if addr in sent_this_template:
            skipped.append((c, "already sent this template"))
        elif addr in replied:
            skipped.append((c, "marked replied in sent_log.csv"))
        elif min_days_since and addr not in last_sent:
            skipped.append((c, "never contacted, nothing to follow up on"))
        elif min_days_since and days_since(last_sent[addr]) < min_days_since:
            skipped.append((c, f"last contacted {days_since(last_sent[addr])}d ago"))
        else:
            selected.append(c)
    return selected, skipped


def render(template_text, contact, sender_name):
    fields = dict(contact)
    if not fields.get("first_name"):
        fields["first_name"] = contact.get("name", "").split(" ")[0]
    fields["my_name"] = sender_name
    try:
        return template_text.format_map(fields)
    except KeyError as e:
        sys.exit(
            f"Template uses {{{e.args[0]}}} but contacts.csv has no such column "
            f"(row for {contact['email']}). Columns: {', '.join(contact)}"
        )


def build_message(sender_name, sender_addr, contact, subject, body, attachment):
    msg = EmailMessage()
    msg["From"] = formataddr((sender_name, sender_addr))
    msg["To"] = formataddr((contact.get("name", ""), contact["email"]))
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment:
        data = attachment.read_bytes()
        maintype, subtype = ("application", "pdf") if attachment.suffix.lower() == ".pdf" else ("application", "octet-stream")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=attachment.name)
    return msg


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--template", default="initial", help="template name in templates/ (default: initial)")
    parser.add_argument("--send", action="store_true", help="actually send; without this it's a dry run")
    parser.add_argument("--limit", type=int, default=20, help="max emails this run (default: 20)")
    parser.add_argument("--min-days-since", type=int, default=0, metavar="N",
                        help="only email people last contacted at least N days ago (for follow-ups)")
    parser.add_argument("--attach", type=Path, help="file to attach, e.g. resume.pdf")
    parser.add_argument("--delay", type=int, default=60, metavar="SECONDS",
                        help="average pause between sends (default: 60)")
    parser.add_argument("--name", default="Sahpar Nil Ozer", help="your name for the From header and {my_name}")
    args = parser.parse_args()

    if args.attach and not args.attach.exists():
        sys.exit(f"Attachment not found: {args.attach}")

    subject_tmpl, body_tmpl = load_template(args.template)
    contacts = load_contacts()
    log = load_log()
    selected, skipped = select_recipients(contacts, log, args.template, args.min_days_since)

    for contact, reason in skipped:
        print(f"skip  {contact['email']:<40} {reason}")
    if not selected:
        print("Nobody eligible to email. Done.")
        return
    if len(selected) > args.limit:
        print(f"note  {len(selected)} eligible, capping at --limit {args.limit}")
        selected = selected[: args.limit]

    previews = []
    for contact in selected:
        subject = render(subject_tmpl, contact, args.name)
        body = render(body_tmpl, contact, args.name)
        previews.append((contact, subject, body))

    if not args.send:
        for contact, subject, body in previews:
            print("\n" + "=" * 72)
            print(f"To:      {contact.get('name', '')} <{contact['email']}>")
            print(f"Subject: {subject}\n")
            print(body)
        print("=" * 72)
        print(f"\nDry run: {len(previews)} email(s) rendered, nothing sent. "
              f"Re-run with --send to send them.")
        return

    sender_addr = os.environ.get("GMAIL_ADDRESS")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not sender_addr or not password:
        sys.exit("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD (see README.md).")

    print(f"\nAbout to send {len(previews)} email(s) as {args.name} <{sender_addr}> "
          f"using template '{args.template}'.")
    if input("Type 'send' to confirm: ").strip().lower() != "send":
        print("Aborted, nothing sent.")
        return

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(sender_addr, password)
        for i, (contact, subject, body) in enumerate(previews):
            msg = build_message(args.name, sender_addr, contact, subject, body, args.attach)
            smtp.send_message(msg)
            append_log({
                "email": contact["email"],
                "template": args.template,
                "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "subject": subject,
                "replied": "",
            })
            print(f"sent  {contact['email']}  ({i + 1}/{len(previews)})")
            if i < len(previews) - 1:
                pause = args.delay * random.uniform(0.7, 1.3)
                time.sleep(pause)

    print(f"\nDone: {len(previews)} sent, logged to {LOG_CSV.name}.")


if __name__ == "__main__":
    main()
