# Access Manager Toolkit

A local, browser-based toolkit for the bulk operations access managers do — **creating access profiles
and governance groups from CSV files** (and rolling them back). It talks to the target system's REST API,
shows live progress, lets you **pause, resume or cancel at any time**, and makes you **review and
approve exactly what will change** before anything is sent.

It runs entirely on your machine: a small Python web server bound to `127.0.0.1` and a single SQLite
file. No external database, no cloud service, no CDN — the UI works offline.

Target system today: **SailPoint Identity Security Cloud (IdentityNow)**, plus an offline **Sandbox**
connector for practice, demos and training.

## Run it — one file, one command

Needs Python 3.11+ ([python.org](https://www.python.org/downloads/)); nothing else to install.

1. Download `AccessManagerToolkit-<version>.pyz` from the
   [latest release](https://github.com/ThatGaskin/AccessManagerDashboard/releases/latest)
   (optionally compare it with the `.sha256` file).
2. Run it:

   ```bash
   python AccessManagerToolkit-<version>.pyz
   ```

   On Windows use `py AccessManagerToolkit-<version>.pyz`, or just double-click the file (the Python
   installer associates `.pyz` files). The toolkit opens in your browser at `http://127.0.0.1:8765`.
   Keep the window open while you work; press **Ctrl+C** to stop (running jobs are paused safely).

The `.pyz` is a zip of the toolkit plus all of its dependencies, so it works the same on Windows, macOS
and Linux. Data (jobs, audit log, sandbox) is stored in your user profile — Windows
`%LOCALAPPDATA%\AccessManagerToolkit`, macOS `~/Library/Application Support/AccessManagerToolkit`,
Linux `~/.local/share/AccessManagerToolkit` — and survives upgrades: run the newer file instead.
Pass `--data-dir` to use another folder. Starting it twice just opens the browser on the running instance.

## Run from source

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
From a source checkout the data folder defaults to `./data`. `python -m amtoolkit` works too.

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
   `idn:sources:read`, `idn:entitlement:read`, `idn:identity:read`, plus `idn:workgroup:read` and
   `idn:workgroup:write` for governance groups.
2. In **Connections → Add SailPoint Identity Security Cloud** enter the API base URL
   (`https://<tenant>.api.identitynow.com`), the client ID, and choose the environment.
3. Provide the secret: `export AMT_CLIENT_SECRET=...` before `python run.py`, or **Set secret** in the UI.
4. **Test** the connection.

Endpoints used: `POST /oauth/token`, `GET /v3/public-identities`, `GET /v3/sources`,
`GET /{beta|v2024|v2025}/entitlements` and `/{beta|v2024|v2025}/workgroups` (governance groups,
including `members/bulk-add`) — both versions configurable per connection — and
`GET/POST/DELETE /v3/access-profiles`.

## CSV format — Create access profiles

Download the template from the *New job* page. Headers are matched case-insensitively; `,` `;` and tab
delimiters and UTF-8 (with or without BOM) / Windows-1252 encodings are accepted.

| Column | Required | Notes |
|---|---|---|
| `name` | yes | Unique, max 128 characters. Existing names are skipped (or flagged as errors — job option). |
| `description` | | Warned when empty. |
| `owner` | yes | Email, username (alias) or identity ID. Must be a person — see below. |
| `source` | yes | Source name. |
| `entitlements` | yes | Entitlement names or values on that source, separated by `;` (or `|`). |
| `requestable`, `enabled` | | yes/no, default yes. |
| `approval_schemes` | | In order, separated by `;`: `manager`, `owner`, `source_owner`, `app_owner`, `governance_group:<name or ID>`. |
| `comments_required`, `denial_comments_required` | | yes/no, default no. |

## Ownership and governance groups

* **Owners are people.** In Identity Security Cloud an access profile is owned by exactly one identity.
  If a governance group name is put in the `owner` column, the plan flags the row and explains that the
  group should be used as an approver instead.
* **Governance groups approve.** Add `governance_group:Finance Approvers` (name or ID) to
  `approval_schemes`. The plan resolves the group, shows its ID, and warns when it has no members —
  approvals assigned to an empty group can never be completed.
* **Missing groups.** Rows that reference a group that does not exist are flagged, and the job shows
  **Create missing governance groups…**. That prepares a separate *Create governance groups* job (you
  choose the owner and members) which goes through the same review → approve → run flow. When it has
  finished, its page offers **Re-plan that job now** for the access profile job.

### CSV format — Create governance groups

| Column | Required | Notes |
|---|---|---|
| `name` | yes | Unique, max 128 characters. Existing names are skipped (or flagged — job option). |
| `description` | | |
| `owner` | yes | One person: email, username (alias) or identity ID. |
| `members` | | Identities separated by `;`. Warned when empty. |

Members are added right after the group is created. If that step fails, the row fails with the group's
ID and **Retry failed** only adds the members to the existing group. Rollback deletes the groups the
job created. See `examples/governance_groups_sandbox.csv`.

## Experimental features

These are marked **experimental** throughout the UI (action picker, job pages, approval dialog, Reports
page). They were built from the SailPoint ISC API documentation and are tested end-to-end against the
offline sandbox only — **try them on a sandbox or test tenant first.** They use the same safety model as
everything else: read-only plan, approval bound to the plan, pause/resume, audit log, rollback.

| Action | What it does | Rollback |
|---|---|---|
| Update access profiles | Change description, owner, requestable/enabled, approvers; add/remove entitlements. Blank cells = unchanged, `(clear)` empties a description, `none` removes all approvers. | Restores previous values |
| Reassign ownership | Move ownership of access profiles, roles, entitlements, sources and governance groups. Optional `current_owner` column as a safety check. | Hands ownership back* |
| Update entitlements | Description, owner, privileged, requestable. | Restores previous values* |
| Change governance group members | Add/remove members; only real changes are planned. | Reverses the changes |
| Create roles / Delete roles | Roles bundling existing access profiles. | Deletes created roles |
| Submit access requests | Bulk grant or revoke requests (they still go through the tenant's approvals). Skips items with a pending request. | Submits the opposite request |
| Cancel access requests | Cancel pending requests by ID. | — |
| Forward pending approvals | Reassign pending approvals to another approver. | Forwards them back |

\* A previous owner that no longer exists (or "no owner") cannot be restored; such rows show an error in
the rollback job that you can edit or exclude.

**Updates are drift-safe:** the plan records the current value of every field it will change; at run
time the toolkit re-reads the object and refuses to apply a row whose fields were changed by someone else
after planning, so a stale plan never overwrites newer edits.

**Reports** (read-only) scan the tenant and list findings, and can export them as a pre-filled CSV for
the action that fixes them:

| Report | Follow-up action |
|---|---|
| Access profiles without a description | Update access profiles |
| Requestable access profiles without approvers | Update access profiles |
| Objects owned by an identity (e.g. a leaver) | Reassign ownership |
| Objects without a valid owner (none, or deleted identity) | Reassign ownership |
| Governance groups without members | Change governance group members |
| Entitlements missing a description or owner | Update entitlements |

Reports read up to 20,000 objects per type. Not covered yet: certification campaign management and
leaver verification.

API used (in addition to the stable features): `PATCH` with JSON Patch on `/v3/access-profiles`,
`/v3/roles`, `/v3/sources`, `/{beta|v2025}/entitlements`, `/{beta|v2025}/workgroups`; `GET/POST/DELETE
/v3/roles`; `workgroups/{id}/members` and `members/bulk-delete`; `/v3/access-requests`,
`/v3/access-requests/cancel`, `/v3/access-request-status`; `/v3/access-request-approvals/pending` and
`/{id}/forward`. Typical extra scopes: `idn:role-unchecked:manage`, `idn:entitlement:update`,
`idn:sources:manage`, `idn:access-request:manage`, `idn:access-request-administration:write`.
Things most likely to need adjusting on a real tenant: which fields each object's PATCH endpoint accepts,
the filters accepted by `access-request-status` and `access-request-approvals/pending`, and admin
visibility of other people's pending approvals.

## Project layout

```
run.py                     launcher for a source checkout (same as python -m amtoolkit)
amtoolkit/
  cli.py                   command-line launcher (waitress server, graceful pause on Ctrl+C)
  app.py                   Flask JSON API + security middleware
  service.py               use cases and safety rules (edit, approve, rollback…)
  engine.py                background runners: plan / execute / pause / cancel / crash recovery
  store.py, db.py          SQLite persistence (WAL), job & item state machine, audit log
  csvio.py                 CSV parsing/export
  actions/                 bulk actions (CSV schema → plan → execute → rollback):
                           access profiles and governance groups; experimental.py for the rest
  reports.py               read-only hygiene reports (experimental)
  connectors/              target systems: SailPoint ISC, offline sandbox; resilient HTTP client;
                           model.py normalizes objects and builds JSON Patch updates
  static/                  single-page UI (vanilla JS/CSS, no build step)
tests/                     pytest suite (workflow, safety rules, HTTP retry logic, API security)
packaging/build_pyz.py     builds the single-file AccessManagerToolkit.pyz
.github/workflows/         CI (tests on Linux + Windows) and Release (build, smoke-test, publish .pyz)
examples/                  sample CSVs for the sandbox
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

### Building the single file locally

```bash
python packaging/build_pyz.py      # → dist/AccessManagerToolkit.pyz
```

## Releasing

1. Bump `__version__` in `amtoolkit/__init__.py` and merge to `main`.
2. Tag and push: `git tag v0.2.0 && git push origin v0.2.0`.
3. The **Release** workflow runs the tests, builds the `.pyz`, starts it on Windows, macOS and Linux
   (Python 3.11 and 3.13) and checks that it serves the UI, then publishes a GitHub release with the
   `.pyz` and its SHA-256 checksum.
   It refuses to publish if the tag does not match `__version__`. Tags containing `-` (e.g. `v0.2.0-rc1`)
   become pre-releases.

To get a test build without publishing, run **Actions → Release → Run workflow**; the `.pyz` is attached
to the workflow run as an artifact.

