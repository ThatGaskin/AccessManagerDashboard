"use strict";
// Access Manager Toolkit UI. Plain JavaScript, no build step, no external resources.
// All DOM is built with textContent (never innerHTML with data) to avoid injection from CSV contents.

const CSRF = document.querySelector('meta[name="csrf-token"]').content;
const view = document.getElementById("view");
const modal = document.getElementById("modal");
const modalBody = document.getElementById("modal-body");
let META = null;
let pollTimer = null;

// ------------------------------------------------------------------ helpers
async function api(method, path, body, isForm) {
  const opts = { method, headers: { "X-CSRF-Token": CSRF, Accept: "application/json" } };
  if (body !== undefined) {
    if (isForm) opts.body = body;
    else { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
  }
  const resp = await fetch(path, opts);
  let data = null;
  try { data = await resp.json(); } catch (_) { /* non-JSON */ }
  if (!resp.ok) throw new Error((data && data.error) || `HTTP ${resp.status}`);
  return data;
}

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style") el.style.cssText = v; // CSSOM, allowed by the strict CSP (style attributes are not)
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

function toast(text, isError) {
  const t = h("div", { class: "toast" + (isError ? " err" : "") }, text);
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), isError ? 8000 : 3500);
}

async function guarded(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); return undefined; }
}

function fmtTime(iso) { return iso ? new Date(iso).toLocaleString() : ""; }

const STATUS_BADGE = {
  planning: "b-info", review: "b-warn", approved: "b-info", running: "b-info", pausing: "b-warn",
  paused: "b-warn", completed: "b-ok", cancelled: "",
  pending: "", succeeded: "b-ok", skipped: "", failed: "b-err", excluded: "",
  create: "b-info", delete: "b-err", skip: "", error: "b-err",
};
const badge = (text, cls) => h("span", { class: "badge " + (cls !== undefined ? cls : (STATUS_BADGE[text] || "")) }, text);
const envBadge = (env) => badge(env, env === "production" ? "b-prod" : env === "test" ? "b-warn" : "b-ok");

let modalSeq = 0; // identifies the dialog currently shown, so late async results don't reopen a closed one
function openModal(...content) {
  modalSeq += 1;
  modalBody.replaceChildren(...content.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  if (!modal.open) modal.showModal();
}
function closeModal() { if (modal.open) modal.close(); }

function confirmDialog(title, text, okLabel, danger) {
  return new Promise((resolve) => {
    const done = (v) => { closeModal(); resolve(v); };
    openModal(h("h2", {}, title), h("p", {}, text),
      h("div", { class: "modal-actions" },
        h("button", { onclick: () => done(false) }, "Back"),
        h("button", { class: danger ? "danger" : "primary", onclick: () => done(true) }, okLabel)));
  });
}

function download(url) { window.location.href = url; }

// ------------------------------------------------------------------ router
const routes = [
  [/^#?\/?$/, renderJobs],
  [/^#\/new$/, renderNewJob],
  [/^#\/connections$/, renderConnections],
  [/^#\/jobs\/([\w-]+)$/, renderJob],
];

async function route() {
  clearInterval(pollTimer);
  pollTimer = null;
  closeModal();
  const hash = location.hash || "#/";
  document.querySelectorAll("[data-nav]").forEach((a) => {
    const nav = a.dataset.nav;
    a.classList.toggle("active", (nav === "jobs" && (hash === "#/" || hash.startsWith("#/jobs"))) ||
      hash === "#/" + nav);
  });
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) { await guarded(() => fn(...m.slice(1))); return; }
  }
  view.replaceChildren(h("div", { class: "empty" }, "Page not found"));
}

// ------------------------------------------------------------------ jobs list
async function renderJobs() {
  const jobs = await api("GET", "/api/jobs");
  const table = h("table", {},
    h("thead", {}, h("tr", {}, ["Job", "Action", "Connection", "Status", "Progress", "Created"].map((t) => h("th", {}, t)))),
    h("tbody", {}, jobs.map((j) => {
      const c = j.counts;
      return h("tr", { class: "clickable", onclick: () => { location.hash = `#/jobs/${j.id}`; } },
        h("td", {}, h("strong", {}, j.name), j.parent_job_id ? h("div", { class: "small muted" }, "rollback job") : null),
        h("td", {}, j.action_title),
        h("td", {}, j.connection.name, " ", envBadge(j.connection.environment)),
        h("td", {}, badge(j.status), j.status === "paused" && !j.approved ? h("div", { class: "small muted" }, "needs approval") : null),
        h("td", { class: "small" }, `${c.done} / ${c.included}`, c.status.failed ? h("span", { class: "badge b-err" }, `${c.status.failed} failed`) : null),
        h("td", { class: "small muted" }, fmtTime(j.created_at)));
    })));
  view.replaceChildren(
    h("div", { class: "row spread" }, h("h1", {}, "Jobs"), h("a", { class: "btn primary", href: "#/new" }, "New bulk job")),
    h("div", { class: "panel" }, jobs.length ? table : h("div", { class: "empty" },
      "No jobs yet. ", h("a", { href: "#/new" }, "Start one from a CSV file"), ".")));
  pollTimer = setInterval(async () => {
    if (location.hash === "#/" || location.hash === "") {
      const fresh = await api("GET", "/api/jobs").catch(() => null);
      if (fresh && JSON.stringify(fresh.map((j) => [j.id, j.status, j.counts.done])) !==
          JSON.stringify(jobs.map((j) => [j.id, j.status, j.counts.done]))) route();
    }
  }, 3000);
}

// ------------------------------------------------------------------ new job
async function renderNewJob() {
  const conns = await api("GET", "/api/connections");
  if (!conns.length) {
    view.replaceChildren(h("h1", {}, "New bulk job"), h("div", { class: "panel empty" },
      "Add a connection first. ", h("a", { href: "#/connections" }, "Go to connections"),
      " — start with a Sandbox connection to practise safely."));
    return;
  }
  const actionSel = h("select", { name: "action" }, META.actions.map((a) => h("option", { value: a.key }, a.title)));
  const connSel = h("select", { name: "connection_id" }, conns.map((c) => h("option", { value: c.id }, `${c.name} (${c.environment})`)));
  const nameIn = h("input", { name: "name", placeholder: "e.g. Finance access profiles – Q4", maxlength: "200" });
  const fileIn = h("input", { type: "file", accept: ".csv,text/csv", hidden: true });
  const fileLabel = h("div", {}, "Drop a CSV file here or ", h("button", { type: "button", class: "link", onclick: () => fileIn.click() }, "browse"));
  const drop = h("div", { class: "dropzone" }, fileLabel);
  const optsBox = h("div");
  const docs = h("div");
  const prodWarn = h("div");
  let file = null;

  const setFile = (f) => { file = f; fileLabel.replaceChildren(h("strong", {}, f.name), ` (${Math.ceil(f.size / 1024)} KB) `, h("button", { type: "button", class: "link", onclick: () => fileIn.click() }, "change")); };
  fileIn.addEventListener("change", () => fileIn.files[0] && setFile(fileIn.files[0]));
  drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
  drop.addEventListener("dragleave", () => drop.classList.remove("over"));
  drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) setFile(e.dataTransfer.files[0]); });

  const refresh = () => {
    const action = META.actions.find((a) => a.key === actionSel.value);
    optsBox.replaceChildren(...action.options.map((o) => h("label", { class: "field" }, h("span", {}, o.label, h("div", { class: "help" }, o.help)),
      h("select", { name: "opt_" + o.name }, o.choices.map((c) => h("option", { value: c, selected: c === o.default }, c))))));
    docs.replaceChildren(
      h("h2", {}, action.title), h("p", { class: "muted" }, action.description),
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Column"), h("th", {}, "Description"), h("th", {}, "Example"))),
        h("tbody", {}, action.columns.map((c) => h("tr", {}, h("td", { class: "mono" }, c.name, c.required ? " *" : ""), h("td", { class: "small" }, c.help), h("td", { class: "mono" }, c.example))))),
      h("p", {}, h("a", { class: "btn", href: `/api/actions/${action.key}/template.csv` }, "Download CSV template")));
    const conn = conns.find((c) => c.id === connSel.value);
    prodWarn.replaceChildren(conn && conn.environment === "production" ? h("div", { class: "banner prod" }, "This connection is PRODUCTION. Nothing is changed until you review and approve the plan.") : "");
  };
  actionSel.addEventListener("change", refresh);
  connSel.addEventListener("change", refresh);

  const submit = h("button", { class: "primary", type: "submit" }, "Upload and plan");
  const form = h("form", {
    onsubmit: (e) => {
      e.preventDefault();
      if (!file) { toast("Choose a CSV file", true); return; }
      const fd = new FormData(form);
      fd.append("file", file);
      submit.disabled = true;
      guarded(async () => {
        const res = await api("POST", "/api/jobs", fd, true);
        location.hash = `#/jobs/${res.id}`;
      }).finally(() => { submit.disabled = false; });
    },
  },
  h("label", { class: "field" }, h("span", {}, "Action"), actionSel),
  h("label", { class: "field" }, h("span", {}, "Connection"), connSel),
  prodWarn,
  h("label", { class: "field" }, h("span", {}, "Job name ", h("span", { class: "help" }, "(optional)")), nameIn),
  optsBox,
  h("div", { class: "field" }, h("span", { class: "muted small" }, "CSV file (UTF-8, comma/semicolon/tab separated)"), drop, fileIn),
  h("p", { class: "small muted" }, "Uploading creates a plan using read-only lookups. Nothing is changed in the target system until you approve and start the job."),
  submit);
  view.replaceChildren(h("h1", {}, "New bulk job"), h("div", { class: "grid2" }, h("div", { class: "panel" }, form), h("div", { class: "panel" }, docs)));
  refresh();
}

// ------------------------------------------------------------------ connections
async function renderConnections() {
  const conns = await api("GET", "/api/connections");
  const secretLabel = { memory: ["secret in memory", "b-ok"], env: ["secret from env var", "b-ok"], missing: ["secret missing", "b-err"], not_required: ["no secret needed", ""] };
  view.replaceChildren(
    h("div", { class: "row spread" }, h("h1", {}, "Connections"),
      h("div", { class: "row" }, META.connector_kinds.map((k) => h("button", { onclick: () => editConnection(null, k.kind) }, "Add " + k.label)))),
    h("div", { class: "panel" }, conns.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Name", "Type", "Environment", "Credentials", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, conns.map((c) => h("tr", {},
        h("td", {}, h("strong", {}, c.name), h("div", { class: "small muted mono" }, c.config.base_url || "")),
        h("td", {}, c.kind_label), h("td", {}, envBadge(c.environment)),
        h("td", {}, badge(...secretLabel[c.secret_status])),
        h("td", { class: "row" },
          h("button", { onclick: (e) => testConnection(c.id, e.target) }, "Test"),
          c.secret_status !== "not_required" ? h("button", { onclick: () => setSecret(c) }, "Set secret") : null,
          h("button", { onclick: () => editConnection(c) }, "Edit"),
          h("button", { class: "danger", onclick: () => deleteConnection(c) }, "Delete")))))) :
      h("div", { class: "empty" }, "No connections yet. Add a Sandbox connection to try the toolkit without touching a real tenant.")),
    h("div", { class: "panel small muted" },
      h("h2", {}, "How credentials are handled"),
      h("ul", {},
        h("li", {}, "Client secrets are never written to disk by the toolkit and never sent back to the browser."),
        h("li", {}, "Either set the environment variable named in the connection before starting the toolkit, or enter the secret here; it is kept in memory until the toolkit stops."),
        h("li", {}, "Use a dedicated API client with only the scopes the actions need, and a test tenant first."))));
}

async function testConnection(id, btn) {
  btn.disabled = true;
  await guarded(async () => {
    const r = await api("POST", `/api/connections/${id}/test`);
    toast((r.ok ? "✓ " : "✗ ") + r.message, !r.ok);
  });
  btn.disabled = false;
}

function setSecret(conn) {
  const input = h("input", { type: "password", autocomplete: "off", placeholder: "client secret" });
  openModal(h("h2", {}, `Client secret for ${conn.name}`),
    h("p", { class: "muted small" }, "Kept in memory only. Leave empty and save to forget the stored secret."),
    input,
    h("div", { class: "modal-actions" }, h("button", { onclick: closeModal }, "Cancel"),
      h("button", { class: "primary", onclick: () => guarded(async () => {
        await api("POST", `/api/connections/${conn.id}/secret`, { secret: input.value });
        closeModal(); toast("Secret updated"); route();
      }) }, "Save")));
  input.focus();
}

function editConnection(conn, kind) {
  kind = conn ? conn.kind : kind;
  const meta = META.connector_kinds.find((k) => k.kind === kind);
  const name = h("input", { value: conn ? conn.name : (kind === "mock" ? "Sandbox" : ""), required: true });
  const env = h("select", {}, META.environments.map((e) => h("option", { value: e, selected: conn ? conn.environment === e : (kind === "mock" ? e === "sandbox" : e === "test") }, e)));
  const inputs = {};
  const fields = meta.fields.map((f) => {
    const value = conn ? (conn.config[f.name] ?? f.default) : f.default;
    inputs[f.name] = f.choices ? h("select", {}, f.choices.map((c) => h("option", { value: c, selected: c === value }, c)))
      : h("input", { value: value || "" });
    return h("label", { class: "field" }, h("span", {}, f.label, f.required ? " *" : "", f.help ? h("div", { class: "help" }, f.help) : null), inputs[f.name]);
  });
  const secret = meta.needs_secret ? h("input", { type: "password", autocomplete: "off", placeholder: conn ? "leave empty to keep current" : "optional — or use the environment variable" }) : null;
  openModal(h("h2", {}, (conn ? "Edit " : "Add ") + meta.label), h("p", { class: "muted small" }, meta.description),
    h("label", { class: "field" }, h("span", {}, "Name *"), name),
    h("label", { class: "field" }, h("span", {}, "Environment", h("div", { class: "help" }, "Production connections require typing the connection name to approve a plan.")), env),
    fields,
    secret ? h("label", { class: "field" }, h("span", {}, "Client secret", h("div", { class: "help" }, "Kept in memory only.")), secret) : null,
    h("div", { class: "modal-actions" }, h("button", { onclick: closeModal }, "Cancel"),
      h("button", { class: "primary", onclick: () => guarded(async () => {
        const config = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
        const payload = { name: name.value, kind, environment: env.value, config };
        if (secret && secret.value) payload.secret = secret.value;
        if (conn) await api("PUT", `/api/connections/${conn.id}`, payload);
        else await api("POST", "/api/connections", payload);
        closeModal(); toast("Connection saved"); route();
      }) }, "Save")));
}

async function deleteConnection(conn) {
  if (!(await confirmDialog("Delete connection", `Delete '${conn.name}'? Jobs that use it must be deleted first.`, "Delete", true))) return;
  await guarded(async () => { await api("DELETE", `/api/connections/${conn.id}`); route(); });
}

// ------------------------------------------------------------------ job detail
const FILTERS = [
  ["all", "All", (c) => c.total], ["errors", "Errors", (c) => c.errors], ["warnings", "Warnings", (c) => c.warnings],
  ["create", "To create", (c) => c.ops.create || 0], ["delete", "To delete", (c) => c.ops.delete || 0],
  ["skip", "Skip", (c) => c.ops.skip || 0], ["excluded", "Excluded", (c) => c.excluded],
  ["pending", "Pending", (c) => c.status.pending], ["succeeded", "Succeeded", (c) => c.status.succeeded],
  ["skipped", "Skipped", (c) => c.status.skipped], ["failed", "Failed", (c) => c.status.failed],
];

async function renderJob(jobId) {
  const state = { job: null, filter: "all", q: "", offset: 0, limit: 100, tab: "items", selected: new Set(), sig: "", rate: [] };
  const header = h("div");
  const body = h("div");
  view.replaceChildren(header, body);

  const loadJob = async () => {
    state.job = await api("GET", `/api/jobs/${jobId}`);
    const c = state.job.counts;
    state.rate.push([Date.now(), c.done]);
    state.rate = state.rate.filter(([t]) => Date.now() - t < 60000);
    renderHeader();
    const sig = JSON.stringify([state.job.status, state.job.updated_at, c.done, c.ops, c.excluded, c.errors]);
    if (sig !== state.sig) { state.sig = sig; await renderBody(); }
  };

  const doCommand = async (cmd, confirmText) => {
    if (confirmText && !(await confirmDialog("Please confirm", confirmText, "Yes, continue", cmd === "cancel" || cmd === "rollback"))) return;
    await guarded(async () => {
      const res = await api("POST", `/api/jobs/${jobId}/${cmd}`);
      if (cmd === "rollback") { toast("Rollback job created — review its plan before approving"); location.hash = `#/jobs/${res.id}`; return; }
      await loadJob();
    });
  };

  function renderHeader() {
    const j = state.job; const c = j.counts; const a = j.allowed;
    const steps = [["Plan", ["planning"]], ["Review", ["review"]], ["Approve", ["approved"]], ["Run", ["running", "pausing", "paused"]], ["Done", ["completed", "cancelled"]]];
    const currentIdx = steps.findIndex(([, ss]) => ss.includes(j.status));
    const total = Math.max(1, c.included);
    const pct = (n) => `${(100 * n / total).toFixed(2)}%`;
    let eta = "";
    if (j.status === "running" && state.rate.length > 1) {
      const [t0, d0] = state.rate[0]; const [t1, d1] = state.rate[state.rate.length - 1];
      const perSec = (d1 - d0) / Math.max(1, (t1 - t0) / 1000);
      if (perSec > 0) eta = `${perSec.toFixed(1)} rows/s · about ${Math.ceil(c.remaining / perSec / 60)} min left`;
    }
    const buttons = [
      a.replan && h("button", { onclick: () => doCommand("plan", "Refresh the plan for all rows that have not run yet? Lookups are read-only; any approval is cleared.") }, "Re-plan"),
      a.approve && h("button", { class: "primary", onclick: () => approveDialog(jobId, loadJob) }, "Review & approve…"),
      a.start && h("button", { class: "primary", onclick: () => doCommand("start", `Start making ${c.remaining} change(s) in '${j.connection.name}' (${j.connection.environment})? You can pause at any time.`) }, "Start"),
      a.resume && h("button", { class: "primary", onclick: () => doCommand("resume") }, "Resume"),
      a.pause && h("button", { onclick: () => doCommand("pause") }, "Pause"),
      a.retry_failed && h("button", { onclick: () => doCommand("retry-failed", `Queue ${c.status.failed} failed row(s) again?`) }, "Retry failed"),
      a.rollback && h("button", { onclick: () => doCommand("rollback", `Create a rollback job that deletes the ${c.status.succeeded} profile(s) this job created? It will open in review; nothing is deleted until you approve and start it.`) }, "Create rollback…"),
      a.cancel && h("button", { class: "danger", onclick: () => doCommand("cancel", "Cancel this job? Rows that already ran stay as they are; remaining rows will not run. This cannot be resumed.") }, "Cancel job"),
      h("button", { onclick: () => download(`/api/jobs/${jobId}/export.csv`) }, "Export CSV"),
      a.delete && h("button", { class: "danger", onclick: async () => {
        if (!(await confirmDialog("Delete job", "Delete this job and its audit log from this computer? Changes already made in the target system are NOT undone.", "Delete", true))) return;
        await guarded(async () => { await api("DELETE", `/api/jobs/${jobId}`); location.hash = "#/"; });
      } }, "Delete"),
    ];
    const banners = [];
    if (j.connection.environment === "production") banners.push(h("div", { class: "banner prod" }, `PRODUCTION — ${j.connection.name}`));
    if (j.error) banners.push(h("div", { class: "banner err" }, j.error));
    if (j.connection.secret_status === "missing") banners.push(h("div", { class: "banner warn" }, "The client secret for this connection is not loaded. ", h("a", { href: "#/connections" }, "Set it on the Connections page"), " before planning or running."));
    if (["review", "paused"].includes(j.status) && !j.approved && j.blockers.length) banners.push(h("div", { class: "banner warn" }, "Before you can approve: ", j.blockers.join("; "), "."));
    if (j.status === "paused" && !j.approved && !j.blockers.length) banners.push(h("div", { class: "banner info" }, "The plan changed while paused. Review and approve it again to resume."));
    if (j.status === "approved") banners.push(h("div", { class: "banner info" }, `Approved ${fmtTime(j.approved_at)}. Any edit will require approval again.`));
    if (j.status === "planning") banners.push(h("div", { class: "banner info" }, "Planning: looking up owners, sources, entitlements and existing profiles (read-only)…"));
    if (j.status === "pausing") banners.push(h("div", { class: "banner info" }, "Pausing after the current row finishes…"));

    header.replaceChildren(h("div", { class: "panel" },
      h("div", { class: "row spread" },
        h("div", {}, h("h1", {}, j.name), h("div", { class: "muted small" }, `${j.action_title} · `, j.connection.name, " ", envBadge(j.connection.environment), j.source_file ? ` · ${j.source_file}` : "", ` · created ${fmtTime(j.created_at)}`,
          j.parent_job_id ? h("span", {}, " · rollback of ", h("a", { href: `#/jobs/${j.parent_job_id}` }, "parent job")) : null)),
        badge(j.status)),
      h("div", { class: "stepper" }, steps.map(([label], i) => h("div", { class: "step " + (i < currentIdx || j.status === "completed" ? "done" : i === currentIdx ? "current" : "") }, label))),
      banners,
      h("div", { class: "progress", title: "succeeded / skipped / failed / running" },
        h("div", { class: "p-ok", style: `width:${pct(c.status.succeeded)}` }), h("div", { class: "p-skip", style: `width:${pct(c.status.skipped)}` }),
        h("div", { class: "p-err", style: `width:${pct(c.status.failed)}` }), h("div", { class: "p-run", style: `width:${pct(c.status.running)}` })),
      h("div", { class: "row spread" },
        h("div", { class: "stats" },
          [["Rows", c.total], ["Included", c.included], ["Succeeded", c.status.succeeded], ["Skipped", c.status.skipped], ["Failed", c.status.failed], ["Remaining", c.remaining]]
            .map(([l, n]) => h("div", { class: "stat" }, h("b", {}, n), h("span", { class: "muted small" }, l)))),
        h("div", { class: "muted small" }, eta)),
      h("div", { class: "row", style: "margin-top:12px" }, buttons)));
  }

  async function renderBody() {
    const tabs = h("div", { class: "tabs" },
      [["items", "Rows"], ["log", "Activity & audit log"]].map(([k, l]) => h("button", { class: state.tab === k ? "active" : "", onclick: () => { state.tab = k; renderBody(); } }, l)));
    const content = state.tab === "items" ? await itemsPanel() : await logPanel();
    body.replaceChildren(h("div", { class: "panel" }, tabs, content));
  }

  async function itemsPanel() {
    const j = state.job; const c = j.counts;
    const res = await api("GET", `/api/jobs/${jobId}/items?filter=${state.filter}&q=${encodeURIComponent(state.q)}&offset=${state.offset}&limit=${state.limit}`);
    const editable = j.allowed.edit;
    const chips = h("div", { class: "chips" }, FILTERS.filter(([k, , f]) => k === "all" || f(c) > 0).map(([k, l, f]) =>
      h("button", { class: "chip" + (state.filter === k ? " active" : ""), onclick: () => { state.filter = k; state.offset = 0; state.selected.clear(); renderBody(); } }, `${l} (${f(c)})`)));
    const search = h("input", { placeholder: "Search rows…", value: state.q, style: "max-width:260px" });
    search.addEventListener("keydown", (e) => { if (e.key === "Enter") { state.q = search.value; state.offset = 0; renderBody(); } });
    const selectedIds = () => [...state.selected];
    const bulkBar = editable ? h("div", { class: "row" },
      h("button", { disabled: !c.errors, onclick: () => bulk("exclude_errors", `Exclude all ${c.errors} row(s) with errors?`) }, "Exclude rows with errors"),
      h("button", { disabled: !c.excluded, onclick: () => bulk("include_all", `Include all ${c.excluded} excluded row(s) again?`) }, "Include all"),
      h("button", { disabled: !state.selected.size, onclick: () => setIncluded(selectedIds(), false) }, `Exclude selected (${state.selected.size})`),
      h("button", { disabled: !state.selected.size, onclick: () => setIncluded(selectedIds(), true) }, "Include selected")) : null;

    const canSelect = (i) => editable && ["pending", "failed"].includes(i.status);
    const allBox = h("input", { type: "checkbox", title: "Select page", disabled: !editable, onchange: (e) => { res.items.filter(canSelect).forEach((i) => e.target.checked ? state.selected.add(i.id) : state.selected.delete(i.id)); renderBody(); } });
    const rows = res.items.map((i) => {
      const status = i.included ? i.status : "excluded";
      const cb = h("input", { type: "checkbox", disabled: !canSelect(i), checked: state.selected.has(i.id), onclick: (e) => e.stopPropagation(), onchange: (e) => { e.target.checked ? state.selected.add(i.id) : state.selected.delete(i.id); renderBody(); } });
      const changed = JSON.stringify(i.input) !== JSON.stringify(i.original);
      return h("tr", { class: "clickable" + (i.included ? "" : " excluded"), onclick: () => itemDialog(jobId, i.id, state, loadJob) },
        h("td", {}, cb), h("td", { class: "mono" }, i.row_no),
        h("td", {}, h("strong", {}, i.target || i.input.name || i.input.id || "—"), changed ? h("span", { class: "badge b-warn", title: "Edited after upload" }, "edited") : null),
        h("td", {}, i.op ? badge(i.op) : h("span", { class: "muted small" }, "not planned")),
        h("td", {}, h("ul", { class: "msgs" }, i.messages.slice(0, 3).map((m) => h("li", { class: m.level }, m.text)), i.messages.length > 3 ? h("li", { class: "muted" }, `+${i.messages.length - 3} more`) : null)),
        h("td", {}, badge(status), i.error ? h("div", { class: "small", style: "color:var(--err)" }, i.error) : null),
        h("td", { class: "mono" }, i.remote_id || ""));
    });
    const pages = Math.ceil(res.total / state.limit);
    const page = Math.floor(state.offset / state.limit) + 1;
    return h("div", {},
      h("div", { class: "row spread" }, chips),
      h("div", { class: "row spread", style: "margin-bottom:8px" }, search, bulkBar),
      res.items.length ? h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, allBox), h("th", {}, "Line"), h("th", {}, "Target"), h("th", {}, "Planned"), h("th", {}, "Validation"), h("th", {}, "Status"), h("th", {}, "Remote ID"))),
        h("tbody", {}, rows)) : h("div", { class: "empty" }, j.status === "planning" ? "Planning…" : "No rows match this filter"),
      pages > 1 ? h("div", { class: "pager" },
        h("button", { disabled: page <= 1, onclick: () => { state.offset -= state.limit; renderBody(); } }, "‹ Prev"),
        h("span", { class: "small muted" }, `Page ${page} of ${pages} · ${res.total} rows`),
        h("button", { disabled: page >= pages, onclick: () => { state.offset += state.limit; renderBody(); } }, "Next ›")) : null);
  }

  async function bulk(op, text) {
    if (!(await confirmDialog("Change included rows", text + " This clears any approval.", "Yes"))) return;
    await guarded(async () => { const r = await api("POST", `/api/jobs/${jobId}/bulk`, { op }); toast(`${r.changed} row(s) updated`); state.sig = ""; await loadJob(); });
  }
  async function setIncluded(ids, included) {
    await guarded(async () => {
      const r = await api("POST", `/api/jobs/${jobId}/include`, { ids, included });
      toast(`${r.changed} row(s) ${included ? "included" : "excluded"}`); state.selected.clear(); state.sig = ""; await loadJob();
    });
  }

  async function logPanel() {
    const events = await api("GET", `/api/jobs/${jobId}/events?limit=1000`);
    const lvl = { info: "b-info", warning: "b-warn", error: "b-err" };
    return h("div", { class: "log" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Time"), h("th", {}, "Level"), h("th", {}, "Event"), h("th", {}, "Details"))),
      h("tbody", {}, events.map((e) => h("tr", {},
        h("td", { class: "muted", style: "white-space:nowrap" }, fmtTime(e.ts)), h("td", {}, badge(e.level, lvl[e.level])),
        h("td", { class: "mono" }, e.kind),
        h("td", {}, e.message, e.kind === "item_edited" && e.data ? h("ul", { class: "msgs" }, Object.entries(e.data).map(([k, v]) => h("li", {}, h("span", { class: "mono" }, k), ": ", h("span", { class: "diff-from" }, v.from || "∅"), " → ", v.to || "∅"))) : null))))));
  }

  await loadJob();
  let ticks = 0;
  pollTimer = setInterval(() => {
    if (modal.open || document.hidden) return;
    const active = ["planning", "running", "pausing"].includes(state.job.status);
    if (active || ++ticks % 4 === 0) loadJob().catch(() => {});
  }, 1200);
}

// ------------------------------------------------------------------ item editor
async function itemDialog(jobId, itemId, state, onChange) {
  const action = META.actions.find((a) => a.key === state.job.action);
  let item = await api("GET", `/api/jobs/${jobId}/items/${itemId}`);
  let mySeq = null;
  const stillOpen = () => modal.open && modalSeq === mySeq;
  const busy = (btn) => { btn.disabled = true; btn.textContent = "Working…"; };
  const render = () => {
    const editable = state.job.allowed.edit && ["pending", "failed"].includes(item.status);
    const byField = {};
    item.messages.forEach((m) => { (byField[m.field || "_"] = byField[m.field || "_"] || []).push(m); });
    const inputs = {};
    const fields = action.columns.map((col) => {
      const value = item.input[col.name] || "";
      const orig = item.original[col.name] || "";
      const multiline = col.name === "description" || col.name === "entitlements";
      inputs[col.name] = multiline ? h("textarea", { rows: 2, disabled: !editable }, value) : h("input", { value, disabled: !editable });
      const errs = byField[col.name] || [];
      return h("label", { class: "field" + (errs.some((m) => m.level === "error") ? " has-error" : "") + (value !== orig ? " changed" : "") },
        h("span", {}, col.name, col.required ? " *" : "", h("div", { class: "help" }, col.help)),
        inputs[col.name],
        value !== orig ? h("div", { class: "small muted" }, "CSV value: ", h("span", { class: "diff-from" }, orig || "∅")) : null,
        h("ul", { class: "msgs" }, errs.map((m) => h("li", { class: m.level }, m.text))));
    });
    const general = byField._ || [];
    const save = async (e) => {
      const values = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
      busy(e.target);
      await guarded(async () => {
        item = await api("PUT", `/api/jobs/${jobId}/items/${itemId}`, { input: values });
        toast(`Line ${item.row_no} saved and re-planned.`);
        state.sig = ""; onChange();
      });
      if (stillOpen()) render();
    };
    const act = async (e, fn) => {
      busy(e.target);
      await guarded(async () => { item = await fn(); state.sig = ""; onChange(); });
      if (stillOpen()) render();
    };
    const revert = () => action.columns.forEach((c) => { inputs[c.name].value = item.original[c.name] || ""; });
    openModal(
      h("div", { class: "row spread" }, h("h2", {}, `Line ${item.row_no}: ${item.target || item.input.name || ""}`),
        h("div", { class: "row" }, item.op ? badge(item.op) : null, badge(item.included ? item.status : "excluded"))),
      !editable ? h("div", { class: "banner info" }, item.status === "succeeded" || item.status === "skipped" ? "This row has already run and can no longer be changed." : "Rows can be edited while the job is in review, approved, paused or completed — not while planning or running.") : null,
      general.length ? h("ul", { class: "msgs" }, general.map((m) => h("li", { class: m.level }, m.text))) : null,
      h("div", { class: "grid2" },
        h("div", {}, h("h3", {}, "Row values"), fields),
        h("div", {},
          h("h3", {}, "What will be sent"),
          item.payload ? h("pre", { class: "json" }, JSON.stringify(item.payload, null, 2)) : h("p", { class: "muted" }, item.op === "error" ? "Nothing — fix the errors first." : "Nothing — this row is skipped."),
          item.result || item.error ? h("div", {}, h("h3", {}, "Result"), item.error ? h("div", { class: "banner err" }, item.error) : null, item.result ? h("pre", { class: "json" }, JSON.stringify(item.result, null, 2)) : null) : null,
          h("p", { class: "small muted" }, `Revision ${item.revision} · attempts ${item.attempts} · updated ${fmtTime(item.updated_at)}`))),
      h("div", { class: "modal-actions" },
        editable ? h("button", { onclick: (e) => act(e, async () => {
          await api("POST", `/api/jobs/${jobId}/include`, { ids: [itemId], included: !item.included });
          return api("GET", `/api/jobs/${jobId}/items/${itemId}`);
        }) }, item.included ? "Exclude row" : "Include row") : null,
        editable ? h("button", { onclick: (e) => act(e, () => api("POST", `/api/jobs/${jobId}/items/${itemId}/replan`)) }, "Re-check") : null,
        editable ? h("button", { onclick: revert }, "Revert to CSV values") : null,
        h("button", { onclick: closeModal }, "Close"),
        editable ? h("button", { class: "primary", onclick: save }, "Save & re-plan") : null));
    mySeq = modalSeq;
  };
  render();
}

// ------------------------------------------------------------------ approval
async function approveDialog(jobId, onDone) {
  const p = await guarded(() => api("GET", `/api/jobs/${jobId}/approval`));
  if (!p) return;
  const confirmInput = p.requires_confirmation_text ? h("input", { placeholder: p.requires_confirmation_text, autocomplete: "off" }) : null;
  const ops = Object.entries(p.ops).map(([op, n]) => h("div", { class: "stat" }, h("b", {}, n), badge(op)));
  openModal(h("h2", {}, "Review and approve the plan"),
    p.connection.environment === "production" ? h("div", { class: "banner prod" }, `These changes will be made in PRODUCTION (${p.connection.name}).`) : h("div", { class: "banner info" }, `Target: ${p.connection.name} (${p.connection.environment})`),
    h("div", { class: "stats" }, ops, h("div", { class: "stat" }, h("b", {}, p.excluded), h("span", { class: "muted small" }, "excluded")), h("div", { class: "stat" }, h("b", {}, p.warnings), h("span", { class: "muted small" }, "with warnings"))),
    h("h3", {}, `First ${p.sample.length} of ${p.total_changes} change(s)`),
    h("table", {}, h("tbody", {}, p.sample.map((s) => h("tr", {}, h("td", { class: "mono" }, s.row_no), h("td", {}, badge(s.op)), h("td", {}, s.target),
      h("td", { class: "small muted" }, s.payload && s.payload.entitlements ? `${s.payload.entitlements.length} entitlement(s) · owner ${s.payload.owner.name || s.payload.owner.id}` : ""))))),
    h("p", { class: "small muted" }, "Approval is bound to plan fingerprint ", h("span", { class: "mono" }, p.fingerprint.slice(0, 12)), ". If anything changes after this, you will have to approve again."),
    confirmInput ? h("label", { class: "field" }, h("span", {}, `Type ${p.requires_confirmation_text} to confirm`), confirmInput) : null,
    h("div", { class: "modal-actions" }, h("button", { onclick: closeModal }, "Back"),
      h("button", { class: "primary", onclick: () => guarded(async () => {
        await api("POST", `/api/jobs/${jobId}/approve`, { fingerprint: p.fingerprint, confirmation: confirmInput ? confirmInput.value : null });
        closeModal(); toast("Plan approved. Press Start when you are ready."); onDone();
      }) }, "Approve plan")));
}

// ------------------------------------------------------------------ boot
(async () => {
  META = await api("GET", "/api/meta");
  document.getElementById("operator").textContent = `v${META.version} · ${META.operator}`;
  modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
  window.addEventListener("hashchange", route);
  route();
})();
