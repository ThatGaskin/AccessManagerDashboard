# Access Manager Toolkit

A local, browser-based toolkit for the bulk operations access managers do — starting with **creating
access profiles from a CSV file** (and rolling them back). It talks to the target system's REST API,
shows live progress, lets you **pause, resume or cancel at any time**, and makes you **review and
approve exactly what will change** before anything is sent.

It runs entirely on your machine: a small Python web server bound to `127.0.0.1` and a single SQLite
file. No external database, no cloud service, no CDN — the UI works offline.

Target system today: **SailPoint Identity Security Cloud (IdentityNow)**, plus an offline **Sandbox**
connector for practice, demos and training.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py                    # opens http://127.0.0.1:8765
```

1. **Connections → Add Sandbox (offline)** and save.
2. **New job** → action *Create access profiles* → upload `examples/access_profiles_sandbox.csv`.
3. Review the plan: fix rows inline or exclude them, then **Review & approve…** → **Start**.
4. Try **Pause**, edit a pending row, re-approve, **Resume**. Afterwards try **Create rollback…**.

Options: `--port 8765`, `--data-dir ./data`, `--no-browser`, `--debug` (or `AMT_PORT`, `AMT_DATA_DIR`).

## How a bulk job works

```
 upload CSV ──► PLAN ──► REVIEW ──► APPROVE ──► RUN ⇄ PAUSE ──► DONE
              read-only   edit /     bound to     row by row,   export results,
              lookups     exclude    a hash of    saved after   retry failed,
                          rows       the plan     every row     rollback
```

* **Plan (dry run).** Every row is validated and resolved against the live API using read-only calls:
  owner → identity ID, source → source ID, entitlements → entitlement IDs, and "does a profile with
  this name already exist?". The result is the exact JSON payload that would be sent, visible per row.
* **Review.** Filter by errors / warnings / planned action, search, and open any row to edit its values
  side-by-side with the payload. Edited fields show the original CSV value. Saving re-plans that row
  immediately. Rows can be excluded individually, by selection, or "exclude all rows with errors".
  Duplicate names within the file are flagged.
* **Approve.** Shows a summary and sample of the changes. Approval is bound to a SHA-256 fingerprint
  of everything that will be sent (connection, options, every included row's revision and payload).
  If anything changes afterwards — an edit, an exclusion, a re-plan, a change made in another browser
  tab — the approval no longer matches and the job cannot start until it is approved again.
  Connections marked **production** additionally require typing the connection name.
* **Run.** Rows are executed one at a time in CSV order on a background thread. Status is committed
  to SQLite after every row, and progress, rate and ETA update live in the UI.
* **Pause / resume.** Pause finishes the current row and stops. While paused you can edit or exclude
  rows that have not run yet (rows that already ran are locked) — doing so requires re-approval
  before resuming. **Cancel** stops for good.
* **Done.** Export a results CSV, **retry failed** rows, or **create a rollback job**. A rollback is an
  ordinary job (delete the profiles this job created) that goes through the same plan → review →
  approve flow — nothing is deleted without approval.

## Safety and reliability

| Concern | What the toolkit does |
|---|---|
| Nothing happens by accident | Planning is read-only. Changes require an explicit approval of a specific plan fingerprint plus Start. |
| Stale approval | Approval is invalidated automatically by any change to what would be sent. |
| Process killed / laptop closed mid-run | On restart, running jobs come back **paused**; the in-flight row is marked *in doubt*. |
| Duplicate creates on retry | Before each create the toolkit checks whether the profile exists. An *in doubt* row that turns out to exist is recorded as succeeded (*reconciled*); one created by someone else is **skipped**, so rollback never deletes other people's objects. |
| Ambiguous API responses | POSTs are never blindly retried after a timeout or 5xx; the toolkit checks the remote state instead. |
| Rate limits / outages | Client-side token-bucket throttle (default 80 calls / 10 s), automatic retry of 429/503 honouring `Retry-After`, exponential backoff with jitter for idempotent calls. |
| Runaway failures | After 5 consecutive failures the job pauses itself so you can investigate. |
| Audit trail | Every create, plan, edit (with before/after values), exclusion, approval (with OS user and fingerprint), start, pause and per-row result is logged and visible in the UI. |
| Secrets | Never written to disk by the toolkit and never returned by the API. Provide them via an environment variable (named per connection, default `AMT_CLIENT_SECRET`) or enter them in the UI (kept in memory until the toolkit stops). |
| Local web security | Listens on 127.0.0.1 only; Host-header allow-list (DNS-rebinding protection); per-launch CSRF token on every state-changing request; cross-origin requests rejected; strict Content-Security-Policy; no `innerHTML` with data. |
| CSV exports | Cells starting with `= + - @` are escaped to prevent spreadsheet formula injection. |

## SailPoint Identity Security Cloud setup

1. Create an API client (or personal access token) for a **service identity**, ideally in a sandbox
   tenant first. Grant only what the actions need, e.g. `idn:access-profile:manage`,
   `idn:sources:read`, `idn:entitlement:read`, `idn:identity:read`.
2. In **Connections → Add SailPoint Identity Security Cloud** enter the API base URL
   (`https://<tenant>.api.identitynow.com`), the client ID, and choose the environment.
3. Provide the secret: `export AMT_CLIENT_SECRET=...` before `python run.py`, or **Set secret** in the UI.
4. **Test** the connection.

Endpoints used: `POST /oauth/token`, `GET /v3/public-identities`, `GET /v3/sources`,
`GET /{beta|v2024|v2025}/entitlements` (configurable per connection), `GET/POST/DELETE /v3/access-profiles`.

## CSV format — Create access profiles

Download the template from the *New job* page. Headers are matched case-insensitively; `,` `;` and tab
delimiters and UTF-8 (with or without BOM) / Windows-1252 encodings are accepted.

| Column | Required | Notes |
|---|---|---|
| `name` | yes | Unique, max 128 characters. Existing names are skipped (or flagged as errors — job option). |
| `description` | | Warned when empty. |
| `owner` | yes | Email, username (alias) or identity ID. |
| `source` | yes | Source name. |
| `entitlements` | yes | Entitlement names or values on that source, separated by `;` (or `|`). |
| `requestable`, `enabled` | | yes/no, default yes. |
| `approval_schemes` | | `manager;owner;source_owner;app_owner;governance_group:<id>` in order. |
| `comments_required`, `denial_comments_required` | | yes/no, default no. |

## Project layout

```
run.py                     launcher (waitress server, graceful pause on Ctrl+C)
amtoolkit/
  app.py                   Flask JSON API + security middleware
  service.py               use cases and safety rules (edit, approve, rollback…)
  engine.py                background runners: plan / execute / pause / cancel / crash recovery
  store.py, db.py          SQLite persistence (WAL), job & item state machine, audit log
  csvio.py                 CSV parsing/export
  actions/                 bulk actions (CSV schema → plan → execute → rollback)
  connectors/              target systems: SailPoint ISC, offline sandbox; resilient HTTP client
  static/                  single-page UI (vanilla JS/CSS, no build step)
tests/                     pytest suite (workflow, safety rules, HTTP retry logic, API security)
examples/                  sample CSV for the sandbox
```

### Adding another bulk action

Subclass `amtoolkit.actions.base.Action`, define `columns`, `plan()` (read-only, returns the payload)
and `execute()` (must be safe to call again after a crash), optionally `inverse` + `rollback_row()`,
and register it in `amtoolkit/actions/__init__.py`. Add any new API calls to the `Connector`
interface and implement them for each connector (including the sandbox). The UI, review/approval
flow, progress, pause/resume and audit log work automatically.

## Development

```bash
pip install -r requirements-dev.txt
pytest
```
