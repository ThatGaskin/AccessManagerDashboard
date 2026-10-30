"""Side-by-side comparison of one object type in two environments (read-only).

Lists every object of the type in both connections, matches them by name and compares their copyable
attributes as people read them — owners, groups, entitlements and access profiles by name, since IDs differ
between tenants. The result is a starting point: the chosen objects and attributes become a normal
"Copy objects between environments" job, whose plan does the exact, ID-level translation and shows the
precise before → after for review. Results are kept in memory only.
"""
import logging
import threading
from collections import OrderedDict

from .actions.copy import COPY_TYPES, attr_specs, find_by_name
from .connectors import model
from .connectors.base import ConnectorError, Interrupted
from .reports import MAX_PER_TYPE
from .util import new_id, now_iso

log = logging.getLogger(__name__)
KEEP = 10  # comparisons kept in memory
NAMES_SHOWN = 200  # entitlement names listed per source and side; the counts are always complete


def comparable(spec, obj, group_names, members=None):
    """An attribute's value in a form that can be compared across environments."""
    value = obj.get(spec.name) if not spec.name.startswith("config.") else (obj.get("config") or {}).get(spec.label)
    if spec.secret:
        return None
    if spec.kind == "identity":
        return obj.get("owner_name") or obj.get("owner")
    if spec.kind == "group":
        return obj.get("management_workgroup_name") or obj.get("management_workgroup")
    if spec.kind == "cluster":
        return obj.get("cluster_name") or obj.get("cluster")
    if spec.kind == "entitlements":
        return sorted((obj.get("entitlement_names") or {}).get(i, i) for i in value or [])
    if spec.kind == "access_profiles":
        return sorted((obj.get("access_profile_names") or {}).get(i, i) for i in value or [])
    if spec.kind == "approvers":
        return [f"group {group_names.get(t.split(':', 1)[1], t.split(':', 1)[1])}" if t.startswith("governance_group:")
                else t for t in value or []]
    if spec.kind == "members":
        return sorted(members or [])
    if spec.kind == "text":
        return value or ""
    return value


def _same(a, b):
    if isinstance(a, list) and isinstance(b, list):
        return a == b
    return model.same(a, b)


class CompareRunner:
    def __init__(self, connections):
        self.connections = connections
        self._results = OrderedDict()
        self._aborts = {}
        self._lock = threading.Lock()

    def start(self, source_id, target_id, type_):
        if type_ not in COPY_TYPES:
            raise ValueError("Choose an object type: " + ", ".join(COPY_TYPES))
        if not source_id or not target_id:
            raise ValueError("Choose both connections")
        if source_id == target_id:
            raise ValueError("Choose two different connections")
        store = self.connections.store
        source, target = store.get_connection(source_id), store.get_connection(target_id)
        compare_id = new_id("cmp")
        result = {"id": compare_id, "type": type_, "type_label": model.TYPE_LABELS[type_], "status": "running",
                  "scanned": 0, "error": None, "created_at": now_iso(), "finished_at": None, "rows": [],
                  "only_in_target": [],
                  "source": {k: source[k] for k in ("id", "name", "environment")},
                  "target": {k: target[k] for k in ("id", "name", "environment")}}
        abort = threading.Event()
        with self._lock:
            self._results[compare_id] = result
            self._aborts[compare_id] = abort
            while len(self._results) > KEEP:
                old, _ = self._results.popitem(last=False)
                self._aborts.pop(old, None)
        threading.Thread(target=self._run, args=(result, abort), daemon=True, name=f"compare-{compare_id}").start()
        return compare_id

    def get(self, compare_id):
        with self._lock:
            result = self._results.get(compare_id)
            if result is None:
                from .store import NotFound
                raise NotFound("Comparison not found (comparisons are kept in memory until the toolkit restarts)")
            return dict(result)

    def cancel(self, compare_id):
        with self._lock:
            abort = self._aborts.get(compare_id)
        if abort:
            abort.set()

    def _list(self, connector, type_, result, abort):
        out = {}
        for obj in connector.list_objects(type_, limit=MAX_PER_TYPE, abort_event=abort):
            if abort.is_set():
                raise Interrupted()
            out.setdefault((obj.get("name") or "").lower(), []).append(obj)
            result["scanned"] += 1
        return out

    def _run(self, result, abort):
        type_ = result["type"]
        try:
            src = self.connections.build(result["source"]["id"], abort_event=abort)
            dst = self.connections.build(result["target"]["id"], abort_event=abort)
            src_objs = self._list(src, type_, result, abort)
            dst_objs = self._list(dst, type_, result, abort)
            src_groups, dst_groups = {}, {}
            if type_ == "access_profile":
                src_groups = {g["id"]: g["name"] for g in src.list_objects("governance_group", abort_event=abort)}
                dst_groups = {g["id"]: g["name"] for g in dst.list_objects("governance_group", abort_event=abort)}
            rows = []
            for key, objs in sorted(src_objs.items()):
                rows.append(self._row(src, dst, type_, objs, dst_objs.get(key, []), src_groups, dst_groups, abort))
            result["only_in_target"] = sorted(objs[0]["name"] for key, objs in dst_objs.items() if key not in src_objs)
            if type_ == "source":
                self._entitlements(src, dst, rows, result, abort)
            result["rows"] = rows
            result["summary"] = self._summary(rows, result, len(src_objs), len(dst_objs))
            result["status"] = "completed"
        except Interrupted:
            result["status"] = "cancelled"
        except (ConnectorError, ValueError) as exc:
            result.update(status="failed", error=str(exc))
        except Exception as exc:
            log.exception("Comparison %s crashed", result["id"])
            result.update(status="failed", error=f"Unexpected error: {exc}")
        finally:
            result["finished_at"] = now_iso()
            with self._lock:
                self._aborts.pop(result["id"], None)

    def _catalog(self, connector, result, abort):
        """Entitlement names per source (lower-cased source name -> {lower-cased name: name})."""
        out = {}
        count = 0
        for ent in connector.list_objects("entitlement", limit=MAX_PER_TYPE, abort_event=abort):
            if abort.is_set():
                raise Interrupted()
            name = ent.get("name") or ent.get("value") or ent["id"]
            out.setdefault((ent.get("source_name") or "").lower(), {})[name.lower()] = name
            count += 1
            result["scanned"] += 1
        return out, count >= MAX_PER_TYPE

    def _entitlements(self, src, dst, rows, result, abort):
        """Adds each source's entitlement catalog comparison. Entitlements are not copied — they come from
        aggregating the source — but a missing one breaks access profiles that use it."""
        src_cat, src_cut = self._catalog(src, result, abort)
        dst_cat, dst_cut = self._catalog(dst, result, abort)
        result["entitlements_incomplete"] = src_cut or dst_cut
        for row in rows:
            a = src_cat.get(row["name"].lower(), {})
            b = dst_cat.get(row["name"].lower(), {}) if row["target_id"] else None
            ents = {"source_count": len(a), "target_count": None if b is None else len(b),
                    "only_in_source": [], "only_in_target": []}
            if b is not None:
                only_a = sorted((a[k] for k in a.keys() - b.keys()), key=str.lower)
                only_b = sorted((b[k] for k in b.keys() - a.keys()), key=str.lower)
                ents.update(only_in_source=only_a[:NAMES_SHOWN], only_in_target=only_b[:NAMES_SHOWN],
                            only_in_source_count=len(only_a), only_in_target_count=len(only_b))
            row["entitlements"] = ents
            row["entitlements_differ"] = bool(b is not None and (ents["only_in_source"] or ents["only_in_target"]))

    @staticmethod
    def _summary(rows, result, in_source, in_target):
        count = lambda status: sum(1 for r in rows if r["status"] == status)  # noqa: E731
        return {"in_source": in_source, "in_target": in_target, "new": count("new"), "different": count("different"),
                "same": count("same"), "ambiguous": count("ambiguous"), "only_in_target": len(result["only_in_target"]),
                "entitlements_differ": sum(1 for r in rows if r.get("entitlements_differ"))}

    def _row(self, src, dst, type_, src_objs, dst_objs, src_groups, dst_groups, abort):
        if len(src_objs) > 1 or len(dst_objs) > 1:
            s = src_objs[0]
            return {"name": s["name"], "source_id": s["id"], "target_id": None, "status": "ambiguous", "diffs": [],
                    "secrets": [], "problem": "The name is used by more than one object; copy it by ID"}
        if abort.is_set():
            raise Interrupted()
        return object_row(src, dst, type_, src_objs[0], dst_objs[0] if dst_objs else None, src_groups, dst_groups)


def object_row(src, dst, type_, s, d, src_groups, dst_groups, include_same=False):
    """One object compared across two environments. Lists the differing attributes, or with include_same
    every copyable attribute, each with "same" telling whether the values already match."""
    row = {"name": s["name"], "source_id": s["id"], "target_id": None, "status": "new", "diffs": [], "secrets": []}
    specs = attr_specs(type_, s)
    if d is not None:
        row["target_id"] = d["id"]
        for key in (d.get("config") or {}):
            specs.setdefault(f"config.{key}", attr_specs(type_, d)[f"config.{key}"])
    src_members = dst_members = None
    if type_ == "governance_group" and (d is not None or include_same):
        src_members = [m.get("name") or m["id"] for m in src.list_governance_group_members(s["id"])]
        if d is not None:
            dst_members = [m.get("name") or m["id"] for m in dst.list_governance_group_members(d["id"])]
    for spec in specs.values():
        a = comparable(spec, s, src_groups, src_members)
        b = comparable(spec, d, dst_groups, dst_members) if d is not None else None
        if spec.secret:
            row["secrets"].append(spec.label)
            continue
        if spec.kind == "members" and src_members is None:
            continue
        equal = d is not None and _same(a, b)
        if equal and not include_same:
            continue
        row["diffs"].append({"attr": spec.name, "label": spec.label, "kind": spec.kind, "source": a, "target": b,
                             "same": equal, "env_specific": spec.env_specific, "required": spec.required,
                             "only_in_target": spec.name.startswith("config.") and
                             spec.label not in (s.get("config") or {})})
    if d is not None:
        row["status"] = "different" if any(not x["same"] for x in row["diffs"]) else "same"
    # When each side was last changed, and which one more recently: a target changed after the source may
    # hold a fix that moving the source's values would overwrite.
    row["source_modified"] = s.get("modified")
    row["target_modified"] = d.get("modified") if d is not None else None
    row["newer"] = {"first": "source", "second": "target", "same": "same"}.get(
        model.changed_later(row["source_modified"], row["target_modified"]))
    return row


def group_names(connector, objs):
    """Names of the governance groups the objects' approvers refer to, looked up one by one."""
    names = {}
    for obj in objs:
        for token in (obj or {}).get("approval_schemes") or []:
            if token.startswith("governance_group:"):
                gid = token.split(":", 1)[1]
                if gid not in names:
                    found = connector.find_governance_groups(gid)
                    names[gid] = found[0]["name"] if found else gid
    return names


def compare_one(src, dst, type_, ref):
    """The attribute table for one object of the source environment and its namesake in the target."""
    if type_ not in COPY_TYPES:
        raise ValueError("Choose an object type: " + ", ".join(COPY_TYPES))
    found = [o for o in src.find_objects(type_, ref)
             if o["id"].lower() == ref.lower() or (o.get("name") or "").lower() == ref.lower()]
    if len(found) != 1:
        raise ValueError(f"{model.TYPE_LABELS[type_]} '{ref}' " +
                         ("was not found" if not found else f"matches {len(found)} objects; choose it by ID"))
    s = found[0]
    matches = find_by_name(dst, type_, s["name"])
    if len(matches) > 1:
        raise ValueError(f"'{s['name']}' matches {len(matches)} objects in the target; names must be unique")
    d = dst.get_object(type_, matches[0]["id"]) if matches else None
    return object_row(src, dst, type_, s, d, group_names(src, [s]), group_names(dst, [d]), include_same=True)
