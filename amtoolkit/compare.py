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

from .actions.copy import COPY_TYPES, attr_specs
from .connectors import model
from .connectors.base import ConnectorError, Interrupted
from .reports import MAX_PER_TYPE
from .util import new_id, now_iso

log = logging.getLogger(__name__)
KEEP = 10  # comparisons kept in memory


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
            result["rows"] = rows
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

    def _row(self, src, dst, type_, src_objs, dst_objs, src_groups, dst_groups, abort):
        s = src_objs[0]
        row = {"name": s["name"], "source_id": s["id"], "target_id": None, "status": "new", "diffs": [], "secrets": []}
        if len(src_objs) > 1 or len(dst_objs) > 1:
            row.update(status="ambiguous", problem="The name is used by more than one object; copy it by ID")
            return row
        d = dst_objs[0] if dst_objs else None
        specs = attr_specs(type_, s)
        if d is not None:
            row["target_id"] = d["id"]
            for key in (d.get("config") or {}):
                specs.setdefault(f"config.{key}", attr_specs(type_, d)[f"config.{key}"])
        src_members = dst_members = None
        if type_ == "governance_group" and d is not None:
            if abort.is_set():
                raise Interrupted()
            src_members = [m.get("name") or m["id"] for m in src.list_governance_group_members(s["id"])]
            dst_members = [m.get("name") or m["id"] for m in dst.list_governance_group_members(d["id"])]
        for spec in specs.values():
            a = comparable(spec, s, src_groups, src_members)
            b = comparable(spec, d, dst_groups, dst_members) if d is not None else None
            if spec.secret:
                row["secrets"].append(spec.label)
                continue
            if (d is not None and _same(a, b)) or (spec.kind == "members" and src_members is None):
                continue
            row["diffs"].append({"attr": spec.name, "label": spec.label, "source": a, "target": b,
                                 "env_specific": spec.env_specific,
                                 "only_in_target": spec.name.startswith("config.") and
                                 spec.label not in (s.get("config") or {})})
        if d is not None:
            row["status"] = "different" if row["diffs"] else "same"
        return row
