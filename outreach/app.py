#!/usr/bin/env python3
"""Personal outreach app: a local dashboard for cold-email campaigns.

Run it, open http://127.0.0.1:5000, and manage contacts, templates,
sending, and follow-ups from the browser. Everything lives in a local
SQLite file (outreach.db) next to this script.

    pip install flask
    export GMAIL_ADDRESS="you@gmail.com"
    export GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"   # Google App Password
    python3 app.py
"""

import csv
import io
import os
import random
import smtplib
import sqlite3
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr
from pathlib import Path

from flask import Flask, flash, g, redirect, render_template, request, url_for
from jinja2 import DictLoader

DB_PATH = Path(__file__).resolve().parent / "outreach.db"
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 587
BATCH_CAP = 25  # max emails per send action; keeps Gmail happy

STAGES = ["to_contact", "contacted", "followup_sent", "replied", "closed"]
STAGE_LABELS = {
    "to_contact": "To contact",
    "contacted": "Contacted",
    "followup_sent": "Followed up",
    "replied": "Replied",
    "closed": "Closed",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    first_name TEXT DEFAULT '',
    email TEXT NOT NULL UNIQUE,
    org TEXT DEFAULT '',
    role TEXT DEFAULT '',
    personal_line TEXT DEFAULT '',
    stage TEXT DEFAULT 'to_contact',
    notes TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    subject TEXT NOT NULL,
    body TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sends (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    template_id INTEGER REFERENCES templates(id),
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""

SEED_TEMPLATES = [
    (
        "initial",
        "Cleanroom engineer (CVD/EBL) interested in {org}",
        "Hi {first_name},\n\n"
        "{personal_line}\n\n"
        "I'm a senior cleanroom tech at VINSE, Vanderbilt's Class-100 nanofabrication "
        "facility, where I own the CVD graphene line end to end and run e-beam "
        "lithography, ALD, and RIE — about 1,100 hours of hands-on fab time so far. "
        "Recent highlights: rebuilt our CVD reactor and cut its leak rate roughly 10x, "
        "taped out a chip on Sky130, and I build software that makes fabs more reliable.\n\n"
        "I'd love to talk about {role} work at {org}. Would you have 15 minutes in the "
        "next week or two?\n\n"
        "My work, with photos and numbers: https://www.linkedin.com/in/sahpar-ozer/\n\n"
        "Thanks for your time,\n{my_name}\nVanderbilt '28 - Nashville, TN",
    ),
    (
        "followup",
        "Re: Cleanroom engineer (CVD/EBL) interested in {org}",
        "Hi {first_name},\n\n"
        "Just floating this back to the top of your inbox — I know things get busy.\n\n"
        "Short version: senior cleanroom tech at Vanderbilt's Class-100 facility (VINSE), "
        "1,100+ hours across CVD, e-beam litho, ALD, and RIE, and I'd love 15 minutes to "
        "talk about {role} work at {org}.\n\n"
        "If now isn't the right time, no worries at all — and if there's someone better "
        "to ask, I'd appreciate a pointer.\n\n"
        "Best,\n{my_name}\nhttps://www.linkedin.com/in/sahpar-ozer/",
    ),
]

DEFAULT_SETTINGS = {"my_name": "Sahpar Nil Ozer", "followup_days": "5"}


# ---------------------------------------------------------------- database

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    for name, subject, body in SEED_TEMPLATES:
        db.execute(
            "INSERT OR IGNORE INTO templates (name, subject, body) VALUES (?,?,?)",
            (name, subject, body),
        )
    for key, value in DEFAULT_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?,?)", (key, value))
    db.commit()
    db.close()


def setting(key):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def days_since(iso):
    then = datetime.fromisoformat(iso)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).days


# ---------------------------------------------------------------- rendering

def merge_fields(contact):
    fields = {k: contact[k] or "" for k in contact.keys()}
    if not fields.get("first_name"):
        fields["first_name"] = (contact["name"] or "").split(" ")[0]
    fields["my_name"] = setting("my_name")
    return fields


def render_email(template, contact):
    fields = merge_fields(contact)
    try:
        return template["subject"].format_map(fields), template["body"].format_map(fields)
    except (KeyError, IndexError) as e:
        raise ValueError(f"Template uses unknown field {{{e.args[0]}}}") from e


def eligible_contacts(template_id):
    """Contacts this template can go to: not replied/closed, not already sent it."""
    return get_db().execute(
        """SELECT c.*,
                  (SELECT MAX(sent_at) FROM sends s WHERE s.contact_id = c.id) AS last_sent
           FROM contacts c
           WHERE c.stage NOT IN ('replied', 'closed')
             AND c.id NOT IN (SELECT contact_id FROM sends WHERE template_id = ?)
           ORDER BY c.org, c.name""",
        (template_id,),
    ).fetchall()


# ---------------------------------------------------------------- flask app

app = Flask(__name__)
app.secret_key = os.environ.get("OUTREACH_SECRET", "local-only-dev-key")


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.context_processor
def inject_globals():
    return {"STAGES": STAGES, "STAGE_LABELS": STAGE_LABELS}


@app.template_filter("ago")
def ago(iso):
    if not iso:
        return "never"
    d = days_since(iso)
    return "today" if d == 0 else f"{d}d ago"


@app.get("/")
def dashboard():
    db = get_db()
    counts = dict(
        db.execute("SELECT stage, COUNT(*) n FROM contacts GROUP BY stage").fetchall()
    )
    followup_days = int(setting("followup_days") or 5)
    due = db.execute(
        """SELECT c.*, MAX(s.sent_at) AS last_sent
           FROM contacts c JOIN sends s ON s.contact_id = c.id
           WHERE c.stage = 'contacted'
           GROUP BY c.id
           HAVING julianday('now') - julianday(MAX(s.sent_at)) >= ?
           ORDER BY last_sent""",
        (followup_days,),
    ).fetchall()
    recent = db.execute(
        """SELECT s.sent_at, s.subject, c.name, c.email, c.stage, c.id AS contact_id
           FROM sends s JOIN contacts c ON c.id = s.contact_id
           ORDER BY s.sent_at DESC LIMIT 12"""
    ).fetchall()
    followup_tpl = db.execute("SELECT id FROM templates WHERE name='followup'").fetchone()
    return render_template(
        "dashboard.html", counts=counts, due=due, recent=recent,
        followup_days=followup_days, followup_tpl=followup_tpl,
        creds_ok=bool(os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD")),
    )


# -------- contacts

@app.get("/contacts")
def contacts():
    stage = request.args.get("stage", "")
    q = "SELECT c.*, (SELECT MAX(sent_at) FROM sends s WHERE s.contact_id=c.id) AS last_sent FROM contacts c"
    args = ()
    if stage in STAGES:
        q += " WHERE c.stage = ?"
        args = (stage,)
    rows = get_db().execute(q + " ORDER BY c.created_at DESC", args).fetchall()
    return render_template("contacts.html", rows=rows, stage=stage)


@app.post("/contacts/add")
def contacts_add():
    f = request.form
    if not f.get("name", "").strip() or "@" not in f.get("email", ""):
        flash("Name and a valid email are required.", "err")
        return redirect(url_for("contacts"))
    try:
        get_db().execute(
            """INSERT INTO contacts (name, first_name, email, org, role, personal_line, notes, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (f["name"].strip(), f.get("first_name", "").strip(), f["email"].strip().lower(),
             f.get("org", "").strip(), f.get("role", "").strip(),
             f.get("personal_line", "").strip(), f.get("notes", "").strip(), now_iso()),
        )
        get_db().commit()
        flash(f"Added {f['name'].strip()}.", "ok")
    except sqlite3.IntegrityError:
        flash(f"{f['email'].strip()} is already in your contacts.", "err")
    return redirect(url_for("contacts"))


@app.post("/contacts/import")
def contacts_import():
    file = request.files.get("csv")
    if not file:
        flash("Choose a CSV file first.", "err")
        return redirect(url_for("contacts"))
    reader = csv.DictReader(io.StringIO(file.read().decode("utf-8-sig")))
    added = skipped = 0
    db = get_db()
    for row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        if "@" not in row.get("email", ""):
            skipped += 1
            continue
        try:
            db.execute(
                """INSERT INTO contacts (name, first_name, email, org, role, personal_line, notes, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (row.get("name", row["email"]), row.get("first_name", ""), row["email"].lower(),
                 row.get("org", ""), row.get("role", ""), row.get("personal_line", ""),
                 row.get("notes", ""), now_iso()),
            )
            added += 1
        except sqlite3.IntegrityError:
            skipped += 1
    db.commit()
    flash(f"Imported {added} contact(s), skipped {skipped} (duplicates or no email).", "ok")
    return redirect(url_for("contacts"))


@app.post("/contacts/<int:cid>/update")
def contacts_update(cid):
    f = request.form
    db = get_db()
    if "stage" in f and f["stage"] in STAGES:
        db.execute("UPDATE contacts SET stage=? WHERE id=?", (f["stage"], cid))
    for field in ("name", "first_name", "email", "org", "role", "personal_line", "notes"):
        if field in f:
            db.execute(f"UPDATE contacts SET {field}=? WHERE id=?", (f[field].strip(), cid))
    db.commit()
    return redirect(request.referrer or url_for("contacts"))


@app.post("/contacts/<int:cid>/delete")
def contacts_delete(cid):
    db = get_db()
    db.execute("DELETE FROM contacts WHERE id=?", (cid,))
    db.commit()
    flash("Contact deleted.", "ok")
    return redirect(url_for("contacts"))


@app.get("/contacts/<int:cid>")
def contact_detail(cid):
    db = get_db()
    contact = db.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
    if not contact:
        flash("No such contact.", "err")
        return redirect(url_for("contacts"))
    history = db.execute(
        "SELECT * FROM sends WHERE contact_id=? ORDER BY sent_at DESC", (cid,)
    ).fetchall()
    return render_template("contact_detail.html", c=contact, history=history)


# -------- templates

@app.get("/templates")
def templates_page():
    rows = get_db().execute("SELECT * FROM templates ORDER BY id").fetchall()
    return render_template("templates.html", rows=rows)


@app.post("/templates/save")
def templates_save():
    f = request.form
    db = get_db()
    if f.get("id"):
        db.execute("UPDATE templates SET name=?, subject=?, body=? WHERE id=?",
                   (f["name"].strip(), f["subject"].strip(), f["body"], f["id"]))
    else:
        try:
            db.execute("INSERT INTO templates (name, subject, body) VALUES (?,?,?)",
                       (f["name"].strip(), f["subject"].strip(), f["body"]))
        except sqlite3.IntegrityError:
            flash("A template with that name already exists.", "err")
            return redirect(url_for("templates_page"))
    db.commit()
    flash("Template saved.", "ok")
    return redirect(url_for("templates_page"))


@app.post("/templates/<int:tid>/delete")
def templates_delete(tid):
    db = get_db()
    db.execute("DELETE FROM templates WHERE id=?", (tid,))
    db.commit()
    flash("Template deleted (send history kept).", "ok")
    return redirect(url_for("templates_page"))


# -------- compose & send

@app.get("/compose")
def compose():
    db = get_db()
    tpls = db.execute("SELECT * FROM templates ORDER BY id").fetchall()
    if not tpls:
        flash("Create a template first.", "err")
        return redirect(url_for("templates_page"))
    tid = request.args.get("template", type=int) or tpls[0]["id"]
    template = db.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone() or tpls[0]
    min_days = request.args.get("min_days", type=int) or 0
    previews, errors = [], []
    for c in eligible_contacts(template["id"]):
        if min_days and (not c["last_sent"] or days_since(c["last_sent"]) < min_days):
            continue
        try:
            subject, body = render_email(template, c)
            previews.append((c, subject, body))
        except ValueError as e:
            errors.append(f"{c['email']}: {e}")
    for e in errors:
        flash(e, "err")
    return render_template(
        "compose.html", tpls=tpls, template=template, previews=previews,
        min_days=min_days, batch_cap=BATCH_CAP,
        creds_ok=bool(os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD")),
    )


@app.post("/send")
def send():
    db = get_db()
    template = db.execute(
        "SELECT * FROM templates WHERE id=?", (request.form["template_id"],)
    ).fetchone()
    ids = request.form.getlist("contact_id")[:BATCH_CAP]
    test_mode = "test" in request.form
    sender = os.environ.get("GMAIL_ADDRESS", "")
    password = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not sender or not password:
        flash("Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD before sending (see README).", "err")
        return redirect(url_for("compose", template=template["id"]))
    if not ids:
        flash("Nobody selected.", "err")
        return redirect(url_for("compose", template=template["id"]))

    my_name = setting("my_name")
    sent = 0
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(sender, password)
            for i, cid in enumerate(ids):
                c = db.execute("SELECT * FROM contacts WHERE id=?", (cid,)).fetchone()
                if not c:
                    continue
                subject, body = render_email(template, c)
                msg = EmailMessage()
                msg["From"] = formataddr((my_name, sender))
                msg["Subject"] = f"[TEST] {subject}" if test_mode else subject
                msg["To"] = formataddr((my_name, sender)) if test_mode \
                    else formataddr((c["name"], c["email"]))
                msg.set_content(body)
                smtp.send_message(msg)
                sent += 1
                if test_mode:
                    break  # one test email is enough
                db.execute(
                    "INSERT INTO sends (contact_id, template_id, subject, body, sent_at) VALUES (?,?,?,?,?)",
                    (c["id"], template["id"], subject, body, now_iso()),
                )
                new_stage = "followup_sent" if c["stage"] == "contacted" else "contacted"
                if c["stage"] in ("to_contact", "contacted"):
                    db.execute("UPDATE contacts SET stage=? WHERE id=?", (new_stage, c["id"]))
                db.commit()
                if i < len(ids) - 1:
                    time.sleep(random.uniform(2, 5))
    except smtplib.SMTPAuthenticationError:
        flash("Gmail rejected the login — check GMAIL_ADDRESS / GMAIL_APP_PASSWORD.", "err")
        return redirect(url_for("compose", template=template["id"]))
    except (smtplib.SMTPException, OSError) as e:
        db.commit()
        flash(f"Stopped after {sent} send(s): {e}", "err")
        return redirect(url_for("compose", template=template["id"]))

    if test_mode:
        flash(f"Test email sent to {sender}. Nothing logged.", "ok")
    else:
        flash(f"Sent {sent} email(s) with '{template['name']}'.", "ok")
    return redirect(url_for("compose", template=template["id"]))


# -------- settings

@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    db = get_db()
    if request.method == "POST":
        for key in ("my_name", "followup_days"):
            if key in request.form:
                db.execute("REPLACE INTO settings (key, value) VALUES (?,?)",
                           (key, request.form[key].strip()))
        db.commit()
        flash("Settings saved.", "ok")
        return redirect(url_for("settings_page"))
    return render_template(
        "settings.html", my_name=setting("my_name"),
        followup_days=setting("followup_days"),
        gmail=os.environ.get("GMAIL_ADDRESS", ""),
        has_password=bool(os.environ.get("GMAIL_APP_PASSWORD")),
    )


# ---------------------------------------------------------------- HTML

BASE = """<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Outreach</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#F6F8FA;--ink:#131C28;--steel:#5D6B7A;--line:#DEE4EA;--blue:#1D5FBF;--blue-soft:#E2ECF9;--card:#fff}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:15px/1.55 "IBM Plex Sans",system-ui,sans-serif}
.mono{font-family:"IBM Plex Mono",monospace;font-size:12px;letter-spacing:.04em;color:var(--steel)}
a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
header{position:sticky;top:0;background:rgba(246,248,250,.92);backdrop-filter:blur(6px);border-bottom:1px solid var(--line);z-index:5}
.nav{max-width:1060px;margin:0 auto;padding:14px 24px;display:flex;gap:22px;align-items:baseline}
.nav .brand{font-weight:600}
.nav a{color:var(--ink);font-size:14px}.nav a.on{color:var(--blue);font-weight:500}
main{max-width:1060px;margin:0 auto;padding:28px 24px 64px}
h1{font-size:22px;font-weight:600;letter-spacing:-.015em;margin-bottom:18px}
h2{font-size:15px;font-weight:600;margin:26px 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:4px;padding:18px 20px;margin-bottom:16px}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;font-family:"IBM Plex Mono",monospace;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--steel);padding:6px 10px;border-bottom:1px solid var(--line)}
td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
.tag{font-family:"IBM Plex Mono",monospace;font-size:11px;background:var(--blue-soft);color:var(--ink);padding:2px 8px;border-radius:2px;white-space:nowrap}
.btn{display:inline-block;font-size:13px;font-weight:500;padding:7px 14px;border-radius:3px;border:1px solid var(--ink);background:none;color:var(--ink);cursor:pointer}
.btn:hover{background:var(--ink);color:var(--bg)}
.btn.primary{background:var(--ink);color:var(--bg)}.btn.primary:hover{background:var(--blue);border-color:var(--blue)}
.btn.small{font-size:12px;padding:3px 9px}
.btn.danger{border-color:#B3413B;color:#B3413B}.btn.danger:hover{background:#B3413B;color:#fff}
input[type=text],input[type=email],input[type=number],textarea,select{width:100%;font:inherit;padding:7px 10px;border:1px solid var(--line);border-radius:3px;background:#fff}
textarea{font-family:"IBM Plex Mono",monospace;font-size:13px;min-height:180px}
label{display:block;font-family:"IBM Plex Mono",monospace;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--steel);margin:12px 0 4px}
.flash{padding:10px 14px;border-radius:3px;margin-bottom:14px;font-size:14px}
.flash.ok{background:var(--blue-soft)}.flash.err{background:#F9E3E2}
.grid{display:grid;gap:14px}.grid.cols{grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.stat{background:var(--card);border:1px solid var(--line);border-radius:4px;padding:14px 16px}
.stat .n{font-size:24px;font-weight:600}.stat .l{font-family:"IBM Plex Mono",monospace;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--steel)}
details.preview{border:1px solid var(--line);border-radius:3px;margin-bottom:8px;background:#fff}
details.preview summary{padding:9px 12px;cursor:pointer;font-size:14px;display:flex;gap:10px;align-items:center}
details.preview pre{padding:12px 16px;border-top:1px solid var(--line);white-space:pre-wrap;font:13px/1.5 "IBM Plex Mono",monospace;color:#2A3340}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
form.inline{display:inline}
.muted{color:var(--steel);font-size:13px}
</style></head><body>
<header><nav class="nav">
<span class="brand">outreach</span>
<a href="{{ url_for('dashboard') }}" class="{{ 'on' if request.endpoint=='dashboard' }}">Dashboard</a>
<a href="{{ url_for('contacts') }}" class="{{ 'on' if request.endpoint and request.endpoint.startswith('contact') }}">Contacts</a>
<a href="{{ url_for('compose') }}" class="{{ 'on' if request.endpoint in ('compose','send') }}">Compose</a>
<a href="{{ url_for('templates_page') }}" class="{{ 'on' if request.endpoint=='templates_page' }}">Templates</a>
<a href="{{ url_for('settings_page') }}" class="{{ 'on' if request.endpoint=='settings_page' }}">Settings</a>
</nav></header>
<main>
{% for cat, m in get_flashed_messages(with_categories=true) %}<div class="flash {{ cat }}">{{ m }}</div>{% endfor %}
{% block content %}{% endblock %}
</main></body></html>"""

DASHBOARD = """{% extends "base.html" %}{% block content %}
<h1>Dashboard</h1>
{% if not creds_ok %}<div class="flash err">Gmail credentials not set — you can manage everything, but sending is disabled. See Settings.</div>{% endif %}
<div class="grid cols">
{% for s in STAGES %}<div class="stat"><div class="n">{{ counts.get(s, 0) }}</div><div class="l">{{ STAGE_LABELS[s] }}</div></div>{% endfor %}
</div>

<h2>Follow-ups due ({{ followup_days }}+ days, no reply)</h2>
<div class="card">
{% if due %}
<table><tr><th>Name</th><th>Org</th><th>Last emailed</th><th></th></tr>
{% for c in due %}<tr>
<td><a href="{{ url_for('contact_detail', cid=c.id) }}">{{ c.name }}</a></td>
<td>{{ c.org }}</td><td class="mono">{{ c.last_sent | ago }}</td>
<td><form class="inline" method="post" action="{{ url_for('contacts_update', cid=c.id) }}">
<input type="hidden" name="stage" value="replied"><button class="btn small">They replied</button></form></td>
</tr>{% endfor %}</table>
{% if followup_tpl %}<p style="margin-top:12px"><a class="btn primary" href="{{ url_for('compose', template=followup_tpl.id, min_days=followup_days) }}">Compose follow-ups →</a></p>{% endif %}
{% else %}<p class="muted">Nothing due. Add contacts and send an initial round, or wait out the follow-up window.</p>{% endif %}
</div>

<h2>Recent activity</h2>
<div class="card">
{% if recent %}
<table><tr><th>When</th><th>To</th><th>Subject</th><th>Stage</th></tr>
{% for s in recent %}<tr>
<td class="mono">{{ s.sent_at | ago }}</td>
<td><a href="{{ url_for('contact_detail', cid=s.contact_id) }}">{{ s.name }}</a></td>
<td>{{ s.subject }}</td><td><span class="tag">{{ STAGE_LABELS[s.stage] }}</span></td>
</tr>{% endfor %}</table>
{% else %}<p class="muted">No emails sent yet.</p>{% endif %}
</div>
{% endblock %}"""

CONTACTS = """{% extends "base.html" %}{% block content %}
<h1>Contacts</h1>
<div class="row" style="margin-bottom:14px">
<a class="btn small {{ 'primary' if not stage }}" href="{{ url_for('contacts') }}">All</a>
{% for s in STAGES %}<a class="btn small {{ 'primary' if stage==s }}" href="{{ url_for('contacts', stage=s) }}">{{ STAGE_LABELS[s] }}</a>{% endfor %}
</div>
<div class="card">
{% if rows %}
<table><tr><th>Name</th><th>Email</th><th>Org / role</th><th>Stage</th><th>Last emailed</th><th></th></tr>
{% for c in rows %}<tr>
<td><a href="{{ url_for('contact_detail', cid=c.id) }}">{{ c.name }}</a></td>
<td class="mono">{{ c.email }}</td>
<td>{{ c.org }}{% if c.role %} <span class="muted">· {{ c.role }}</span>{% endif %}</td>
<td><form class="inline" method="post" action="{{ url_for('contacts_update', cid=c.id) }}">
<select name="stage" onchange="this.form.submit()" style="width:auto;font-size:12px;padding:3px 6px">
{% for s in STAGES %}<option value="{{ s }}" {{ 'selected' if c.stage==s }}>{{ STAGE_LABELS[s] }}</option>{% endfor %}
</select></form></td>
<td class="mono">{{ c.last_sent | ago }}</td>
<td><form class="inline" method="post" action="{{ url_for('contacts_delete', cid=c.id) }}"
 onsubmit="return confirm('Delete {{ c.name }} and their send history?')">
<button class="btn small danger">×</button></form></td>
</tr>{% endfor %}</table>
{% else %}<p class="muted">No contacts{% if stage %} in this stage{% endif %} yet.</p>{% endif %}
</div>

<h2>Add a contact</h2>
<div class="card"><form method="post" action="{{ url_for('contacts_add') }}">
<div class="grid" style="grid-template-columns:1fr 1fr 1fr">
<div><label>Name</label><input type="text" name="name" required placeholder="Jane Doe"></div>
<div><label>Greeting override</label><input type="text" name="first_name" placeholder="Prof. Doe (optional)"></div>
<div><label>Email</label><input type="email" name="email" required placeholder="jane@company.com"></div>
<div><label>Org</label><input type="text" name="org" placeholder="Example Semiconductors"></div>
<div><label>Role you're asking about</label><input type="text" name="role" placeholder="process engineering internship"></div>
<div><label>Notes</label><input type="text" name="notes"></div>
</div>
<label>Personal line — one sentence about *their* work; this is what gets replies</label>
<input type="text" name="personal_line" placeholder="I saw your IEDM talk on 300mm ALD integration and…">
<p style="margin-top:14px"><button class="btn primary">Add contact</button></p>
</form></div>

<h2>Import CSV</h2>
<div class="card"><form method="post" action="{{ url_for('contacts_import') }}" enctype="multipart/form-data">
<p class="muted">Columns: <span class="mono">name, first_name, email, org, role, personal_line, notes</span> — only email is required. Duplicates are skipped.</p>
<p style="margin-top:10px" class="row"><input type="file" name="csv" accept=".csv" style="width:auto">
<button class="btn">Import</button></p>
</form></div>
{% endblock %}"""

CONTACT_DETAIL = """{% extends "base.html" %}{% block content %}
<h1>{{ c.name }} <span class="tag">{{ STAGE_LABELS[c.stage] }}</span></h1>
<div class="card"><form method="post" action="{{ url_for('contacts_update', cid=c.id) }}">
<div class="grid" style="grid-template-columns:1fr 1fr 1fr">
<div><label>Name</label><input type="text" name="name" value="{{ c.name }}"></div>
<div><label>Greeting override</label><input type="text" name="first_name" value="{{ c.first_name }}"></div>
<div><label>Email</label><input type="email" name="email" value="{{ c.email }}"></div>
<div><label>Org</label><input type="text" name="org" value="{{ c.org }}"></div>
<div><label>Role</label><input type="text" name="role" value="{{ c.role }}"></div>
<div><label>Stage</label><select name="stage">{% for s in STAGES %}<option value="{{ s }}" {{ 'selected' if c.stage==s }}>{{ STAGE_LABELS[s] }}</option>{% endfor %}</select></div>
</div>
<label>Personal line</label><input type="text" name="personal_line" value="{{ c.personal_line }}">
<label>Notes</label><input type="text" name="notes" value="{{ c.notes }}">
<p style="margin-top:14px"><button class="btn primary">Save</button>
<a class="btn" href="{{ url_for('contacts') }}">Back</a></p>
</form></div>

<h2>Email history</h2>
{% if history %}{% for s in history %}
<details class="preview"><summary><span class="mono">{{ s.sent_at | ago }}</span> {{ s.subject }}</summary>
<pre>{{ s.body }}</pre></details>
{% endfor %}{% else %}<p class="muted">Never emailed.</p>{% endif %}
{% endblock %}"""

TEMPLATES_HTML = """{% extends "base.html" %}{% block content %}
<h1>Templates</h1>
<p class="muted" style="margin-bottom:16px">Placeholders: <span class="mono">{first_name} {name} {org} {role} {personal_line} {my_name}</span></p>
{% for t in rows %}
<div class="card"><form method="post" action="{{ url_for('templates_save') }}">
<input type="hidden" name="id" value="{{ t.id }}">
<div class="grid" style="grid-template-columns:220px 1fr">
<div><label>Name</label><input type="text" name="name" value="{{ t.name }}"></div>
<div><label>Subject</label><input type="text" name="subject" value="{{ t.subject }}"></div>
</div>
<label>Body</label><textarea name="body">{{ t.body }}</textarea>
<p style="margin-top:12px" class="row"><button class="btn primary">Save</button>
<a class="btn" href="{{ url_for('compose', template=t.id) }}">Compose with this →</a>
<form class="inline" method="post" action="{{ url_for('templates_delete', tid=t.id) }}"
 onsubmit="return confirm('Delete template {{ t.name }}?')"><button class="btn danger">Delete</button></form></p>
</form></div>
{% endfor %}

<h2>New template</h2>
<div class="card"><form method="post" action="{{ url_for('templates_save') }}">
<div class="grid" style="grid-template-columns:220px 1fr">
<div><label>Name</label><input type="text" name="name" required placeholder="conference-intro"></div>
<div><label>Subject</label><input type="text" name="subject" required></div>
</div>
<label>Body</label><textarea name="body"></textarea>
<p style="margin-top:12px"><button class="btn primary">Create</button></p>
</form></div>
{% endblock %}"""

COMPOSE = """{% extends "base.html" %}{% block content %}
<h1>Compose</h1>
<div class="card"><form method="get" action="{{ url_for('compose') }}" class="row">
<div><label>Template</label>
<select name="template" onchange="this.form.submit()" style="width:auto">
{% for t in tpls %}<option value="{{ t.id }}" {{ 'selected' if t.id==template.id }}>{{ t.name }}</option>{% endfor %}
</select></div>
<div><label>Only if last emailed ≥ N days ago</label>
<input type="number" name="min_days" value="{{ min_days }}" min="0" style="width:90px" onchange="this.form.submit()"></div>
</form></div>

{% if not creds_ok %}<div class="flash err">Sending disabled: set GMAIL_ADDRESS and GMAIL_APP_PASSWORD, then restart the app.</div>{% endif %}

<form method="post" action="{{ url_for('send') }}">
<input type="hidden" name="template_id" value="{{ template.id }}">
<h2>{{ previews | length }} eligible contact(s) <span class="muted">(already-sent, replied, and closed are excluded; {{ batch_cap }} max per batch)</span></h2>
{% if previews %}
<p style="margin-bottom:10px" class="row">
<button type="button" class="btn small" onclick="document.querySelectorAll('.pick').forEach(c=>c.checked=true)">Select all</button>
<button type="button" class="btn small" onclick="document.querySelectorAll('.pick').forEach(c=>c.checked=false)">Select none</button>
</p>
{% for c, subject, body in previews %}
<details class="preview"><summary>
<input type="checkbox" class="pick" name="contact_id" value="{{ c.id }}" checked onclick="event.stopPropagation()">
<strong>{{ c.name }}</strong> <span class="mono">{{ c.email }}</span>
<span class="muted">{{ subject }}</span>
</summary><pre>{{ body }}</pre></details>
{% endfor %}
<p style="margin-top:16px" class="row">
<button class="btn primary" {{ '' if creds_ok else 'disabled' }}
 onclick="return confirm('Send to every checked contact?')">Send selected</button>
<button class="btn" name="test" value="1" {{ '' if creds_ok else 'disabled' }}>Send one test to myself</button>
</p>
{% else %}<p class="muted">Nobody eligible for this template. Add contacts, or lower the day filter.</p>{% endif %}
</form>
{% endblock %}"""

SETTINGS = """{% extends "base.html" %}{% block content %}
<h1>Settings</h1>
<div class="card"><form method="post">
<label>Your name (From header, {my_name})</label>
<input type="text" name="my_name" value="{{ my_name }}" style="max-width:340px">
<label>Follow-up window (days)</label>
<input type="number" name="followup_days" value="{{ followup_days }}" min="1" style="width:90px">
<p style="margin-top:14px"><button class="btn primary">Save</button></p>
</form></div>

<h2>Gmail credentials</h2>
<div class="card">
<p>Read from environment variables when the app starts — never stored in the database.</p>
<table style="margin-top:10px">
<tr><td class="mono">GMAIL_ADDRESS</td><td>{{ gmail or '— not set —' }}</td></tr>
<tr><td class="mono">GMAIL_APP_PASSWORD</td><td>{{ 'set' if has_password else '— not set —' }}</td></tr>
</table>
<p class="muted" style="margin-top:10px">Create an App Password at
myaccount.google.com/apppasswords (requires 2-Step Verification), then restart the app with both variables exported.</p>
</div>
{% endblock %}"""

app.jinja_loader = DictLoader({
    "base.html": BASE,
    "dashboard.html": DASHBOARD,
    "contacts.html": CONTACTS,
    "contact_detail.html": CONTACT_DETAIL,
    "templates.html": TEMPLATES_HTML,
    "compose.html": COMPOSE,
    "settings.html": SETTINGS,
})


if __name__ == "__main__":
    init_db()
    app.run(host="127.0.0.1", port=5000, debug=False)
