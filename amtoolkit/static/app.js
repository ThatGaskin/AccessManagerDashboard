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
  if (!resp.ok) {
    const err = new Error((data && data.error) || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.code = data && data.code;
    throw err;
  }
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
  update: "b-info", request: "b-info", cancel: "b-warn", forward: "b-info",
};
const badge = (text, cls) => h("span", { class: "badge " + (cls !== undefined ? cls : (STATUS_BADGE[text] || "")) }, text);
const envBadge = (env) => badge(env, env === "production" ? "b-prod" : env === "test" ? "b-warn" : "b-ok");

let modalSeq = 0; // identifies the dialog currently shown, so late async results don't reopen a closed one
function showView(...content) {
  view.replaceChildren(...content.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
}

function openModal(...content) {
  modalSeq += 1;
  modalBody.replaceChildren(...content.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
  if (!modal.open) modal.showModal();
}
function closeModal() { if (modal.open) modal.close(); }

function download(url) { window.location.href = url; }

// ------------------------------------------------------------------ confirmations
// Every command first asks the server for a summary of exactly what will happen. The server signs it
// against the job's current state; the command is only accepted with that token, the ticked checkbox and,
// on production, the typed connection name. If the job changed in between, the server refuses (HTTP 428)
// and a fresh summary is shown.
function confirmationDialog(summary) {
  return new Promise((resolve) => {
    const done = (v) => { closeModal(); resolve(v); };
    const check = h("input", { type: "checkbox", id: "confirm-check" });
    const typed = summary.requires_text ? h("input", { placeholder: summary.requires_text, autocomplete: "off", spellcheck: "false" }) : null;
    const ok = h("button", { class: summary.danger ? "danger" : "primary", disabled: true,
      onclick: () => done({ token: summary.token, acknowledged: true, text: typed ? typed.value.trim() : null }) }, "Confirm");
    const update = () => { ok.disabled = !(check.checked && (!typed || typed.value.trim() === summary.requires_text)); };
    check.addEventListener("change", update);
    if (typed) typed.addEventListener("input", update);
    openModal(
      h("h2", {}, summary.title),
      summary.warnings.map((w) => h("div", { class: "banner " + (w.startsWith("PRODUCTION") ? "prod" : "warn") }, w)),
      h("ul", { class: "summary" }, summary.lines.map((l) => h("li", {}, l))),
      typed ? h("label", { class: "field" }, h("span", {}, `Type ${summary.requires_text} to confirm`), typed) : null,
      h("label", { class: "confirm-check", for: "confirm-check" }, check, h("span", {}, summary.checkbox)),
      h("p", { class: "small muted" }, `This summary is valid for ${Math.round(summary.expires_in / 60)} minutes and only for the job as it is now.`),
      h("div", { class: "modal-actions" }, h("button", { onclick: () => done(null) }, "Back"), ok));
    check.focus();
  });
}

// Asks for a summary, shows it, and runs `send(confirmation)` once the user confirms. Retries with a fresh
// summary if the server says the reviewed one is no longer current.
async function confirmed(confirmUrl, command, params, send) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const summary = await api("POST", confirmUrl, { command, params: params || {} });
    const confirmation = await confirmationDialog(summary);
    if (!confirmation) return undefined;
    try {
      return await send(confirmation);
    } catch (e) {
      if (e.code !== "confirmation_required") throw e;
      toast(e.message + " — showing the updated summary", true);
    }
  }
  return undefined;
}

// ------------------------------------------------------------------ router
const routes = [
  [/^#?\/?$/, renderJobs],
  [/^#\/new(?:\?action=([\w.]+))?$/, renderNewJob],
  [/^#\/connections$/, renderConnections],
  [/^#\/reports$/, renderReports],
  [/^#\/copy$/, renderCopy],
  [/^#\/compare$/, renderCompareForm],
  [/^#\/compare\/([\w-]+)$/, renderCompare],
  [/^#\/reports\/([\w-]+)$/, renderReport],
  [/^#\/jobs\/([\w-]+)$/, renderJob],
  [/^#\/logs$/, renderLogs],
];

async function route() {
  clearInterval(pollTimer);
  pollTimer = null;
  closeModal();
  const hash = location.hash || "#/";
  document.querySelectorAll("[data-nav]").forEach((a) => {
    const nav = a.dataset.nav;
    a.classList.toggle("active", (nav === "jobs" && (hash === "#/" || hash.startsWith("#/jobs"))) ||
      hash.split("?")[0] === "#/" + nav || (nav === "copy" && hash.startsWith("#/compare")));
  });
  for (const [re, fn] of routes) {
    const m = hash.match(re);
    if (m) { await guarded(() => fn(...m.slice(1))); return; }
  }
  view.replaceChildren(h("div", { class: "empty" }, "Page not found"));
}

// ------------------------------------------------------------------ backend logs
// Live tail of the server log: requests, calls to the target system and every job's audit events.
async function renderLogs() {
  let after = 0;
  const levelSelect = h("select", {}, ["DEBUG", "INFO", "WARNING", "ERROR"].map((l) => h("option", { value: l }, l)));
  levelSelect.value = "INFO";
  const follow = h("input", { type: "checkbox", checked: true });
  const tbody = h("tbody", {});
  const box = h("div", { class: "panel log" }, h("table", {}, tbody));
  async function load() {
    const rows = await api("GET", `/api/logs?after=${after}&level=${levelSelect.value}`);
    for (const r of rows) {
      after = Math.max(after, r.id);
      tbody.append(h("tr", { class: `lvl-${r.level.toLowerCase()}` }, h("td", { class: "nowrap muted" }, r.ts),
        h("td", {}, r.level), h("td", { class: "muted" }, r.logger), h("td", { class: "pre" }, r.message)));
    }
    while (tbody.children.length > 2000) tbody.firstChild.remove();
    if (rows.length && follow.checked) box.scrollTop = box.scrollHeight;
    return true;
  }
  levelSelect.addEventListener("change", () => { after = 0; tbody.replaceChildren(); guarded(load); });
  view.replaceChildren(h("div", { class: "row spread" }, h("h1", {}, "Backend log"),
    h("div", { class: "row" }, h("label", {}, "Level ", levelSelect), h("label", {}, follow, " Follow"),
      h("button", { onclick: () => { tbody.replaceChildren(); } }, "Clear view"))),
    h("p", { class: "small muted" }, "Requests to this server, calls to the target system and job events. The same log is written to logs/toolkit.log in the data folder. Start with --log-level DEBUG to see every lookup."),
    box);
  await load();
  pollTimer = setInterval(() => load().catch(() => {}), 2000);
}

// ------------------------------------------------------------------ jobs list
async function renderJobs() {
  const jobs = await api("GET", "/api/jobs");
  const table = h("table", {},
    h("thead", {}, h("tr", {}, ["Job", "Action", "Connection", "Status", "Progress", "Created"].map((t) => h("th", {}, t)))),
    h("tbody", {}, jobs.map((j) => {
      const c = j.counts;
      return h("tr", { class: "clickable", onclick: () => { location.hash = `#/jobs/${j.id}`; } },
        h("td", {}, h("strong", {}, j.name), j.parent_job_id ? h("div", { class: "small muted" }, "follow-up job") : null),
        h("td", {}, j.action_title),
        h("td", {}, j.connection.name, " ", envBadge(j.connection.environment)),
        h("td", {}, badge(j.status), j.experimental ? experimentalBadge() : null, j.status === "paused" && !j.approved ? h("div", { class: "small muted" }, "needs approval") : null),
        h("td", { class: "small" }, `${c.done} / ${c.included}`, c.status.failed ? h("span", { class: "badge b-err" }, `${c.status.failed} failed`) : null),
        h("td", { class: "small muted" }, fmtTime(j.created_at)));
    })));
  view.replaceChildren(
    h("div", { class: "row spread" }, h("h1", {}, "Jobs"), h("a", { class: "btn primary", href: "#/new" }, "New bulk job")),
    h("div", { class: "panel" }, jobs.length ? table : h("div", { class: "empty" },
      h("img", { class: "empty-mark", src: "/static/logo.svg", alt: "" }),
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
// Guidance shown next to the CSV format, so the rules about owners and governance groups are visible
// before anyone prepares a file.
const ACTION_NOTES = {
  "access_profile.create": [
    "Owners: an access profile is owned by one person. Put an identity (email, username or ID) in the owner column — governance groups cannot own access profiles.",
    "Approvers: to let a team approve requests, add governance_group:<group name> to approval_schemes, e.g. manager;governance_group:Finance Approvers. The plan checks that each group exists and warns when it has no members.",
    "Creating groups from the CSV: fill in new_group_owner (and new_group_members, new_group_description) and any group named in approval_schemes that does not exist yet is created right before the row's access profile. The plan lists every group that will be created; rows naming the same new group must use the same new_group_* values.",
    "Missing groups: without new_group_owner, a row referencing a group that does not exist is flagged and the job offers “Create missing governance groups”, which prepares a separate job for you to review and approve. Re-plan this job once the groups exist.",
  ],
  "access_profile.update": [
    "Approvers: approval_schemes replaces all approvers. To use a governance group that does not exist yet, fill in new_group_owner (and new_group_members); the group is created right before the profile is updated.",
  ],
  "governance_group.create": [
    "A governance group is a team of identities used as approvers and reviewers. It has one owner (a person) and any number of members.",
    "A group without members cannot complete approvals; the plan warns about this.",
    "Reference new groups from access profiles with approval_schemes = governance_group:<group name>.",
  ],
};

async function renderNewJob(presetAction) {
  const conns = await api("GET", "/api/connections");
  if (!conns.length) {
    view.replaceChildren(h("h1", {}, "New bulk job"), h("div", { class: "panel empty" },
      "Add a connection first. ", h("a", { href: "#/connections" }, "Go to connections"),
      " — start with a Sandbox connection to practise safely."));
    return;
  }
  const opt = (a) => h("option", { value: a.key, selected: a.key === presetAction }, a.title);
  const listed = META.actions.filter((a) => !a.hidden);
  const tested = listed.filter((a) => !a.experimental), untested = listed.filter((a) => a.experimental);
  const actionSel = h("select", { name: "action" },
    tested.length ? h("optgroup", { label: "Tested" }, tested.map(opt)) : null,
    untested.length ? h("optgroup", { label: "Experimental — not yet tested on a real tenant" }, untested.map(opt)) : null);
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
      o.kind === "connection"
        ? h("select", { name: "opt_" + o.name }, conns.filter((c) => c.id !== connSel.value).map((c) => h("option", { value: c.id }, `${c.name} (${c.environment})`)))
        : h("select", { name: "opt_" + o.name }, o.choices.map((c) => h("option", { value: c, selected: c === o.default }, c))))));
    docs.replaceChildren(
      h("h2", {}, action.title, action.experimental ? experimentalBadge() : null), h("p", { class: "muted" }, action.description),
      action.experimental ? experimentalBanner() : null,
      h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Column"), h("th", {}, "Description"), h("th", {}, "Example"))),
        h("tbody", {}, action.columns.map((c) => h("tr", {}, h("td", { class: "mono" }, c.name, c.required ? " *" : ""), h("td", { class: "small" }, c.help, c.ref && c.ref !== "id" ? h("div", { class: "muted" }, `Looked up: ${REF_LABELS[c.ref] || c.ref}${c.multi ? " (several, ';')" : ""} — any text; misses suggest similar names`) : null), h("td", { class: "mono" }, c.example))))),
      h("p", {}, h("a", { class: "btn", href: `/api/actions/${action.key}/template.csv` }, "Download CSV template")),
      ACTION_NOTES[action.key] ? h("div", { class: "banner info" }, h("strong", {}, "Owners and governance groups"),
        h("ul", { class: "notes" }, ACTION_NOTES[action.key].map((t) => h("li", {}, t)))) : null);
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
      h("div", { class: "row" }, META.connector_kinds.map((k) => h("button", { onclick: () => editConnection(null, k.kind) }, "Add " + k.label)),
        demoButton(conns.some((c) => c.name === "Demo Test") ? "Reset demo environments" : "Create demo environments"))),
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
  await guarded(async () => {
    const summary = await api("POST", `/api/connections/${conn.id}/confirm`, {});
    const confirmation = await confirmationDialog(summary);
    if (!confirmation) return;
    await api("DELETE", `/api/connections/${conn.id}`, { confirmation });
    toast("Connection deleted");
    route();
  });
}

// ------------------------------------------------------------------ job detail
const FILTERS = [
  ["all", "All", (c) => c.total], ["errors", "Errors", (c) => c.errors], ["warnings", "Warnings", (c) => c.warnings],
  ["create", "To create", (c) => c.ops.create || 0], ["delete", "To delete", (c) => c.ops.delete || 0],
  ["update", "To update", (c) => c.ops.update || 0], ["request", "To request", (c) => c.ops.request || 0],
  ["cancel", "To cancel", (c) => c.ops.cancel || 0], ["forward", "To forward", (c) => c.ops.forward || 0],
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

  const confirmUrl = `/api/jobs/${jobId}/confirm`;
  // Commands that are not plain POST /api/jobs/<id>/<command>.
  const ENDPOINTS = { delete: ["DELETE", `/api/jobs/${jobId}`] };
  const doCommand = (cmd) => guarded(async () => {
    if (cmd === "pause") { await api("POST", `/api/jobs/${jobId}/pause`, {}); await loadJob(); return; } // always one click
    const [method, url] = ENDPOINTS[cmd] || ["POST", `/api/jobs/${jobId}/${cmd}`];
    const res = await confirmed(confirmUrl, cmd, {}, (confirmation) => api(method, url, { confirmation }));
    if (res === undefined) return;
    if (cmd === "rollback") { toast("Rollback job created — review its plan before approving"); location.hash = `#/jobs/${res.id}`; return; }
    if (cmd === "delete") { toast("Job deleted"); location.hash = "#/"; return; }
    if (cmd === "approve") toast("Plan approved. Press Start when you are ready.");
    state.sig = "";
    await loadJob();
  });

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
      a.replan && h("button", { onclick: () => doCommand("plan") }, "Re-plan…"),
      a.approve && h("button", { class: "primary", onclick: () => doCommand("approve") }, "Review & approve…"),
      a.start && h("button", { class: "primary", onclick: () => doCommand("start") }, "Start…"),
      a.resume && h("button", { class: "primary", onclick: () => doCommand("resume") }, "Resume…"),
      a.pause && h("button", { title: "Stops after the current row. No confirmation needed.", onclick: () => doCommand("pause") }, "Pause"),
      a.retry_failed && h("button", { onclick: () => doCommand("retry-failed") }, "Retry failed…"),
      a.rollback && h("button", { onclick: () => doCommand("rollback") }, "Create rollback…"),
      a.cancel && h("button", { class: "danger", onclick: () => doCommand("cancel") }, "Cancel job…"),
      h("button", { onclick: () => download(`/api/jobs/${jobId}/export.csv`) }, "Export CSV"),
      a.delete && h("button", { class: "danger", onclick: () => doCommand("delete") }, "Delete…"),
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
    if (j.missing_governance_groups.length) banners.push(h("div", { class: "banner warn" },
      `${j.missing_governance_groups.length} governance group(s) used as approvers do not exist yet: `,
      h("strong", {}, j.missing_governance_groups.join(", ")), ". Fill in new_group_owner (and new_group_members) on those rows to create them with this job, or create them in a separate job (reviewed and approved like any other), then Re-plan this job. ",
      a.create_groups ? h("button", { onclick: () => missingGroupsDialog(j) }, "Create missing governance groups…") : null));
    if (j.new_governance_groups.length) banners.push(h("div", { class: "banner info" },
      `${j.new_governance_groups.length} governance group(s) will be created from the CSV when the rows that use them run: `,
      h("strong", {}, j.new_governance_groups.join(", ")), ". A rollback of this job does not delete them."));
    if (j.parent && j.action === "governance_group.create" && j.parent.action === "access_profile.create" && j.status === "completed") {
      const canReplan = ["review", "approved", "paused"].includes(j.parent.status);
      banners.push(h("div", { class: "banner info" }, "Groups are ready. Re-plan ", h("a", { href: `#/jobs/${j.parent.id}` }, j.parent.name),
        " so its rows pick them up. ",
        canReplan ? h("button", { onclick: () => guarded(async () => {
          const res = await confirmed(`/api/jobs/${j.parent.id}/confirm`, "plan", {}, (confirmation) => api("POST", `/api/jobs/${j.parent.id}/plan`, { confirmation }));
          if (res) location.hash = `#/jobs/${j.parent.id}`;
        }) }, "Re-plan that job now…") : null));
    }

    header.replaceChildren(h("div", { class: "panel" },
      h("div", { class: "row spread" },
        h("div", {}, h("h1", {}, j.name), h("div", { class: "muted small" }, `${j.action_title} · `,
          j.source_connection ? h("span", {}, "from ", j.source_connection.name, " ", j.source_connection.environment ? envBadge(j.source_connection.environment) : null, " to ") : null,
          j.connection.name, " ", envBadge(j.connection.environment), j.source_file ? ` · ${j.source_file}` : "", ` · created ${fmtTime(j.created_at)}`,
          j.parent ? h("span", {}, " · created from ", h("a", { href: `#/jobs/${j.parent.id}` }, j.parent.name)) : null)),
        h("div", { class: "row" }, j.experimental ? experimentalBadge() : null, badge(j.status))),
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
      h("button", { disabled: !c.errors, onclick: () => bulk("exclude_errors") }, "Exclude rows with errors"),
      h("button", { disabled: !c.excluded, onclick: () => bulk("include_all") }, "Include all"),
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

  async function bulk(op) {
    await guarded(async () => {
      const r = await confirmed(confirmUrl, "bulk", { op }, (confirmation) => api("POST", `/api/jobs/${jobId}/bulk`, { op, confirmation }));
      if (r) { toast(`${r.changed} row(s) updated`); state.sig = ""; await loadJob(); }
    });
  }
  async function setIncluded(ids, included) {
    await guarded(async () => {
      const r = await confirmed(confirmUrl, "include", { ids, included }, (confirmation) => api("POST", `/api/jobs/${jobId}/include`, { ids, included, confirmation }));
      if (r) { toast(`${r.changed} row(s) ${included ? "included" : "excluded"}`); state.selected.clear(); state.sig = ""; await loadJob(); }
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
      const connId = state.job.connection.id;
      let control = inputs[col.name];
      if (editable && col.ref === "approver") control = approverBuilder(control, connId);
      else if (editable && col.ref && col.ref !== "id") {
        control = refSuggest(control, col.ref, col.multi, connId,
          ["entitlement", "object:entitlement"].includes(col.ref) ? () => (inputs.source ? inputs.source.value.trim() : "") : null);
      }
      return h("div", { class: "field item-field" + (errs.some((m) => m.level === "error") ? " has-error" : "") + (value !== orig ? " changed" : "") },
        h("span", {}, col.name, col.required ? " *" : "", h("div", { class: "help" }, col.help)),
        control,
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
          await api("POST", `/api/jobs/${jobId}/include`, { ids: [itemId], included: !item.included, single_row: true });
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

// ------------------------------------------------------------------ governance groups
function missingGroupsDialog(job) {
  const owner = h("input", { placeholder: "e.g. alice@example.com", autocomplete: "off" });
  const members = h("input", { placeholder: "optional, e.g. alice@example.com;carol", autocomplete: "off" });
  const description = h("input", { value: `Approvers for access profiles in '${job.name}'`, maxlength: "500" });
  openModal(h("h2", {}, "Create missing governance groups"),
    h("p", {}, "This prepares a new ", h("strong", {}, "Create governance groups"), " job for:"),
    h("ul", {}, job.missing_governance_groups.map((n) => h("li", {}, n))),
    h("div", { class: "banner info" }, "Nothing is created yet. The new job opens in review: you can set a different owner or members per group, then approve and start it. Afterwards, re-plan this access profile job."),
    h("label", { class: "field" }, h("span", {}, "Owner for the new groups *", h("div", { class: "help" }, "One person (email, username or identity ID). Groups cannot be owned by another group.")), owner),
    h("label", { class: "field" }, h("span", {}, "Members", h("div", { class: "help" }, "Identities separated by ';'. Groups without members cannot complete approvals — you can also add members per group in review.")), members),
    h("label", { class: "field" }, h("span", {}, "Description"), description),
    h("div", { class: "modal-actions" }, h("button", { onclick: closeModal }, "Cancel"),
      h("button", { class: "primary", onclick: () => guarded(async () => {
        const params = { owner: owner.value, members: members.value, description: description.value };
        const res = await confirmed(`/api/jobs/${job.id}/confirm`, "missing-governance-groups", params,
          (confirmation) => api("POST", `/api/jobs/${job.id}/missing-governance-groups`, { ...params, confirmation }));
        if (res) { toast("Governance group job created — review it, then approve and start"); location.hash = `#/jobs/${res.id}`; }
      }) }, "Prepare job…")));
  owner.focus();
}

function changeSummary(p) {
  if (!p) return "";
  if (p.entitlements) {
    const groups = (p.accessRequestConfig.approvalSchemes || []).filter((a) => a.approverType === "GOVERNANCE_GROUP").length;
    return `${p.entitlements.length} entitlement(s) · owner ${p.owner.name || p.owner.id}` + (groups ? ` · ${groups} approver group(s)` : "");
  }
  if (p.group && p.members) return `owner ${p.group.owner.name || p.group.owner.id} · ${p.members.length} member(s)`;
  if (p.group && p.add) return `+${p.add.length} / −${p.remove.length} member(s)`;
  if (p.after) return Object.keys(p.after).map((f) => (p.labels && p.labels[f] ? `${f}: ${p.labels[f][0]} → ${p.labels[f][1]}` : `${f}: ${fmtValue(p.before[f])} → ${fmtValue(p.after[f])}`)).join(" · ");
  if (p.requestType) return `${p.requestType === "GRANT_ACCESS" ? "grant" : "revoke"} for ${p.display.identity}`;
  if (p.new_owner) return `→ ${p.new_owner}`;
  return p.id ? p.id : "";
}

function fmtValue(v) {
  if (Array.isArray(v)) return v.length ? `${v.length} item(s)` : "none";
  if (v === true) return "yes";
  if (v === false) return "no";
  return v === null || v === undefined || v === "" ? "empty" : String(v).length > 40 ? String(v).slice(0, 37) + "…" : String(v);
}

// ------------------------------------------------------------------ experimental
function experimentalBadge() {
  return h("span", { class: "badge b-warn", title: "Built from the API documentation; tested only against the offline sandbox" }, "experimental");
}
function experimentalBanner() {
  return h("div", { class: "banner warn" }, h("strong", {}, "Experimental. "),
    "Built from the SailPoint API documentation and tested only against the offline sandbox. Try it on a sandbox or test tenant first and check the results before using it in production.");
}

// ------------------------------------------------------------------ reports
function plainInput(placeholder) {
  const el = h("input", { placeholder: placeholder || "" });
  return { el, get value() { return el.value.trim(); } };
}

const REF_LABELS = { identity: "identity", source: "source", access_profile: "access profile", entitlement: "entitlement on the row's source",
  governance_group: "governance group", approver: "approver type or governance group", "object:access_profile": "access profile",
  "object:role": "role", "object:entitlement": "entitlement", "object:governance_group": "governance group", "object:source": "source" };

// Typeahead for reference cells. Any text is accepted; once typing pauses, objects whose name *contains*
// the text are offered. In multi-value cells only the part after the last ';' is searched and replaced.
function refSuggest(input, kind, multi, connectionId, scope) {
  const list = h("ul", { class: "picker-list", role: "listbox" });
  list.hidden = true;
  let timer = null;
  let seq = 0;
  const current = () => (multi ? input.value.split(";").pop() : input.value).trim();
  const put = (value) => {
    if (multi) {
      const parts = input.value.split(";");
      parts[parts.length - 1] = (parts.length > 1 ? " " : "") + value;
      input.value = parts.join(";");
    } else input.value = value;
    list.hidden = true;
    input.focus();
  };
  input.setAttribute("autocomplete", "off");
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = current();
    if (q.length < 2) { list.hidden = true; return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      const sc = scope ? scope() : "";
      let rows = [];
      try {
        rows = await api("GET", `/api/connections/${connectionId}/search?kind=${encodeURIComponent(kind)}&q=${encodeURIComponent(q)}` +
          (sc ? `&scope=${encodeURIComponent(sc)}` : ""));
      } catch (_) { /* suggestions are optional; the typed text is still used */ }
      if (mine !== seq) return;
      list.replaceChildren(...rows.map((r) => h("li", { role: "option", tabindex: "0",
        onmousedown: (e) => { e.preventDefault(); put(r.value); },
        onkeydown: (e) => {
          if (e.key === "Enter") { e.preventDefault(); put(r.value); }
          if (e.key === "ArrowDown" && e.target.nextSibling) e.target.nextSibling.focus();
          if (e.key === "ArrowUp") (e.target.previousSibling || input).focus();
          if (e.key === "Escape") { list.hidden = true; input.focus(); }
        } }, r.label)));
      list.hidden = !rows.length;
    }, 350);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" && !list.hidden && list.firstChild) { e.preventDefault(); list.firstChild.focus(); }
    if (e.key === "Escape") list.hidden = true;
  });
  input.addEventListener("blur", () => setTimeout(() => { if (!list.contains(document.activeElement)) list.hidden = true; }, 150));
  return h("div", { class: "picker" }, input, list);
}

// Approvers are either a role (manager, owner, …) or a governance group: choose the type first, then —
// for a group — type any part of its name. The cell stays editable as plain text.
const APPROVER_TYPES = [["manager", "Manager of the requester"], ["owner", "Owner of the access profile"],
  ["source_owner", "Source owner"], ["app_owner", "App owner"], ["governance_group", "Governance group…"]];
function approverBuilder(input, connectionId) {
  const type = h("select", { "aria-label": "Approver type" }, h("option", { value: "" }, "Add approver — choose type…"),
    APPROVER_TYPES.map(([v, l]) => h("option", { value: v }, l)));
  const groupInput = h("input", { placeholder: "Group name (type any part)" });
  const groupBox = refSuggest(groupInput, "governance_group", false, connectionId);
  const add = h("button", { type: "button" }, "Add");
  groupBox.hidden = true;
  add.hidden = true;
  const append = (token) => {
    const parts = input.value.split(";").map((p) => p.trim()).filter((p) => p && p.toLowerCase() !== "none");
    if (!parts.some((p) => p.toLowerCase() === token.toLowerCase())) parts.push(token);
    input.value = parts.join(";");
    type.value = ""; groupInput.value = ""; groupBox.hidden = true; add.hidden = true;
  };
  type.addEventListener("change", () => {
    const group = type.value === "governance_group";
    groupBox.hidden = !group;
    add.hidden = !type.value;
    if (group) groupInput.focus();
  });
  add.addEventListener("click", (e) => {
    e.preventDefault();
    if (type.value === "governance_group") { if (groupInput.value.trim()) append("governance_group:" + groupInput.value.trim()); }
    else if (type.value) append(type.value);
  });
  return h("div", {}, input, h("div", { class: "row approver-add" }, type, groupBox, add));
}

function approversInput(connectionId, placeholder) {
  const el = h("input", { placeholder: placeholder || "" });
  return { el: approverBuilder(el, connectionId), get value() { return el.value.trim(); } };
}

// Search-and-select for identities: type 2+ characters of a name, username or email, pick a person.
// The selected identity's ID is used, so the result never depends on ambiguous free text.
function identityPicker(connectionId, placeholder) {
  let selected = null;
  let timer = null;
  let seq = 0;
  const input = h("input", { placeholder: placeholder || "Search by name, username or email", autocomplete: "off",
    role: "combobox", "aria-autocomplete": "list", "aria-expanded": "false" });
  const list = h("ul", { class: "picker-list", role: "listbox" });
  const chip = h("div", { class: "picker-chip" });
  list.hidden = true;
  chip.hidden = true;
  const describe = (i) => [i.email, i.alias].filter(Boolean).join(" · ");
  const showList = (items) => { list.replaceChildren(...items); list.hidden = false; input.setAttribute("aria-expanded", "true"); };
  const hideList = () => { list.hidden = true; input.setAttribute("aria-expanded", "false"); };
  const choose = (i) => {
    selected = i;
    hideList();
    input.hidden = true;
    chip.hidden = false;
    chip.replaceChildren(h("div", {}, h("strong", {}, i.name || i.id), " ", h("span", { class: "small muted" }, describe(i)),
      h("div", { class: "mono small muted" }, i.id)),
      h("button", { type: "button", class: "link", onclick: () => {
        selected = null; chip.hidden = true; input.hidden = false; input.value = ""; input.focus();
      } }, "Change"));
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { hideList(); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      showList([h("li", { class: "muted" }, "Searching…")]);
      let rows;
      try {
        rows = await api("GET", `/api/connections/${connectionId()}/identities?q=${encodeURIComponent(q)}`);
      } catch (e) {
        if (mine === seq) showList([h("li", { class: "muted" }, `Search failed: ${e.message}`)]);
        return;
      }
      if (mine !== seq) return; // a newer search is running
      showList(rows.length ? rows.map((i) => h("li", { role: "option", tabindex: "0", onclick: () => choose(i),
        onkeydown: (e) => {
          if (e.key === "Enter") { e.preventDefault(); choose(i); }
          if (e.key === "ArrowDown" && e.target.nextSibling) e.target.nextSibling.focus();
          if (e.key === "ArrowUp") (e.target.previousSibling || input).focus();
          if (e.key === "Escape") { hideList(); input.focus(); }
        } }, h("strong", {}, i.name || i.id), " ", h("span", { class: "small muted" }, describe(i))))
        : [h("li", { class: "muted" }, "No matching identities")]);
    }, 250);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" && !list.hidden && list.firstChild && list.firstChild.tabIndex === 0) { e.preventDefault(); list.firstChild.focus(); }
    if (e.key === "Escape") hideList();
  });
  return { el: h("div", { class: "picker" }, input, list, chip), get value() { return selected ? selected.id : ""; } };
}

async function renderReports() {
  const [conns, recent] = await Promise.all([api("GET", "/api/connections"), api("GET", "/api/reports")]);
  if (!conns.length) {
    view.replaceChildren(h("h1", {}, "Reports"), h("div", { class: "panel empty" }, "Add a connection first. ", h("a", { href: "#/connections" }, "Go to connections")));
    return;
  }
  const connSel = h("select", {}, conns.map((c) => h("option", { value: c.id }, `${c.name} (${c.environment})`)));
  const repSel = h("select", {}, META.reports.map((r) => h("option", { value: r.key }, r.title)));
  const details = h("div");
  const inputs = {};
  const refresh = () => {
    const r = META.reports.find((x) => x.key === repSel.value);
    const followup = r.followup_action && META.actions.find((a) => a.key === r.followup_action);
    Object.keys(inputs).forEach((k) => delete inputs[k]);
    const fields = r.params.map((p) => {
      const field = p.kind === "identity" ? identityPicker(() => connSel.value, p.help)
        : p.kind === "approvers" ? approversInput(connSel.value, p.help) : plainInput(p.help);
      inputs[p.name] = field;
      return h("div", { class: "field" }, h("span", { class: "field-label" }, p.label, p.required ? " *" : ""), field.el);
    });
    // h() flattens nested arrays; replaceChildren() would not, so build the content with h().
    details.replaceChildren(h("div", {}, h("p", { class: "muted" }, r.description),
      followup ? h("p", { class: "small muted" }, "Findings can be exported as a CSV for ", h("strong", {}, followup.title), ".") : null,
      fields));
  };
  repSel.addEventListener("change", refresh);
  connSel.addEventListener("change", refresh); // a picked identity belongs to the previous connection
  const runBtn = h("button", { class: "primary", onclick: () => guarded(async () => {
    runBtn.disabled = true;
    const params = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
    const res = await api("POST", "/api/reports", { connection_id: connSel.value, report: repSel.value, params }).finally(() => { runBtn.disabled = false; });
    location.hash = `#/reports/${res.id}`;
  }) }, "Run report");
  view.replaceChildren(
    h("div", { class: "row" }, h("h1", {}, "Reports"), experimentalBadge()),
    h("div", { class: "banner info" }, "Reports are read-only: they only list findings. To fix them, use “Create job from findings” on the report — it creates a job for the matching bulk action that is planned, reviewed and approved like any other job. You can also download the findings as a CSV and edit them in a spreadsheet first."),
    experimentalBanner(),
    h("div", { class: "grid2" },
      h("div", { class: "panel" }, h("label", { class: "field" }, h("span", {}, "Connection"), connSel), h("label", { class: "field" }, h("span", {}, "Report"), repSel), details, runBtn),
      h("div", { class: "panel" }, h("h2", {}, "Recent reports"), recent.length ? h("table", {}, h("tbody", {}, recent.map((r) => h("tr", { class: "clickable", onclick: () => { location.hash = `#/reports/${r.id}`; } },
        h("td", {}, r.title, h("div", { class: "small muted" }, Object.values(r.params).filter(Boolean).join(", "))),
        h("td", {}, badge(r.status, r.status === "completed" ? "b-ok" : r.status === "failed" ? "b-err" : "b-info")),
        h("td", { class: "small" }, r.status === "completed" ? `${r.count} finding(s)` : ""),
        h("td", { class: "small muted" }, fmtTime(r.created_at)))))) : h("p", { class: "muted" }, "None yet."))));
  refresh();
}

function jobFromReportDialog(report, def, followup) {
  const fields = def.fill.map((p) => ({ p, field: p.kind === "identity" ? identityPicker(() => report.connection_id, "Search by name, username or email")
    : p.kind === "approvers" ? approversInput(report.connection_id, p.help) : plainInput(p.help) }));
  const check = h("input", { type: "checkbox", id: "report-job-check" });
  const create = h("button", { class: "primary", disabled: true, onclick: () => guarded(async () => {
    create.disabled = true;
    const defaults = Object.fromEntries(fields.map(({ p, field }) => [p.name, field.value]));
    const res = await api("POST", `/api/reports/${report.id}/create-job`, { defaults }).finally(() => { create.disabled = !check.checked; });
    closeModal();
    toast("Job created — it is being planned (read-only). Review it, then approve and start.");
    location.hash = `#/jobs/${res.id}`;
  }) }, "Create job");
  check.addEventListener("change", () => { create.disabled = !check.checked; });
  openModal(h("h2", {}, `Create a “${followup.title}” job`),
    followup.experimental ? experimentalBanner() : null,
    h("ul", { class: "summary" },
      h("li", {}, `${report.count} row(s), one per finding of “${report.title}”`),
      h("li", {}, "Same connection the report ran on"),
      h("li", {}, "Nothing changes now: the job is planned with read-only lookups and opens in review, where you can edit or exclude rows before approving and starting it.")),
    fields.length ? h("div", {}, h("h3", {}, "Fill in for all rows (optional)"),
      fields.map(({ p, field }) => h("div", { class: "field" }, h("span", { class: "field-label" }, p.label, h("div", { class: "help small muted" }, p.help)), field.el)),
      h("p", { class: "small muted" }, "Anything left empty can be filled in per row during review; rows still missing a value show as errors you can fix or exclude.")) : null,
    h("label", { class: "confirm-check", for: "report-job-check" }, check, h("span", {}, "I have reviewed this summary and want to create the job.")),
    h("div", { class: "modal-actions" }, h("button", { onclick: closeModal }, "Back"), create));
}

async function renderReport(reportId) {
  const load = async () => {
    const r = await api("GET", `/api/reports/${reportId}`);
    const def = META.reports.find((x) => x.key === r.report);
    const followup = def && def.followup_action && META.actions.find((a) => a.key === def.followup_action);
    const running = r.status === "running";
    showView(
      h("div", { class: "row spread" }, h("div", {}, h("h1", {}, r.title), h("div", { class: "small muted" }, `${fmtTime(r.created_at)} · scanned ${r.scanned} object(s)`, Object.values(r.params).filter(Boolean).length ? ` · ${Object.entries(r.params).filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`).join(", ")}` : "")),
        h("div", { class: "row" }, experimentalBadge(), badge(r.status, r.status === "completed" ? "b-ok" : r.status === "failed" ? "b-err" : "b-info"))),
      r.error ? h("div", { class: "banner err" }, r.error) : null,
      running ? h("div", { class: "banner info" }, "Scanning the tenant (read-only)…") : null,
      h("div", { class: "row", style: "margin-bottom:12px" },
        running ? h("button", { onclick: () => guarded(async () => { await api("POST", `/api/reports/${reportId}/cancel`); load(); }) }, "Cancel") : null,
        r.status === "completed" ? h("button", { onclick: () => download(`/api/reports/${reportId}/export.csv`) }, "Export findings CSV") : null,
        r.status === "completed" && followup && r.count ? h("button", { onclick: () => download(`/api/reports/${reportId}/followup.csv`) }, `Download as “${followup.title}” CSV`) : null,
        r.status === "completed" && followup && r.count ? h("button", { class: "primary", onclick: () => jobFromReportDialog(r, def, followup) }, "Create job from findings…") : null,
        h("button", { class: "danger", onclick: () => guarded(async () => {
          const ok = await confirmationDialog({ title: "Delete the report", lines: [`The results of '${r.title}' (${r.count} finding(s)) are removed from this computer`, "Nothing changes in the target system"],
            warnings: [], danger: true, checkbox: "I have reviewed this summary and want to delete this report.", requires_text: null, token: null, expires_in: 600 });
          if (!ok) return;
          await api("DELETE", `/api/reports/${reportId}`); location.hash = "#/reports";
        }) }, "Delete…")),
      r.status === "completed" && followup && r.count ? h("p", { class: "small muted" }, `“Create job from findings” turns these ${r.count} finding(s) into a “${followup.title}” job on the same connection. Prefer a spreadsheet? Download the CSV, edit it, and upload it on `, h("a", { href: `#/new?action=${followup.key}` }, "New job"), ".") : null,
      r.status === "completed" ? h("div", { class: "panel" }, r.rows.length ? h("table", {},
        h("thead", {}, h("tr", {}, def.columns.map((c) => h("th", {}, c)))),
        h("tbody", {}, r.rows.map((row) => h("tr", {}, def.columns.map((c) => h("td", { class: c === "id" ? "mono" : "" }, row[c] ?? "")))))) : h("div", { class: "empty" }, "No findings 🎉"),
      r.count > r.rows.length ? h("p", { class: "small muted" }, `Showing ${r.rows.length} of ${r.count}; the CSV export has all of them.`) : null) : null);
    return running;
  };
  if (await load()) pollTimer = setInterval(async () => { if (!(await load().catch(() => false))) { clearInterval(pollTimer); pollTimer = null; } }, 1500);
}

// ------------------------------------------------------------------ copy one object
// Pick an object in one environment, tick the values to move, and move them to the other. The move is
// planned read-only first and confirmed against exactly that plan; it is recorded as a job so the audit
// log and rollback work as for every other change.

// Dropdown of object names that can be searched: opens with the first names, filters as you type.
function objectDropdown(placeholder, load, onChoose) {
  const input = h("input", { placeholder, autocomplete: "off", spellcheck: "false" });
  const list = h("ul", { class: "picker-list", role: "listbox" });
  list.hidden = true;
  let timer = null, seq = 0;
  const choose = (name) => { input.value = name; list.hidden = true; onChoose(name); };
  const fill = async () => {
    const mine = ++seq;
    let names = [];
    try { names = await load(input.value.trim()); } catch (e) { toast(e.message, true); }
    if (mine !== seq) return;
    list.replaceChildren(...(names.length ? names.map((n) => h("li", { role: "option", tabindex: "0",
      onmousedown: (e) => { e.preventDefault(); choose(n); },
      onkeydown: (e) => {
        if (e.key === "Enter") { e.preventDefault(); choose(n); }
        if (e.key === "ArrowDown" && e.target.nextSibling) e.target.nextSibling.focus();
        if (e.key === "ArrowUp") (e.target.previousSibling || input).focus();
        if (e.key === "Escape") { list.hidden = true; input.focus(); }
      } }, n)) : [h("li", { class: "muted small" }, "Nothing found")]));
    list.hidden = false;
  };
  input.addEventListener("focus", fill);
  input.addEventListener("click", () => { if (list.hidden) fill(); }); // already focused: focus does not fire again
  input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(fill, 250); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" && !list.hidden && list.firstChild && list.firstChild.tabIndex === 0) { e.preventDefault(); list.firstChild.focus(); }
    if (e.key === "Escape") list.hidden = true;
  });
  input.addEventListener("blur", () => setTimeout(() => { if (!list.contains(document.activeElement)) list.hidden = true; }, 150));
  return { el: h("div", { class: "picker" }, input, h("span", { class: "picker-caret", "aria-hidden": "true" }, "▾"), list),
    clear: () => { input.value = ""; list.hidden = true; } };
}

// Two offline sandboxes, "Demo Test" and "Demo Prod", whose data differs on purpose. Running it again resets
// them; nothing outside this computer is touched.
function demoButton(label) {
  return h("button", { title: "Two offline sandboxes with differing data: changed values, objects on one side only, different members and source settings, and a few things that cannot be copied",
    onclick: () => guarded(async () => {
      if (label.startsWith("Reset") && !window.confirm("Reset Demo Test and Demo Prod to their starting data? Changes made in them are lost (nothing outside this computer is affected).")) return;
      const [test, prod] = await api("POST", "/api/demo-environments");
      toast(`${label.startsWith("Reset") ? "Reset" : "Created"} ${test.name} and ${prod.name}`);
      if (location.hash === "#/copy") route(); else location.hash = "#/copy";
    }) }, label);
}

async function renderCopy() {
  const conns = await api("GET", "/api/connections");
  if (conns.length < 2) {
    showView(h("h1", {}, "Copy between environments"), h("div", { class: "panel empty" },
      h("p", {}, "Add two connections — e.g. your test and production tenants — to copy between them. ", h("a", { href: "#/connections" }, "Go to connections")),
      h("p", {}, "Or try it first with two sandboxes whose data differs on purpose:"), demoButton("Create demo environments")));
    return;
  }
  const rank = { test: 0, sandbox: 1, production: 2 };
  const byEnv = [...conns].sort((a, b) => rank[a.environment] - rank[b.environment]);
  const connOpts = (selected) => conns.map((c) => h("option", { value: c.id, selected: c.id === selected }, `${c.name} (${c.environment})`));
  const fromSel = h("select", {}, connOpts(byEnv[0].id));
  const toSel = h("select", {}, connOpts(byEnv[byEnv.length - 1].id));
  const typeSel = h("select", {}, COPY_TYPES.map(([k, l]) => h("option", { value: k }, l.replace(/s$/, ""))));
  const result = h("div");
  const state = { row: null, picked: new Set(), overrides: {} };
  const connName = (id) => conns.find((c) => c.id === id).name;

  const picker = objectDropdown("Search or pick…", (q) => api("GET", `/api/connections/${fromSel.value}/objects?type=${typeSel.value}&q=${encodeURIComponent(q)}`), (name) => preview(name));
  const reset = () => { picker.clear(); state.row = null; result.replaceChildren(); };
  [fromSel, toSel, typeSel].forEach((s) => s.addEventListener("change", reset));

  const request = () => ({ source_connection_id: fromSel.value, connection_id: toSel.value, object_type: typeSel.value,
    object: state.row.source_id, attributes: [...state.picked],
    overrides: Object.fromEntries(Object.entries(state.overrides).filter(([k, v]) => v && state.picked.has(k))) });

  async function preview(name, notice) {
    if (fromSel.value === toSel.value) { toast("Choose two different connections", true); return; }
    result.replaceChildren(h("div", { class: "panel muted" }, `Reading ${name} in both environments…`));
    const row = await guarded(() => api("POST", "/api/copy/preview", { source_connection_id: fromSel.value, connection_id: toSel.value, object_type: typeSel.value, object: name }));
    if (!row) { result.replaceChildren(); return; }
    state.row = row;
    state.overrides = {};
    state.picked = new Set(row.diffs.filter((d) => !d.same && (row.status === "new" ? (!d.env_specific || d.required) : (!d.env_specific && !d.only_in_target))).map((d) => d.attr));
    draw(notice);
  }

  function draw(notice) {
    const row = state.row, from = connName(fromSel.value), to = connName(toSel.value);
    const isNew = row.status === "new";
    const differing = row.diffs.filter((d) => !d.same);
    const canOverride = (d) => typeSel.value === "source" && d.env_specific && (d.kind === "config" || d.kind === "cluster");
    const toggle = (attr, on) => { on ? state.picked.add(attr) : state.picked.delete(attr); draw(); };
    const rows = row.diffs.map((d) => {
      const locked = isNew && d.required;
      const box = h("input", { type: "checkbox", checked: state.picked.has(d.attr) || locked, disabled: d.same || locked,
        title: d.same ? "Already the same" : locked ? "Needed to create it" : "", onchange: (e) => toggle(d.attr, e.target.checked) });
      let override = null;
      if (canOverride(d)) {
        override = h("input", { class: "override", value: state.overrides[d.attr] || "", placeholder: d.kind === "cluster" ? `cluster name in ${to}` : `value for ${to}` });
        override.addEventListener("change", () => { state.overrides[d.attr] = override.value.trim(); if (override.value.trim()) state.picked.add(d.attr); draw(); });
      }
      const ticked = state.picked.has(d.attr) || locked;
      return h("tr", { class: diffRowClass(d, ticked) },
        h("td", {}, box),
        h("td", {}, h("span", { class: "mono" }, d.label),
          d.env_specific ? h("span", { class: "badge b-warn", title: "Usually differs per environment; left alone unless you tick it" }, "environment-specific") : null,
          d.only_in_target ? h("span", { class: "badge", title: `Not set in ${from}; moving it removes it from ${to}` }, `only in ${to}`) : null,
          locked ? h("span", { class: "badge" }, "required") : null),
        compareCells(d, ticked, isNew, state.overrides[d.attr]),
        h("td", {}, override));
    });
    const count = state.picked.size;
    const move = h("button", { class: "primary", disabled: !count, onclick: () => doMove(move) },
      isNew ? `Create in ${to} with ${count} value(s)` : `Move ${count} value(s) to ${to}`);
    result.replaceChildren(...[notice].filter(Boolean),
      h("div", { class: "panel" },
        h("div", { class: "row spread" },
          h("div", {}, h("h2", {}, row.name, " ", badge(row.status === "same" ? "same" : isNew ? `new in ${to}` : `${differing.length} difference(s)`,
            row.status === "same" ? "b-ok" : isNew ? "b-info" : "b-warn")),
            h("div", { class: "small muted" }, isNew ? `Not in ${to} yet — it will be created there with the ticked values.` :
              row.status === "same" ? `Every value already matches ${to}.` : `Tick the values to move from ${from} to ${to}.`)),
          differing.length ? h("div", { class: "row" },
            h("button", { onclick: () => { differing.forEach((d) => state.picked.add(d.attr)); draw(); } }, "Tick all differences"),
            h("button", { onclick: () => { state.picked.clear(); draw(); } }, "Untick all")) : null),
        diffLegend(from, to),
        h("table", { class: "diff copy" },
          h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "Attribute"), h("th", {}, `In ${from}`), h("th", {}, ""), h("th", {}, `In ${to}`), h("th", {}, typeSel.value === "source" ? `Use instead in ${to}` : ""))),
          h("tbody", {}, rows)),
        row.secrets.length ? h("p", { class: "small muted" }, `Secrets are never copied: ${row.secrets.join(", ")}. Set them in ${to} directly.`) : null),
      h("div", { class: "panel sticky-actions" }, h("div", { class: "row" }, move,
        h("span", { class: "small muted" }, "You see exactly what will change and confirm it before anything is sent."))));
  }

  async function doMove(button) {
    button.disabled = true;
    try {
      for (let attempt = 0; attempt < 3; attempt++) {
        const req = request();
        const planned = await api("POST", "/api/copy/plan", req);
        if (planned.op !== "create" && planned.op !== "update") {
          const errors = planned.messages.filter((m) => m.level === "error");
          result.prepend(h("div", { class: "banner " + (errors.length ? "err" : "info") }, errors.length ? h("strong", {}, "This cannot be moved yet:") : "Nothing to change — the ticked values already match.",
            errors.length ? h("ul", { class: "notes" }, errors.map((m) => h("li", {}, m.text))) : null));
          return;
        }
        const confirmation = await confirmationDialog(planned.confirmation);
        if (!confirmation) return;
        let job;
        try {
          job = await api("POST", "/api/copy/move", { ...req, confirmation });
        } catch (e) {
          if (e.code !== "confirmation_required") throw e;
          toast(e.message + " — showing the updated summary", true);
          continue;
        }
        await followMove(job.id, state.row.name);
        return;
      }
    } catch (e) { toast(e.message, true); } finally { button.disabled = false; }
  }

  async function followMove(jobId, name) {
    const progress = h("div", { class: "banner info" }, "Moving…");
    result.prepend(progress);
    let j;
    for (let i = 0; i < 120; i++) {
      j = await api("GET", `/api/jobs/${jobId}`);
      if (["completed", "paused", "cancelled"].includes(j.status)) break;
      await new Promise((r) => setTimeout(r, 500));
    }
    const ok = j.counts.status.succeeded > 0;
    const [item] = (await api("GET", `/api/jobs/${jobId}/items?limit=1`)).items;
    const notice = h("div", { class: "banner " + (ok ? "info" : "err") },
      h("strong", {}, ok ? `Moved to ${connName(toSel.value)}. ` : "The move did not complete. "),
      item && item.error ? item.error + " " : "",
      h("a", { href: `#/jobs/${jobId}` }, "Open the job"), ok ? " — undo it there with Create rollback." : " to see the details and retry.");
    await preview(name, notice);
  }

  showView(
    h("div", { class: "row spread" }, h("div", { class: "row" }, h("h1", {}, "Copy between environments"), experimentalBadge()),
      h("div", { class: "row" }, demoButton(conns.some((c) => c.name === "Demo Test") ? "Reset demo environments" : "Create demo environments"),
        h("a", { href: "#/compare" }, "Compare all objects of a type →"))),
    h("div", { class: "panel" }, h("div", { class: "copy-pick" },
      h("label", { class: "field" }, h("span", {}, "From"), fromSel),
      h("label", { class: "field" }, h("span", {}, "To ", h("span", { class: "help" }, "— changed")), toSel),
      h("label", { class: "field" }, h("span", {}, "Type"), typeSel),
      h("div", { class: "field" }, h("span", { class: "field-label" }, "Object"), picker.el)),
      h("p", { class: "small muted", style: "margin:0" }, "References are matched by name in the other environment: owners and members by email or username; groups, access profiles and sources by name; entitlements by name or value. Environment-specific values (group members, a source's cluster and connection settings) are left alone unless you tick them. Secrets are never copied.")),
    result);
}

// ------------------------------------------------------------------ compare environments
// Read-only side-by-side view of one object type in two connections. The ticked objects and attributes
// become a normal "Copy objects between environments" job, planned and reviewed like any other.
const COPY_TYPES = [["access_profile", "Access profiles"], ["role", "Roles"], ["governance_group", "Governance groups"], ["source", "Sources"]];

async function renderCompareForm() {
  const conns = await api("GET", "/api/connections");
  if (conns.length < 2) {
    showView(h("h1", {}, "Compare environments"), h("div", { class: "panel empty" },
      "Add two connections — e.g. your test and production tenants — to compare them. ", h("a", { href: "#/connections" }, "Go to connections")));
    return;
  }
  const rank = { test: 0, sandbox: 1, production: 2 };
  const byEnv = [...conns].sort((a, b) => rank[a.environment] - rank[b.environment]);
  const connOpts = (selected) => conns.map((c) => h("option", { value: c.id, selected: c.id === selected }, `${c.name} (${c.environment})`));
  const fromSel = h("select", {}, connOpts(byEnv[0].id));
  const toSel = h("select", {}, connOpts(byEnv[byEnv.length - 1].id));
  const typeSel = h("select", {}, COPY_TYPES.map(([k, l]) => h("option", { value: k }, l)));
  const go = h("button", { class: "primary", type: "submit" }, "Compare");
  showView(
    h("div", { class: "row spread" }, h("div", { class: "row" }, h("h1", {}, "Compare environments"), experimentalBadge()),
      h("a", { href: "#/copy" }, "← Copy one object")),
    h("p", { class: "muted" }, "Lists one object type in two connections and shows, attribute by attribute, what differs. Read-only: nothing changes until you create a copy job from the result and approve it."),
    h("div", { class: "grid2" },
      h("div", { class: "panel" }, h("form", { onsubmit: (e) => { e.preventDefault(); go.disabled = true; guarded(async () => {
        const res = await api("POST", "/api/compare", { source_connection_id: fromSel.value, connection_id: toSel.value, object_type: typeSel.value });
        location.hash = `#/compare/${res.id}`;
      }).finally(() => { go.disabled = false; }); } },
        h("label", { class: "field" }, h("span", {}, "Copy from ", h("span", { class: "help" }, "— e.g. test")), fromSel),
        h("label", { class: "field" }, h("span", {}, "Copy to ", h("span", { class: "help" }, "— e.g. production; this is the connection that will be changed")), toSel),
        h("label", { class: "field" }, h("span", {}, "Object type"), typeSel),
        go)),
      h("div", { class: "panel small" }, h("h2", {}, "How copying works"), h("ul", { class: "notes" },
        h("li", {}, "Objects are matched by name. References are translated by name too: owners and members by email or username, groups, access profiles and sources by name, entitlements by name or value on the matching source."),
        h("li", {}, "Pick the attributes to copy for objects that exist in both. Objects missing in the target are created with every copyable attribute."),
        h("li", {}, "Environment-specific attributes — group members, a source's cluster and connection settings such as hosts, URLs and service accounts — are unticked by default and stay as they are in the target."),
        h("li", {}, "Secrets (passwords, client secrets) are never copied; the API does not return them."),
        h("li", {}, "The copy job's plan shows the exact before → after of every attribute. Rollback restores previous values and deletes created objects.")))));
}

function showValue(v) {
  if (v === null || v === undefined || v === "") return h("span", { class: "muted" }, "(none)");
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.length ? v.join(", ") : h("span", { class: "muted" }, "(none)");
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

// Colour-coded comparison cells. Lists (entitlements, members, access profiles, approvers) are shown item
// by item: + only in the source, − only in the target. The middle mark says = same, ≠ different (left
// alone) or → will be moved; the target column then shows what it has now, struck through, and what it
// will have.
function valueChips(list, other, cls) {
  const others = new Set(other || []);
  if (!list || !list.length) return h("span", { class: "muted" }, "(none)");
  return list.map((v) => others.has(v) ? h("span", { class: "vchip" }, v)
    : h("span", { class: `vchip ${cls}`, title: cls === "add" ? "Only here" : cls === "remove" ? "Removed by the move" : "Only in the target" },
      cls === "add" ? "+ " : cls === "remove" || cls === "extra" ? "− " : "", v));
}

function compareCells(d, ticked, isNew, override) {
  const isList = Array.isArray(d.source) || Array.isArray(d.target);
  const src = override !== undefined && override !== "" ? override : d.source;
  const dst = isNew ? null : d.target;
  const sourceCell = h("td", { class: "val" }, isList ? valueChips(d.source || [], dst || [], "add") : showValue(d.source));
  const mark = d.same ? ["same", "=", "Same in both"] : ticked ? ["move", "→", "Will be moved"] : isNew ? ["off", "–", "Not copied"] : ["diff", "≠", "Different — left alone"];
  const markCell = h("td", { class: "markcell" }, h("span", { class: `mark ${mark[0]}`, title: mark[2] }, mark[1]));
  let target;
  if (isNew) {
    target = ticked ? h("ins", { class: "v" }, isList ? (src || []).join(", ") || "(none)" : showValue(src)) : h("span", { class: "muted" }, "—");
  } else if (d.same) {
    target = isList ? valueChips(dst || [], dst || [], "") : showValue(dst);
  } else if (ticked) {
    if (isList) {
      const keep = new Set(src || []);
      target = [...(dst || []).map((v) => keep.has(v) ? h("span", { class: "vchip" }, v) : h("span", { class: "vchip remove", title: "Removed by the move" }, "− ", v)),
        ...(src || []).filter((v) => !(dst || []).includes(v)).map((v) => h("span", { class: "vchip add", title: "Added by the move" }, "+ ", v))];
      if (!target.length) target = h("span", { class: "muted" }, "(none)");
    } else {
      target = [h("del", { class: "v" }, showValue(dst)), " ", h("ins", { class: "v" }, showValue(src))];
    }
  } else {
    target = isList ? valueChips(dst || [], d.source || [], "extra") : showValue(dst);
  }
  return [sourceCell, markCell, h("td", { class: "val after" }, target)];
}

const diffRowClass = (d, ticked) => d.same ? "row-same" : ticked ? "row-move" : "row-diff";

function diffLegend(from, to) {
  return h("div", { class: "legend small muted" },
    h("span", {}, h("span", { class: "mark same" }, "="), " same"),
    h("span", {}, h("span", { class: "mark diff" }, "≠"), " different, left alone"),
    h("span", {}, h("span", { class: "mark move" }, "→"), ` will be moved to ${to}`),
    h("span", {}, h("span", { class: "vchip add" }, "+ "), ` only in ${from}`),
    h("span", {}, h("span", { class: "vchip extra" }, "− "), ` only in ${to}`),
    h("span", {}, h("del", { class: "v" }, "old"), " ", h("ins", { class: "v" }, "new"), ` value in ${to} after the move`));
}

async function renderCompare(compareId) {
  const picked = new Map(); // source_id -> Set of attributes (empty set for new objects)
  let filter = "changes";
  const draw = (r) => {
    const running = r.status === "running";
    const counts = { new: 0, different: 0, same: 0, ambiguous: 0 };
    r.rows.forEach((row) => { counts[row.status] += 1; });
    const typeLabel = (r.type_label || "").toLowerCase() + "s";
    const header = [
      h("div", { class: "row spread" },
        h("div", {}, h("h1", {}, `Compare ${typeLabel}`), h("div", { class: "small muted" }, r.source.name, " ", envBadge(r.source.environment), " → ", r.target.name, " ", envBadge(r.target.environment), ` · ${fmtTime(r.created_at)} · scanned ${r.scanned} object(s)`)),
        h("div", { class: "row" }, experimentalBadge(), badge(r.status, r.status === "completed" ? "b-ok" : r.status === "failed" ? "b-err" : "b-info"))),
      r.error ? h("div", { class: "banner err" }, r.error) : null,
      running ? h("div", { class: "banner info" }, "Reading both environments (read-only)… ", h("button", { onclick: () => guarded(async () => { await api("POST", `/api/compare/${compareId}/cancel`); }) }, "Cancel")) : null,
    ];
    if (r.status !== "completed") { showView(header); return; }
    if (!picked.size) {
      r.rows.forEach((row) => {
        if (row.status === "different") picked.set(row.source_id, new Set(row.diffs.filter((d) => !d.env_specific && !d.only_in_target).map((d) => d.attr)));
      });
    }
    const visible = r.rows.filter((row) => filter === "all" || (filter === "changes" ? ["new", "different"].includes(row.status) : row.status === filter));
    const chip = (k, l, n) => h("button", { class: "chip" + (filter === k ? " active" : ""), onclick: () => { filter = k; draw(r); } }, `${l} (${n})`);
    const chosen = [...picked.entries()].filter(([id, attrs]) => {
      const row = r.rows.find((x) => x.source_id === id);
      return row && (row.status === "new" || attrs.size);
    });
    const nameIn = h("input", { placeholder: `Copy ${typeLabel} from ${r.source.name} to ${r.target.name}`, maxlength: "200", style: "max-width:420px" });
    const create = h("button", { class: "primary", disabled: !chosen.length, onclick: () => guarded(async () => {
      create.disabled = true;
      try {
        const res = await api("POST", `/api/compare/${compareId}/create-job`, { name: nameIn.value, selections: chosen.map(([id, attrs]) => ({ source_id: id, attributes: [...attrs] })) });
        location.hash = `#/jobs/${res.id}`;
      } finally { create.disabled = false; }
    }) }, `Create copy job (${chosen.length})`);

    const rowsEl = visible.map((row) => {
      const selectable = ["new", "different"].includes(row.status);
      const objBox = h("input", { type: "checkbox", disabled: !selectable, checked: picked.has(row.source_id) && (row.status === "new" || picked.get(row.source_id).size > 0),
        onchange: (e) => {
          if (!e.target.checked) picked.set(row.source_id, new Set());
          else picked.set(row.source_id, new Set(row.status === "new" ? [] : row.diffs.filter((d) => !d.env_specific && !d.only_in_target).map((d) => d.attr)));
          if (row.status === "new" && !e.target.checked) picked.delete(row.source_id);
          draw(r);
        } });
      let detail = null;
      if (row.status === "different") {
        const attrs = picked.get(row.source_id) || new Set();
        detail = h("table", { class: "diff" },
          h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "Attribute"), h("th", {}, `In ${r.source.name}`), h("th", {}, ""), h("th", {}, `In ${r.target.name}`))),
          h("tbody", {}, row.diffs.map((d) => h("tr", { class: diffRowClass(d, attrs.has(d.attr)) },
            h("td", {}, h("input", { type: "checkbox", checked: attrs.has(d.attr), onchange: (e) => {
              const set = picked.get(row.source_id) || new Set();
              e.target.checked ? set.add(d.attr) : set.delete(d.attr);
              picked.set(row.source_id, set); draw(r);
            } })),
            h("td", {}, h("span", { class: "mono" }, d.label), d.env_specific ? h("span", { class: "badge b-warn", title: "Usually differs per environment; kept unless you tick it" }, "environment-specific") : null,
              d.only_in_target ? h("span", { class: "badge", title: "Not set in the source; copying removes it from the target" }, "only in target") : null),
            compareCells(d, attrs.has(d.attr), false)))));
      } else if (row.status === "new") {
        const kept = row.diffs.filter((d) => d.env_specific).map((d) => d.label);
        detail = h("div", { class: "small muted" }, `Not in ${r.target.name} — will be created with all copyable attributes.`,
          kept.length ? ` Not copied (environment-specific): ${kept.join(", ")}. Set target values in the job's overrides column if needed.` : "");
      } else if (row.status === "ambiguous") {
        detail = h("div", { class: "small", style: "color:var(--err)" }, row.problem);
      }
      return h("tr", { class: row.status === "same" ? "excluded" : "" },
        h("td", {}, objBox),
        h("td", {}, h("strong", {}, row.name), h("div", {}, badge(row.status, { new: "b-info", different: "b-warn", same: "", ambiguous: "b-err" }[row.status])),
          row.secrets && row.secrets.length ? h("div", { class: "small muted" }, `Secrets not copied: ${row.secrets.join(", ")}`) : null),
        h("td", {}, detail));
    });
    showView(header,
      h("div", { class: "panel" },
        h("div", { class: "chips" }, chip("changes", "To copy", counts.new + counts.different), chip("new", "New", counts.new), chip("different", "Different", counts.different),
          chip("same", "Same", counts.same), counts.ambiguous ? chip("ambiguous", "Ambiguous", counts.ambiguous) : null, chip("all", "All", r.rows.length)),
        r.only_in_target.length ? h("p", { class: "small muted" }, `Only in ${r.target.name} (left alone): ${r.only_in_target.join(", ")}`) : null,
        counts.different ? diffLegend(r.source.name, r.target.name) : null,
        visible.length ? h("table", { class: "compare-list" }, h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "Object"), h("th", {}, "Attributes"))), h("tbody", {}, rowsEl))
          : h("div", { class: "empty" }, r.rows.length ? "Nothing in this filter" : `No ${typeLabel} in ${r.source.name}`)),
      h("div", { class: "panel sticky-actions" },
        h("div", { class: "row" }, nameIn, create,
          h("span", { class: "small muted" }, `Creates a job on ${r.target.name}, planned read-only. Review every before → after, then approve.`))));
  };
  const load = async () => { const r = await api("GET", `/api/compare/${compareId}`); draw(r); return r.status === "running"; };
  if (await load()) pollTimer = setInterval(async () => { if (!(await load().catch(() => false))) { clearInterval(pollTimer); pollTimer = null; } }, 1000);
}

// ------------------------------------------------------------------ boot
(async () => {
  META = await api("GET", "/api/meta");
  document.getElementById("operator").textContent = `v${META.version} · ${META.operator}`;
  modal.addEventListener("click", (e) => { if (e.target === modal) closeModal(); });
  window.addEventListener("hashchange", route);
  route();
})();
