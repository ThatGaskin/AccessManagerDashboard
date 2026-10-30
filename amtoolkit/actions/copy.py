"""Copy objects between environments — e.g. from a test tenant to production — attribute by attribute.

A copy job runs against the *target* connection and reads from a second, *source* connection chosen as a
job option. Planning reads each object in both environments and translates every reference from source
IDs to target IDs by name, because IDs differ between tenants: owners and members by email (or username),
governance groups, access profiles and sources by name, entitlements by name (or value) within the
matching source. The plan shows every attribute's target value before → after; nothing is read from the
source environment while the job runs, so the change applied is exactly the one approved.

Environment-specific attributes — a source's connection settings (hosts, URLs, service accounts, search
DNs), its virtual appliance cluster, and governance group members — are kept as they are in the target
unless a row names them in `attributes`, or sets a target value for them in `overrides`. Secrets
(passwords, client secrets, tokens) are never copied: they cannot be read back from the API.

Objects missing in the target are created with every attribute except kept and environment-specific ones.
An object can refer to one created by an earlier row of the same job (e.g. an access profile approved by
a governance group copied above it); such references are resolved by name when the row runs.

Rollback restores the previous values of updated objects and deletes the objects the job created.
"""
import json
import re
from dataclasses import dataclass, replace

from ..connectors import model
from ..connectors.base import AuthError, ConfigError, ConnectorError
from ..util import parse_bool, split_list
from .base import Action, Column, ItemFailed, Option, Outcome, Plan, msg

COPY_TYPES = ("access_profile", "role", "governance_group", "source")
# A reference to an object that does not exist in the target yet but is created by an earlier row of the
# same job: "pending:<type>:<name>". Resolved by name when the row runs.
PENDING = "pending:"
GROUP_TOKEN = "governance_group:"
CONFIG = model.CONFIG_PREFIX


@dataclass(frozen=True)
class Attr:
    name: str
    label: str
    kind: str  # text, bool, number, identity, group, cluster, entitlements, access_profiles, approvers, members, config
    required: bool = False  # needed to create the object
    env_specific: bool = False  # kept in the target unless named explicitly
    secret: bool = False  # never copied
    marked: bool = False  # env_specific was set by a person (see ENV_MARKS), not by the built-in rules

    def to_dict(self):
        return {"name": self.name, "label": self.label, "kind": self.kind, "required": self.required,
                "env_specific": self.env_specific, "secret": self.secret, "marked": self.marked}


ATTRS = {
    "access_profile": [
        Attr("description", "Description", "text"), Attr("owner", "Owner", "identity", required=True),
        Attr("requestable", "Requestable", "bool"), Attr("enabled", "Enabled", "bool"),
        Attr("approval_schemes", "Approvers", "approvers"),
        Attr("comments_required", "Requester must comment", "bool"),
        Attr("denial_comments_required", "Approver must comment on denial", "bool"),
        Attr("entitlements", "Entitlements", "entitlements", required=True),
    ],
    "role": [
        Attr("description", "Description", "text"), Attr("owner", "Owner", "identity", required=True),
        Attr("requestable", "Requestable", "bool"), Attr("enabled", "Enabled", "bool"),
        Attr("access_profiles", "Access profiles", "access_profiles", required=True),
    ],
    "governance_group": [
        Attr("description", "Description", "text"), Attr("owner", "Owner", "identity", required=True),
        Attr("members", "Members", "members", env_specific=True),
    ],
    "source": [
        Attr("description", "Description", "text"), Attr("owner", "Owner", "identity", required=True),
        Attr("delete_threshold", "Account delete threshold (%)", "number"),
        Attr("authoritative", "Authoritative", "bool"),
        Attr("management_workgroup", "Management governance group", "group"),
        Attr("cluster", "Virtual appliance cluster", "cluster", env_specific=True),
    ],
}

# Connector settings are classified by name. Secrets cannot be read back from the API, so they are never
# copied; settings that point at a particular system or account usually differ per environment.
SECRET_PARTS = ("password", "passwd", "pwd", "secret", "token", "passphrase", "privatekey", "private_key",
                "credential", "apikey", "api_key")
ENV_PARTS = ("searchdn", "basedn", "clientid", "client_id", "orgid", "org_id", "accountid", "tenantid")
ENV_WORDS = {"host", "hostname", "hosts", "url", "urls", "uri", "server", "servers", "port", "domain", "domains",
             "user", "username", "login", "account", "tenant", "endpoint", "instance", "forest", "dn", "dns",
             "proxy", "cert", "certificate", "realm", "path", "ip", "address", "connection", "database", "schema",
             "catalog", "share", "sid", "service"}


def _words(key):
    return {w.lower() for w in re.findall(r"[A-Z]{2,}(?=[A-Z][a-z]|\b|_|\d|$)|[A-Z]?[a-z]+|[A-Z]+", key)}


def config_attr(key):
    low = key.lower()
    secret = any(p in low for p in SECRET_PARTS)
    env = not secret and (any(p in low for p in ENV_PARTS) or bool(_words(key) & ENV_WORDS))
    return Attr(CONFIG + key, key, "config", env_specific=env, secret=secret)


# Attributes people marked as environment-specific (True) or not (False) on the Compare and Copy pages,
# per object type: {type: {attribute: bool}}. They override the built-in classification everywhere —
# comparisons, default selections and plans. Loaded from the store and replaced whenever a mark changes.
ENV_MARKS = {}


def set_env_marks(marks):
    global ENV_MARKS
    ENV_MARKS = {t: dict(attrs) for t, attrs in (marks or {}).items()}


def with_mark(type_, spec):
    """The attribute with a person's environment-specific mark applied. Secrets cannot be marked."""
    mark = ENV_MARKS.get(type_, {}).get(spec.name)
    if mark is None or spec.secret or mark == spec.env_specific:
        return spec
    return replace(spec, env_specific=mark, marked=True)


def builtin_spec(type_, name):
    """An attribute as the built-in rules classify it, or None when the type has no such attribute."""
    if type_ == "source" and name.startswith(CONFIG) and len(name) > len(CONFIG):
        return config_attr(name[len(CONFIG):])
    return next((a for a in ATTRS.get(type_, ()) if a.name == name), None)


def attr_specs(type_, obj=None, marks=True):
    """The copyable attributes of a type; for sources also one per connector setting of `obj`. With
    `marks`, environment-specific marks people set are applied."""
    specs = list(ATTRS[type_])
    if type_ == "source" and obj:
        specs += [config_attr(k) for k in sorted(obj.get("config") or {})]
    return {a.name: with_mark(type_, a) if marks else a for a in specs}


def default_selection(specs):
    return [a.name for a in specs.values() if not a.env_specific and not a.secret]


value_of = model.value_of


def _fmt(value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, dict)):
        return json.dumps(value) if value else "(none)"
    return f"'{value}'" if value not in (None, "") else "(empty)"


def _pending(type_, name):
    return f"{PENDING}{type_}:{name}"


def same(field, a, b):
    if field == "approval_schemes":  # approvers are asked in order
        return list(a or []) == list(b or [])
    return model.same(a, b)


class Mapper:
    """Translates attribute values from the source environment to the target, with names for display."""

    def __init__(self, ctx, m, field_name="attributes"):
        self.ctx = ctx
        self.src = ctx.source
        self.dst = ctx.connector
        self.m = m
        self.field = field_name
        self.names = {}  # target ID -> display name

    def _call(self, side, key, fn):
        try:
            return self.ctx.cached(("copy", side) + key, fn)
        except (AuthError, ConfigError) as exc:
            if side == "src":
                raise type(exc)(f"Copy-from connection: {exc}") from None
            raise

    def error(self, text, code="not_found"):
        self.m.append(msg("error", text, self.field, code))

    def one(self, found, label, shown):
        if not found:
            return None
        if len(found) > 1:
            self.error(f"{label} '{shown}' matches {len(found)} objects in the target; names must be unique",
                       "ambiguous")
            return None
        return found[0]

    def pending(self, type_, name):
        return self.ctx.peek(("copy_pending", type_, name.lower()))

    # ------------------------------------------------------------ single references
    def identity(self, src_id, label):
        if not src_id:
            return None
        found = self._call("src", ("identity", src_id), lambda: self.src.find_identities(src_id))
        if not found:
            self.error(f"{label}: identity {src_id} no longer exists in the source environment")
            return None
        person = found[0]
        for ref in (person.get("email"), person.get("alias")):
            if not ref:
                continue
            match = self._call("dst", ("identity", ref.lower()), lambda r=ref: self.dst.find_identities(r))
            if match:
                target = self.one(match, label, ref)
                if target:
                    self.names[target["id"]] = target.get("name") or ref
                    return target["id"]
                return None
        shown = person.get("email") or person.get("alias") or src_id
        self.error(f"{label} {person.get('name') or shown} ({shown}) has no identity with that email or username "
                   "in the target environment")
        return None

    def group(self, src_id, label):
        if not src_id:
            return None
        found = self._call("src", ("group", src_id), lambda: self.src.find_governance_groups(src_id))
        if not found:
            self.error(f"{label}: governance group {src_id} no longer exists in the source environment")
            return None
        name = found[0]["name"]
        match = self._call("dst", ("group", name.lower()), lambda: self.dst.find_governance_groups(name))
        match = [g for g in match if (g.get("name") or "").lower() == name.lower()]
        if match:
            target = self.one(match, "Governance group", name)
            if target:
                self.names[target["id"]] = name
                return target["id"]
            return None
        planned = self.pending("governance_group", name)
        if planned:
            self.m.append(msg("info", f"{label}: governance group '{name}' is created by {planned} of this job",
                              self.field, "copy_pending"))
            token = _pending("governance_group", name)
            self.names[token] = f"{name} (new)"
            return token
        self.error(f"{label}: governance group '{name}' does not exist in the target. Copy it first — add a "
                   f"governance_group row for '{name}' above this one.")
        return None

    def access_profile(self, src_id, name, label):
        if not name:
            obj = self._call("src", ("object", "access_profile", src_id),
                             lambda: self.src.get_object("access_profile", src_id))
            name = obj and obj["name"]
        if not name:
            self.error(f"{label}: access profile {src_id} no longer exists in the source environment")
            return None
        match = self._call("dst", ("object", "access_profile", name.lower()),
                           lambda: self.dst.find_objects("access_profile", name))
        match = [o for o in match if (o.get("name") or "").lower() == name.lower()]
        if match:
            target = self.one(match, "Access profile", name)
            if target:
                self.names[target["id"]] = name
                return target["id"]
            return None
        planned = self.pending("access_profile", name)
        if planned:
            self.m.append(msg("info", f"{label}: access profile '{name}' is created by {planned} of this job",
                              self.field, "copy_pending"))
            token = _pending("access_profile", name)
            self.names[token] = f"{name} (new)"
            return token
        self.error(f"{label}: access profile '{name}' does not exist in the target. Copy it first — add an "
                   f"access_profile row for '{name}' above this one.")
        return None

    def source(self, name, label):
        match = self._call("dst", ("source", name.lower()), lambda: self.dst.find_sources(name))
        if not match:
            self.error(f"{label}: source '{name}' does not exist in the target environment (sources are matched "
                       "by name)")
            return None
        return self.one(match, "Source", name)

    def cluster(self, src_id, src_name, label):
        if not src_id:
            return None
        name = src_name
        if not name:
            found = self._call("src", ("cluster", src_id), lambda: self.src.find_clusters(src_id))
            name = found[0]["name"] if found else None
        if not name:
            self.error(f"{label}: cluster {src_id} was not found in the source environment")
            return None
        return self.target_cluster(name, label)

    def target_cluster(self, ref, label):
        match = self._call("dst", ("cluster", ref.lower()), lambda: self.dst.find_clusters(ref))
        if not match:
            self.error(f"{label}: no virtual appliance cluster '{ref}' in the target environment")
            return None
        target = self.one(match, "Cluster", ref)
        if target:
            self.names[target["id"]] = target.get("name") or ref
            return target["id"]
        return None

    # ------------------------------------------------------------ lists
    def entitlements(self, src_ids, src_source_name, label):
        if not src_source_name:
            self.error(f"{label}: the access profile has no source in the source environment")
            return None
        source = self.source(src_source_name, label)
        if source is None:
            return None
        out, ok = [], True
        for src_id in src_ids:
            ent = self._call("src", ("object", "entitlement", src_id), lambda i=src_id: self.src.get_object("entitlement", i))
            if not ent:
                self.error(f"{label}: entitlement {src_id} no longer exists in the source environment")
                ok = False
                continue
            match = []
            for ref in dict.fromkeys(r for r in (ent.get("name"), ent.get("value")) if r):
                match = self._call("dst", ("entitlement", source["id"], ref.lower()),
                                   lambda r=ref: self.dst.find_entitlements(source["id"], r))
                if match:
                    break
            if not match:
                self.error(f"{label}: entitlement '{ent.get('name') or ent.get('value')}' was not found on source "
                           f"'{source['name']}' in the target (aggregate the source there first?)")
                ok = False
                continue
            target = self.one(match, "Entitlement", ent.get("name") or ent.get("value"))
            if target is None:
                ok = False
                continue
            self.names[target["id"]] = target.get("name") or target.get("value")
            out.append(target["id"])
        return sorted(set(out)) if ok else None

    def access_profiles(self, src_ids, src_names, label):
        out, ok = [], True
        for src_id in src_ids:
            mapped = self.access_profile(src_id, src_names.get(src_id), label)
            ok = ok and mapped is not None
            if mapped:
                out.append(mapped)
        return sorted(set(out)) if ok else None

    def approvers(self, tokens, label):
        out, ok = [], True
        for token in tokens:
            if token.startswith(GROUP_TOKEN):
                mapped = self.group(token[len(GROUP_TOKEN):], label)
                ok = ok and mapped is not None
                if mapped:
                    out.append(GROUP_TOKEN + mapped)
            else:
                out.append(token)
        return out if ok else None

    def members(self, src_group_id, label):
        members = self._call("src", ("members", src_group_id),
                             lambda: self.src.list_governance_group_members(src_group_id))
        out, ok = [], True
        for member in members:
            mapped = self.identity(member["id"], f"{label} {member.get('name') or ''}".strip())
            ok = ok and mapped is not None
            if mapped:
                out.append(mapped)
        return sorted(set(out)) if ok else None

    def translate(self, spec, src):
        """The source object's value of one attribute, expressed in target IDs. None + an error when it
        cannot be translated."""
        value = value_of(src, spec.name)
        label = spec.label
        if spec.kind in ("text", "bool", "number", "config"):
            return value
        if spec.kind == "identity":
            if not value:
                return None
            return self.identity(value, label)
        if spec.kind == "group":
            return self.group(value, label) if value else None
        if spec.kind == "cluster":
            return self.cluster(value, src.get("cluster_name"), label) if value else None
        if spec.kind == "entitlements":
            return self.entitlements(value or [], src.get("source_name"), label)
        if spec.kind == "access_profiles":
            return self.access_profiles(value or [], src.get("access_profile_names") or {}, label)
        if spec.kind == "approvers":
            return self.approvers(value or [], label)
        if spec.kind == "members":
            return self.members(src["id"], label)
        raise ValueError(spec.kind)


# ---------------------------------------------------------------------------- display
def describe(spec, value, names):
    """A value as people read it: names instead of IDs."""
    def name(v):
        return names.get(v, v)
    if spec.kind in ("identity", "group", "cluster"):
        return f"'{name(value)}'" if value else "(none)"
    if spec.kind == "approvers":
        return ", ".join(f"group {name(t[len(GROUP_TOKEN):])}" if t.startswith(GROUP_TOKEN) else t
                         for t in value or []) or "(none)"
    if spec.kind in ("entitlements", "access_profiles", "members"):
        return ", ".join(sorted(str(name(v)) for v in value or [])) or "(none)"
    return _fmt(value)


def change_label(spec, before, after, names):
    if spec.kind in ("entitlements", "access_profiles", "members"):
        before, after = set(before or []), set(after or [])
        added = sorted(str(names.get(v, v)) for v in after - before)
        removed = sorted(str(names.get(v, v)) for v in before - after)
        parts = ([f"+ {', '.join(added)}"] if added else []) + ([f"− {', '.join(removed)}"] if removed else [])
        return [f"{len(before)} item(s)", f"{len(after)} item(s): " + "; ".join(parts)]
    return [describe(spec, before, names), describe(spec, after, names)]


def target_names(obj):
    """ID -> name for the references a target object already carries."""
    names = {}
    if obj.get("owner"):
        names[obj["owner"]] = obj.get("owner_name") or obj["owner"]
    for key in ("entitlement_names", "access_profile_names"):
        names.update(obj.get(key) or {})
    if obj.get("cluster"):
        names[obj["cluster"]] = obj.get("cluster_name") or obj["cluster"]
    if obj.get("management_workgroup"):
        names[obj["management_workgroup"]] = obj.get("management_workgroup_name") or obj["management_workgroup"]
    return names


# ---------------------------------------------------------------------------- execution helpers
def resolve_pending(c, value):
    """Replaces pending references (objects created by earlier rows of the job) with their IDs."""
    if isinstance(value, list):
        return [resolve_pending(c, v) for v in value]
    if isinstance(value, dict):
        return {k: resolve_pending(c, v) for k, v in value.items()}
    if isinstance(value, str) and value.startswith(GROUP_TOKEN + PENDING):
        return GROUP_TOKEN + resolve_pending(c, value[len(GROUP_TOKEN):])
    if isinstance(value, str) and value.startswith(PENDING):
        type_, name = value[len(PENDING):].split(":", 1)
        found = c.find_governance_groups(name) if type_ == "governance_group" else c.find_objects(type_, name)
        found = [o for o in found if (o.get("name") or "").lower() == name.lower()]
        if len(found) != 1:
            raise ItemFailed(f"{model.TYPE_LABELS[type_]} '{name}' should have been created by an earlier row of this "
                             f"job but {'was not found' if not found else 'matches several objects'}. Run that row "
                             "first, then retry this one.", retryable=False)
        return found[0]["id"]
    return value


def read_target(c, type_, object_id, with_members):
    current = c.get_object(type_, object_id)
    if current is not None and with_members:
        current = dict(current, members=sorted(x["id"] for x in c.list_governance_group_members(object_id)))
    return current


def apply_update(ctx, obj, before, after, in_doubt):
    """Sets fields of an existing object; refuses when someone else changed them since planning."""
    c = ctx.connector
    try:
        current = read_target(c, obj["type"], obj["id"], "members" in after)
        if current is None:
            raise ItemFailed(f"{model.TYPE_LABELS[obj['type']]} '{obj['name']}' no longer exists")
        after = resolve_pending(c, after)
        if all(same(f, value_of(current, f), v) for f, v in after.items()):
            return Outcome("succeeded" if in_doubt else "skipped", obj["id"], "Already has the planned values",
                           {"reconciled": bool(in_doubt)})
        drift = [f for f, v in after.items() if not same(f, value_of(current, f), before.get(f))
                 and not same(f, value_of(current, f), v)]
        if drift:
            raise ItemFailed(f"Changed by someone else since planning ({', '.join(drift)}). Re-check this row to see "
                             "the current values before applying.")
        fields = {f: v for f, v in after.items() if f != "members" and not same(f, value_of(current, f), v)}
        if fields:
            c.patch_object(obj["type"], obj["id"], fields)
        if "members" in after:
            have, want = set(current["members"]), set(after["members"])
            problems = c.add_governance_group_members(obj["id"], sorted(want - have)) + \
                c.remove_governance_group_members(obj["id"], sorted(have - want))
            if problems:
                raise ItemFailed("Some members were not changed: " + "; ".join(problems), in_doubt=True)
    except ConnectorError as exc:
        if isinstance(exc, (AuthError, ConfigError)):
            raise
        raise ItemFailed(str(exc), retryable=exc.retryable, in_doubt=exc.ambiguous) from None
    return Outcome("succeeded", obj["id"], "Updated " + ", ".join(after), {"before": before})


def find_by_name(c, type_, name):
    found = c.find_governance_groups(name) if type_ == "governance_group" else c.find_objects(type_, name)
    return [o for o in found if (o.get("name") or "").lower() == name.lower()]


CREATE = {"access_profile": "create_access_profile", "role": "create_role",
          "governance_group": "create_governance_group", "source": "create_source"}
DELETE = {"access_profile": "delete_access_profile", "role": "delete_role",
          "governance_group": "delete_governance_group", "source": "delete_source"}


def _identity_ref(id_, names):
    return {"type": "IDENTITY", "id": id_, "name": names.get(id_)}


def create_payload(type_, name, values, names, source=None):
    """The API body creating an object with the given (target) attribute values."""
    def get(field, default=None):
        return values.get(field, default)
    body = {"name": name, "description": get("description") or None}
    if get("owner"):
        body["owner"] = _identity_ref(get("owner"), names)
    if type_ == "access_profile":
        body.update(source={"type": "SOURCE", "id": source["id"], "name": source["name"]},
                    entitlements=[{"type": "ENTITLEMENT", "id": i, "name": names.get(i)} for i in get("entitlements", [])],
                    enabled=get("enabled", True), requestable=get("requestable", True),
                    accessRequestConfig={"commentsRequired": get("comments_required", False),
                                         "denialCommentsRequired": get("denial_comments_required", False),
                                         "approvalSchemes": model.scheme_objects(get("approval_schemes", []))})
    elif type_ == "role":
        body.update(accessProfiles=[{"type": "ACCESS_PROFILE", "id": i, "name": names.get(i)}
                                    for i in get("access_profiles", [])],
                    enabled=get("enabled", True), requestable=get("requestable", False))
    elif type_ == "source":
        body.update(connector=values["connector"],
                    connectorAttributes={f[len(CONFIG):]: v for f, v in values.items() if f.startswith(CONFIG)})
        if get("delete_threshold") is not None:
            body["deleteThreshold"] = get("delete_threshold")
        if "authoritative" in values:
            body["authoritative"] = get("authoritative")
        if get("cluster"):
            body["cluster"] = {"type": "CLUSTER", "id": get("cluster"), "name": names.get(get("cluster"))}
        if get("management_workgroup"):
            body["managementWorkgroup"] = {"type": "GOVERNANCE_GROUP", "id": get("management_workgroup"),
                                           "name": names.get(get("management_workgroup"))}
    return body


# ---------------------------------------------------------------------------- the actions
def parse_overrides(text, m):
    """'key=value;key=value' → {key: value}. Values of list settings are separated by '|'."""
    out = {}
    # Only ';' and line breaks separate overrides here: '|' separates the values of one list setting.
    for part in (p.strip() for p in str(text or "").replace("\n", ";").split(";")):
        if not part:
            continue
        if "=" not in part:
            m.append(msg("error", f"Override '{part}' must look like name=value", "overrides", "invalid"))
            continue
        key, value = part.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def coerce(text, like):
    """An override's text as the type of the setting it replaces."""
    if isinstance(like, bool):
        return parse_bool(text, None)
    if isinstance(like, list):
        return [v.strip() for v in text.split("|") if v.strip()]
    if isinstance(like, int) and not isinstance(like, bool):
        return int(text)
    return text


class CopyObjects(Action):
    key = "object.copy"
    title = "Copy objects between environments"
    description = ("Copies access profiles, roles, governance groups and sources from another connection (e.g. "
                   "test) to this one (e.g. production), attribute by attribute. References are matched by name; "
                   "the plan shows every attribute before → after. Objects missing here are created. "
                   "Environment-specific settings are kept unless you name them.")
    inverse = "object.restore"
    source_connection = True
    hidden = True  # driven from the Copy and Compare pages, not from an uploaded file
    columns = [
        Column("object_type", True, "access_profile, role, governance_group or source.", "access_profile"),
        Column("object", True, "Name or ID of the object in the environment you copy FROM.", "Finance - Read"),
        Column("target_name", False, "Name in this environment, when it differs. Blank = the same name.", ""),
        Column("attributes", False, "Attributes to copy, separated by ';'. Blank = all except environment-specific "
               "ones (members, cluster, connection settings). New objects always get the attributes they cannot be "
               "created without. For sources, "
               "connector settings are named config.<setting>, e.g. config.pageSize.", "description;owner"),
        Column("keep", False, "Attributes to leave exactly as they are here, separated by ';' — also for new "
               "objects.", "requestable"),
        Column("overrides", False, "Sources only: values to set here instead of the copied ones, as "
               "name=value separated by ';' (list values separated by '|'). Use it for environment-specific "
               "settings such as config.host or cluster.", "config.host=dc01.prod.local;cluster=Prod VA"),
    ]
    options = [
        Option("source_connection_id", "Copy from", "", [], "The environment to read the objects from. This job's "
               "connection is the one that is changed.", kind="connection"),
        Option("if_missing", "When an object does not exist here yet", "create", ["create", "error"],
               "create = create it with every copyable attribute; error = flag the row so you must decide."),
    ]

    def clean_options(self, raw):
        opts = super().clean_options(raw)
        opts["source_connection_id"] = str((raw or {}).get("source_connection_id") or "").strip()
        return opts

    def duplicate_key(self, row):
        type_ = model.parse_type(row.get("object_type"))
        name = (row.get("target_name") or row.get("object") or "").lower()
        return f"{type_}:{name}" if name else None

    def references(self, row):
        return iter(())  # everything is resolved through the source environment while planning

    def seed_context(self, ctx, items):
        """Registers the objects earlier rows of the job create, so references to them can be planned."""
        for item in sorted(items, key=lambda i: i["row_no"]):
            p = item.get("payload") or {}
            if item.get("op") == "create" and item.get("included") and p.get("object"):
                ctx.remember(("copy_pending", p["object"]["type"], p["object"]["name"].lower()), f"line {item['row_no']}")

    # ------------------------------------------------------------ planning
    def plan(self, ctx, row, options):
        m = []
        if getattr(ctx, "source", None) is None:
            return Plan("error", "", None, [msg("error", "Choose the connection to copy from (job option)", None,
                                                "no_source")])
        type_ = model.parse_type(row.get("object_type"))
        if type_ not in COPY_TYPES:
            return Plan("error", row.get("object") or "", None, [msg(
                "error", "object_type must be one of: " + ", ".join(COPY_TYPES), "object_type", "invalid")])
        ref = row.get("object") or ""
        if not ref:
            return Plan("error", "", None, [msg("error", "object is required", "object", "required")])
        mapper = Mapper(ctx, m)
        label = model.TYPE_LABELS[type_]
        found = mapper._call("src", ("object", type_, ref.lower()), lambda: ctx.source.find_objects(type_, ref))
        found = [o for o in found if o["id"].lower() == ref.lower() or (o.get("name") or "").lower() == ref.lower()]
        if not found:
            return Plan("error", ref, None, [msg("error", f"{label} '{ref}' was not found in the environment you copy "
                                                          "from", "object", "not_found")])
        if len(found) > 1:
            return Plan("error", ref, None, [msg("error", f"'{ref}' matches {len(found)} objects; use the ID",
                                                 "object", "ambiguous")])
        src = found[0]
        name = row.get("target_name") or src["name"]
        if type_ == "governance_group":
            src = dict(src, members=[])  # members are read only when copied
        specs = attr_specs(type_, src)
        overrides = self._overrides(type_, row, specs, src, m)
        keep = self._names(type_, row.get("keep"), specs, "keep", m)
        existing = mapper._call("dst", ("object", type_, name.lower()), lambda: find_by_name(ctx.connector, type_, name))
        if len(existing) > 1:
            m.append(msg("error", f"'{name}' matches {len(existing)} objects in this environment", "target_name",
                         "ambiguous"))
        if any(x["level"] == "error" for x in m):
            return Plan("error", name, None, m)
        source_ref = {"id": src["id"], "name": src["name"]}
        if existing:
            current = ctx.connector.get_object(type_, existing[0]["id"])
            if current is None:
                return Plan("error", name, None, m + [msg("error", f"{label} '{name}' could not be read here", "object",
                                                          "not_found")])
            return self._plan_update(ctx, mapper, type_, src, current, specs, row, keep, overrides, m, source_ref)
        if options.get("if_missing") == "error":
            m.append(msg("error", f"{label} '{name}' does not exist here (option: when missing → error)", "object",
                         "missing"))
            return Plan("error", name, None, m)
        return self._plan_create(ctx, mapper, type_, name, src, specs, row, keep, overrides, m, source_ref)

    @staticmethod
    def _names(type_, text, specs, column, m):
        out = []
        for raw in split_list(text):
            key = raw.strip()
            if key not in specs and key.lower().replace(" ", "_") in specs:
                key = key.lower().replace(" ", "_")
            if key not in specs and type_ == "source" and CONFIG + key in specs:
                key = CONFIG + key
            if key not in specs:
                if type_ == "source" and key.startswith(CONFIG):
                    out.append(key)  # a setting the source object does not have: copying it removes it here
                    continue
                known = ", ".join(a for a in specs if not a.startswith(CONFIG))
                m.append(msg("error", f"Unknown attribute '{raw}' for {model.TYPE_LABELS[type_].lower()}s. Known: "
                                      f"{known}" + (", config.<setting>" if type_ == "source" else ""), column,
                             "invalid"))
                continue
            if specs[key].secret:
                m.append(msg("error", f"'{key}' is a secret; secrets cannot be read from the API and are never copied. "
                                      "Set it in this environment directly.", column, "secret"))
                continue
            out.append(key)
        return list(dict.fromkeys(out))

    def _overrides(self, type_, row, specs, src, m):
        raw = parse_overrides(row.get("overrides"), m)
        if raw and type_ != "source":
            m.append(msg("error", "overrides are only supported for sources", "overrides", "invalid"))
            return {}
        out = {}
        for key, text in raw.items():
            name = key if key in specs or key.startswith(CONFIG) else CONFIG + key if CONFIG + key in specs else key
            if name != "cluster" and not name.startswith(CONFIG):
                m.append(msg("error", f"Override '{key}': only cluster and connector settings (config.<setting>) can be "
                                      "overridden", "overrides", "invalid"))
                continue
            if name in specs and specs[name].secret:
                m.append(msg("error", f"Override '{key}' is a secret; set secrets in this environment directly",
                             "overrides", "secret"))
                continue
            if name == "cluster":
                out[name] = text
                continue
            try:
                value = coerce(text, value_of(src, name))
            except ValueError as exc:
                m.append(msg("error", f"Override '{key}': {exc}", "overrides", "invalid"))
                continue
            out[name] = value
        return out

    def _values(self, ctx, mapper, type_, src, names, overrides):
        """Target values of the given attributes: translated from the source, or taken from overrides."""
        specs = attr_specs(type_, src)
        values = {}
        for name in names:
            spec = specs.get(name) or config_attr(name[len(CONFIG):])
            if name == "cluster" and name in overrides:
                values[name] = mapper.target_cluster(overrides[name], spec.label)
            elif name in overrides:
                values[name] = overrides[name]
            elif name.startswith(CONFIG) and name not in specs:
                values[name] = None
            else:
                values[name] = mapper.translate(spec, src)
        return values

    def _kept_note(self, type_, src, current, specs, chosen, m):
        """Lists the environment-specific attributes that differ but are deliberately left alone."""
        kept = []
        for spec in specs.values():
            if spec.name in chosen or spec.secret or not spec.env_specific or (current is None and spec.kind == "config"):
                continue
            if spec.kind in ("text", "number", "bool", "config") and \
                    model.same(value_of(src, spec.name), value_of(current or {}, spec.name)):
                continue
            kept.append(spec.label if spec.kind == "config" else spec.name)
        if kept:
            where = "left as they are here" if current else "not copied"
            m.append(msg("info", f"Environment-specific, {where}: {', '.join(kept)}. Name them in attributes or "
                                 "overrides to change them.", "attributes", "kept"))
        secrets = [s.label for s in specs.values() if s.secret]
        if secrets:
            m.append(msg("info" if current else "warning",
                         f"Secrets are never copied: {', '.join(secrets)}"
                         + ("" if current else " — set them in this environment after the source is created"),
                         "attributes", "secret"))

    def _plan_update(self, ctx, mapper, type_, src, current, specs, row, keep, overrides, m, source_ref):
        chosen = self._names(type_, row.get("attributes"), specs, "attributes", m) if row.get("attributes") \
            else default_selection(specs)
        for name in chosen:
            spec = specs.get(name)
            if spec and spec.env_specific and name not in overrides:
                m.append(msg("warning", f"{spec.label} is environment-specific; the value from the source environment "
                                        "will be copied here", "attributes", "env_specific"))
        chosen = [a for a in dict.fromkeys(chosen + list(overrides)) if a not in keep]
        if not chosen:
            m.append(msg("error", "Nothing to copy — every attribute is kept", "attributes", "empty"))
        members = ctx.connector.list_governance_group_members(current["id"]) if "members" in chosen else []
        if "members" in chosen:
            current = dict(current, members=sorted(x["id"] for x in members))
        values = self._values(ctx, mapper, type_, src, chosen, overrides)
        if any(x["level"] == "error" for x in m):
            return Plan("error", current["name"], None, m)
        names = dict(target_names(current))
        names.update(mapper.names)
        for x in members:
            names.setdefault(x["id"], x.get("name") or x["id"])
        changes = {f: v for f, v in values.items() if not same(f, value_of(current, f), v)}
        if changes and model.changed_later(current.get("modified"), src.get("modified")) == "first":
            m.append(msg("warning", f"Changed here more recently ({current['modified']}) than in the environment you "
                                    f"copy from ({src['modified']}). Moving overwrites that newer change — check the "
                                    "history first.", "attributes", "target_newer"))
        if not changes:
            m.append(msg("info", "Already the same here; this row will be skipped", None, "unchanged"))
            self._kept_note(type_, src, current, specs, chosen, m)
            return Plan("skip", current["name"], None, m)
        labels = {}
        for field, value in changes.items():
            spec = specs.get(field) or config_attr(field[len(CONFIG):])
            labels[field] = change_label(spec, value_of(current, field), value, names)
            m.append(msg("info", f"{spec.label}: {labels[field][0]} → {labels[field][1]}", "attributes", "change"))
        self._kept_note(type_, src, current, specs, chosen, m)
        payload = {"object": {"type": type_, "id": current["id"], "name": current["name"]}, "copied_from": source_ref,
                   "before": {f: value_of(current, f) for f in changes}, "after": changes, "labels": labels}
        return Plan("update", current["name"], payload, m)

    def _plan_create(self, ctx, mapper, type_, name, src, specs, row, keep, overrides, m, source_ref):
        label = model.TYPE_LABELS[type_]
        if row.get("attributes"):  # the chosen attributes, plus the ones the object cannot be created without
            picked = self._names(type_, row["attributes"], specs, "attributes", m)
            chosen = picked + [a.name for a in specs.values() if a.required and a.name not in picked]
        else:
            chosen = default_selection(specs)
        chosen = [a for a in dict.fromkeys(chosen + list(overrides)) if a not in keep]
        for spec in specs.values():
            if spec.required and spec.name in keep:
                m.append(msg("error", f"{spec.label} is required to create the {label.lower()}; it cannot be kept",
                             "keep", "required"))
        if type_ == "source" and src.get("cluster") and "cluster" not in chosen:
            m.append(msg("error", "The source runs on a virtual appliance cluster, which differs per environment. "
                                  "Choose the cluster to use here (overrides: cluster=<cluster name>).", "overrides",
                         "cluster_required"))
        values = self._values(ctx, mapper, type_, src, chosen, overrides)
        source = None
        if type_ == "access_profile":
            source = mapper.source(src.get("source_name") or "", "Source") if src.get("source_name") else None
            if source is None and not any(x["level"] == "error" for x in m):
                mapper.error("The access profile has no source")
        if type_ == "source":
            values["connector"] = src.get("connector")
            if not values["connector"]:
                mapper.error("The source's connector type could not be read")
            missing = [spec.label for spec in specs.values() if spec.kind == "config" and spec.env_specific
                       and spec.name not in chosen]
            if missing:
                m.append(msg("warning", f"Connection settings left empty (they differ per environment): "
                                        f"{', '.join(missing)}. Add them with overrides, or set them here after "
                                        "creation.", "overrides", "env_specific"))
        for spec in specs.values():
            if spec.required and spec.name in values and not values[spec.name] and \
                    not any(x["level"] == "error" for x in m):
                mapper.error(f"{spec.label} is required to create the {label.lower()}", "required")
        if any(x["level"] == "error" for x in m):
            return Plan("error", name, None, m)
        names = dict(mapper.names)
        body = create_payload(type_, name, values, names, source)
        m.append(msg("info", f"Does not exist here yet; it will be created with: "
                             f"{', '.join(a for a in chosen if a in values) or 'its name only'}", None, "copy_create"))
        for field in chosen:
            spec = specs.get(field) or config_attr(field[len(CONFIG):])
            if field in values:
                m.append(msg("info", f"{spec.label}: {describe(spec, values[field], names)}", "attributes", "change"))
        self._kept_note(type_, src, None, specs, chosen, m)
        ctx.remember(("copy_pending", type_, name.lower()), "an earlier row")
        payload = {"object": {"type": type_, "name": name}, "copied_from": source_ref, "create": body,
                   "members": values.get("members") or []}
        return Plan("create", name, payload, m)

    # ------------------------------------------------------------ execution
    def execute(self, ctx, item, options):
        p = item["payload"]
        in_doubt = (item.get("result") or {}).get("in_doubt")
        if item["op"] == "update":
            return apply_update(ctx, p["object"], p["before"], p["after"], in_doubt)
        return self._create(ctx, p, in_doubt)

    def _create(self, ctx, p, in_doubt):
        c = ctx.connector
        type_, name = p["object"]["type"], p["object"]["name"]
        try:
            existing = find_by_name(c, type_, name)
            if existing and not in_doubt:
                return Outcome("skipped", existing[0]["id"], "Already exists; created by someone else after planning")
            if existing:
                object_id, note = existing[0]["id"], "Found the object from an earlier interrupted attempt"
            else:
                body = resolve_pending(c, p["create"])
                try:
                    created = getattr(c, CREATE[type_])(body)
                except ConnectorError as exc:
                    again = find_by_name(c, type_, name) if exc.ambiguous else []
                    if not again:
                        raise
                    created = again[0]
                object_id, note = created.get("id"), "Created"
            members = resolve_pending(c, p.get("members") or [])
            if type_ == "governance_group" and members:
                problems = c.add_governance_group_members(object_id, members)
                if problems:
                    raise ItemFailed(f"Created ({object_id}) but some members were not added: {'; '.join(problems)}",
                                     in_doubt=True)
        except ConnectorError as exc:
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            raise ItemFailed(str(exc), retryable=exc.retryable, in_doubt=exc.ambiguous) from None
        return Outcome("succeeded", object_id, note, {"id": object_id, "created": True})

    def rollback_row(self, item):
        if item["status"] != "succeeded" or not item.get("payload"):
            return None
        p = item["payload"]
        type_ = p["object"]["type"]
        if item["op"] == "create":
            if not (item.get("result") or {}).get("created") or not item.get("remote_id"):
                return None
            return {"object_type": type_, "object": item["remote_id"], "delete": "yes"}
        return {"object_type": type_, "object": p["object"]["id"], "values": json.dumps(p["before"], sort_keys=True)}


class RestoreObjects(Action):
    """The rollback of a copy job: puts back the previous values of updated objects and deletes created ones."""
    key = "object.restore"
    title = "Restore copied objects"
    description = ("Rollback of 'Copy objects between environments': restores the attribute values objects had "
                   "before the copy, and deletes the objects it created. Built automatically by Create rollback.")
    hidden = True
    columns = [
        Column("object_type", True, "access_profile, role, governance_group or source.", "access_profile"),
        Column("object", True, "ID of the object in this environment.", "", ref="id"),
        Column("values", False, "The attribute values to restore, as JSON.", '{"description": "Old text"}'),
        Column("delete", False, "yes = delete the object (it was created by the copy).", "no"),
    ]

    def duplicate_key(self, row):
        return (row.get("object") or "").lower() or None

    def references(self, row):
        return iter(())

    def plan(self, ctx, row, options):
        m = []
        type_ = model.parse_type(row.get("object_type"))
        if type_ not in COPY_TYPES:
            return Plan("error", "", None, [msg("error", "object_type must be one of: " + ", ".join(COPY_TYPES),
                                                "object_type", "invalid")])
        try:
            delete = parse_bool(row.get("delete"), False)
        except ValueError as exc:
            return Plan("error", "", None, [msg("error", str(exc), "delete", "invalid")])
        values = {}
        if not delete:
            try:
                values = json.loads(row.get("values") or "{}")
            except ValueError:
                values = None
            if not isinstance(values, dict) or not values:
                return Plan("error", "", None, [msg("error", "values must be a JSON object of attribute values",
                                                    "values", "invalid")])
        current = read_target(ctx.connector, type_, row["object"], "members" in values)
        if current is None:
            if delete:
                return Plan("skip", row["object"], None, [msg("info", "Already gone; skipped", "object", "missing")])
            return Plan("error", row["object"], None, [msg("error", f"{model.TYPE_LABELS[type_]} not found", "object",
                                                           "not_found")])
        obj = {"type": type_, "id": current["id"], "name": current["name"]}
        if delete:
            if type_ == "source":
                m.append(msg("warning", "Deleting a source also removes its accounts and entitlements here", "delete"))
            return Plan("delete", current["name"], {"object": obj}, m)
        specs = attr_specs(type_, current)
        changes = {f: v for f, v in values.items() if not same(f, value_of(current, f), v)}
        if not changes:
            return Plan("skip", current["name"], None, [msg("info", "Already has these values; skipped", None,
                                                            "unchanged")])
        names = target_names(current)
        labels = {}
        for field, value in changes.items():
            spec = specs.get(field) or config_attr(field[len(CONFIG):])
            labels[field] = change_label(spec, value_of(current, field), value, names)
            m.append(msg("info", f"{spec.label}: {labels[field][0]} → {labels[field][1]}", "values", "change"))
        return Plan("update", current["name"], {"object": obj, "before": {f: value_of(current, f) for f in changes},
                                                "after": changes, "labels": labels}, m)

    def execute(self, ctx, item, options):
        p = item["payload"]
        if item["op"] == "update":
            return apply_update(ctx, p["object"], p["before"], p["after"], (item.get("result") or {}).get("in_doubt"))
        obj = p["object"]
        try:
            getattr(ctx.connector, DELETE[obj["type"]])(obj["id"])
        except ConnectorError as exc:
            if exc.status == 404:
                return Outcome("skipped", obj["id"], "Already gone")
            if isinstance(exc, (AuthError, ConfigError)):
                raise
            raise ItemFailed(str(exc), retryable=exc.retryable) from None
        return Outcome("succeeded", obj["id"], "Deleted")
