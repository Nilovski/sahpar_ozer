# Cold email outreach

A small mail-merge tool for personal outreach — recruiters, lab PIs,
internship contacts. One Python file, no dependencies, sends through your
Gmail account. Dry-run by default so you always see exactly what will go
out before anything is sent.

## One-time setup

1. **Gmail App Password** (regular password won't work):
   - Turn on 2-Step Verification on your Google account.
   - Go to https://myaccount.google.com/apppasswords and create one
     (name it "outreach"). You get a 16-character password.
2. **Export credentials** in your terminal (or put in `~/.bashrc`):
   ```bash
   export GMAIL_ADDRESS="you@gmail.com"
   export GMAIL_APP_PASSWORD="abcd efgh ijkl mnop"
   ```
3. **Create your contact list:**
   ```bash
   cp contacts.example.csv contacts.csv
   ```
   Then fill in real people. Columns:
   | column | used for |
   |---|---|
   | `name` | To: header; `{first_name}` in templates |
   | `first_name` | optional greeting override — leave blank to use the first word of `name`; set it for professors ("Prof. Smith") |
   | `email` | recipient address |
   | `org` | `{org}` in templates |
   | `role` | `{role}` — what you're asking about ("process engineering internship") |
   | `personal_line` | `{personal_line}` — the one sentence that proves you didn't blast this. **Write it per person.** This is what gets replies. |

   `contacts.csv` and `sent_log.csv` are gitignored — real names and
   emails never end up in the repo.

## Daily use

```bash
python3 send.py                    # dry run: prints every rendered email
python3 send.py --send             # send them (asks you to type 'send')
python3 send.py --send --attach resume.pdf --limit 15
```

Every send is recorded in `sent_log.csv`. Re-running never emails the
same person the same template twice, so you can keep adding rows to
`contacts.csv` and re-running.

**Follow-ups** — after ~5 days with no reply:

```bash
python3 send.py --template followup --min-days-since 5 --send
```

When someone replies, put `y` in the `replied` column of `sent_log.csv`
and the tool stops emailing them entirely.

## Editing templates

Templates live in `templates/*.txt`: first line is the subject, then a
blank line, then the body. Placeholders in `{braces}` map to CSV columns,
plus `{first_name}` (derived from `name`) and `{my_name}`. Add a new
template as `templates/whatever.txt` and use `--template whatever`.

## Staying effective (and out of spam folders)

- **Volume:** keep it to ~15–25/day (the `--limit` default is 20). Gmail
  throttles bulk senders, and small batches with real personalization
  outperform blasts anyway. The tool already pauses ~1 minute between
  sends (`--delay`).
- **Personalize:** a generic `personal_line` is worse than none. One
  specific sentence about *their* work is the highest-leverage thing here.
- **One follow-up, maybe two.** If someone asks you to stop, mark them
  `replied` so they're never contacted again.
- This is for individual professional outreach (job search, research
  contacts). Don't use it for commercial bulk email — that has legal
  requirements (CAN-SPAM, GDPR) this tool doesn't implement.
