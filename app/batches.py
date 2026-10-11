"""Batches started from the app: on this computer, or on an HPC cluster (app/hpc.py).

A batch is pipeline/batch.py run on a list of fires (Unique Fire IDs or name,year[,state]).
Its record, data/batches/<name>/app.json, says where it runs and how far it got, so the app
shows a batch again after a restart (a local batch whose process ended while the app was
closed is shown from its status files; an HPC batch resumes polling and copying back).

  local  `batch.py all <list> --runner local --workers N` as a background process; progress
         from batch.py's own status files, which it writes as each fire finishes.
  hpc    a background thread: start the batch on the cluster's login node, then every
         poll_s seconds read its status files there and copy each fire that is done back here
         (exports, perimeter, terrain and severity layers, config, USGS check), until the
         cluster says the batch is over.
"""

import json
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "pipeline"))
import hpc  # noqa: E402

BATCHES = ROOT / "data" / "batches"
lock = threading.Lock()
procs = {}          # name -> Popen of a local batch started by this app process
threads = {}        # name -> polling thread of an HPC batch
on_fetched = None   # set by the server: called with a fire's key once it is copied back


def record_path(name):
    return BATCHES / name / "app.json"


def load_record(name):
    p = record_path(name)
    return json.loads(p.read_text()) if p.exists() else None


def save_record(rec):
    p = record_path(rec["name"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=1))
    tmp.replace(p)


def new_name():
    return f"app_{datetime.now():%Y%m%d-%H%M%S}"


def active(where=None):
    out = []
    for p in sorted(BATCHES.glob("*/app.json")):
        rec = json.loads(p.read_text())
        if rec["state"] in ("starting", "running") and (where is None or rec["where"] == where):
            out.append(rec["name"])
    return out


def start(lines, where, workers=3):
    """Start a batch; returns its record. Lines are list lines as batch.py reads them."""
    lines = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    if not lines:
        raise ValueError("no fires to run")
    if len(lines) > 500:
        raise ValueError("at most 500 fires per batch")
    if where not in ("local", "hpc"):
        raise ValueError("where: local or hpc")
    if where == "hpc" and not hpc.configured():
        raise ValueError("HPC is not set up: open Settings > HPC")
    with lock:
        if where == "local" and active("local"):
            raise RuntimeError("a batch is already running on this computer; wait for it to finish")
        name = new_name()
        rec = {"name": name, "where": where, "created": time.time(), "lines": lines, "state": "starting",
               "error": None, "fetched": [], "remote": None, "workers": workers,
               "hpc": hpc.public() if where == "hpc" else None,
               # configs that existed before: only fires new to this computer arrive not kept
               "preexisting": sorted(p.name for p in (ROOT / "fires").glob("*.json"))}
        (BATCHES / name).mkdir(parents=True, exist_ok=True)
        (BATCHES / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        save_record(rec)
    if where == "local":
        log = open(BATCHES / name / "app.log", "w", encoding="utf-8")
        procs[name] = subprocess.Popen(
            [sys.executable, str(ROOT / "pipeline" / "batch.py"), "all", str(BATCHES / f"{name}.txt"),
             "--name", name, "--runner", "local", "--workers", str(workers)],
            cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        rec["state"] = "running"
        save_record(rec)
        threading.Thread(target=_watch_local, args=(name, log), daemon=True).start()
    else:
        _start_hpc_thread(name, submit=True)
    return rec


def _watch_local(name, log):
    code = procs[name].wait()
    log.close()
    rec = load_record(name)
    rec["state"] = "done" if code == 0 else "failed"
    if code:
        lines = (BATCHES / name / "app.log").read_text(encoding="utf-8", errors="replace").splitlines()
        rec["error"] = next((l for l in reversed(lines) if l.strip()), f"exit {code}")[:300]
    _mark_new_fires_not_kept(rec)
    save_record(rec)


def _start_hpc_thread(name, submit):
    t = threading.Thread(target=_hpc_loop, args=(name, submit), daemon=True)
    threads[name] = t
    t.start()


def _hpc_loop(name, submit):
    rec = load_record(name)
    try:
        if submit:
            hpc.start_batch(name, rec["lines"])
            rec["state"] = "running"
            save_record(rec)
        while True:
            s = hpc.load()
            arrived = []
            with hpc.Connection(s) as c:
                if not c.exists(f"data/batches/{name}.txt"):
                    raise RuntimeError("this batch never reached the cluster; start it again")
                state = hpc.remote_state(name, conn=c)
                rec = load_record(name)
                rec["remote"] = {**state, "polled": time.time()}
                for key, st in state["status"].items():
                    if key in rec["fetched"]:
                        continue
                    if st.get("status") == "done" or st.get("status", "").startswith("skipped"):
                        config = next((f["config"] for f in state["fires"] if f.get("key") == key), None)
                        if config and hpc.fetch_fire(c, key, config):
                            rec["fetched"].append(key)
                            arrived.append(key)
                if state["finished"]:
                    rec["state"] = "done" if state["exit"] == "0" else "failed"
                    if state["exit"] != "0":
                        rec["error"] = f"the batch on the cluster ended with exit {state['exit']}; " \
                                       "see its log below"
                save_record(rec)
            # after the connection closes: a large fire's map takes minutes to build
            for key in arrived:
                if on_fetched:
                    try:
                        on_fetched(key)
                    except Exception:       # the map builds on first view instead
                        traceback.print_exc()
            if rec["state"] in ("done", "failed"):
                _mark_new_fires_not_kept(rec)
                save_record(rec)
                return
            time.sleep(max(10, int(s["poll_s"])))
    except Exception as e:          # connection lost, login refused, ...: shown in the batch
        traceback.print_exc()
        rec = load_record(name)
        rec["error"] = str(e)[:400] or type(e).__name__     # a timeout has no message
        rec["state"] = "failed" if rec["state"] == "starting" else "disconnected"
        save_record(rec)


def resume():
    """On app start: keep polling HPC batches that were still running (or lost the
    connection); mark local batches whose process is gone as finished."""
    for p in sorted(BATCHES.glob("*/app.json")):
        rec = json.loads(p.read_text())
        if rec["where"] == "hpc" and rec["state"] in ("running", "disconnected"):
            rec["state"] = "running"
            save_record(rec)
            _start_hpc_thread(rec["name"], submit=False)
        elif rec["where"] == "local" and rec["state"] in ("starting", "running") and rec["name"] not in procs:
            rec["state"] = "stopped"
            rec["error"] = "the app was closed while this batch ran; finished fires are kept"
            save_record(rec)


def reconnect(name):
    rec = load_record(name)
    if not rec or rec["where"] != "hpc":
        raise ValueError("not an HPC batch")
    if name in threads and threads[name].is_alive():
        return rec
    rec["state"], rec["error"] = "running", None
    save_record(rec)
    _start_hpc_thread(name, submit=False)
    return rec


def _mark_new_fires_not_kept(rec):
    """Fires a batch added to this computer arrive not kept, like fires added one at a time
    in the app. Configs that existed before the batch keep their state."""
    before = set(rec.get("preexisting", []))
    for p in (ROOT / "fires").glob("*.json"):
        if p.name in before:
            continue
        try:
            fire = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if "kept" not in fire:
            fire["kept"] = False
            p.write_text(json.dumps(fire, indent=1) + "\n")


def view(name):
    """A batch for the browser: per-fire rows (line, key, overall status, each phase)."""
    rec = load_record(name)
    if rec is None:
        return None
    if rec["where"] == "local":
        fires, status = _local_state(name)
    else:
        remote = rec.get("remote") or {}
        fires, status = remote.get("fires", []), remote.get("status", {})
    rows = []
    for f in fires:
        st = status.get(f.get("key"), {}) if f.get("key") else {}
        rows.append({"line": f.get("line"), "key": f.get("key"), "unique_id": f.get("unique_id"),
                     "status": "left out" if f.get("error") else st.get("status", "waiting"),
                     "error": f.get("error") or st.get("error", ""),
                     "phases": {p: v.get("status") for p, v in st.get("phases", {}).items()},
                     "seconds": st.get("seconds"),
                     "fetched": f.get("key") in rec.get("fetched", []) if rec["where"] == "hpc" else None})
    log = ""
    if rec["where"] == "local" and (BATCHES / name / "app.log").exists():
        log = "\n".join((BATCHES / name / "app.log").read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
    elif rec.get("remote"):
        log = rec["remote"].get("log_tail", "")
    return {"name": name, "where": rec["where"], "state": rec["state"], "error": rec["error"],
            "created": rec["created"], "count": len(rec["lines"]), "rows": rows, "log": log,
            "polled": (rec.get("remote") or {}).get("polled"),
            "cluster": (rec.get("hpc") or {}).get("name") or (rec.get("hpc") or {}).get("host")}


def _local_state(name):
    d = BATCHES / name
    fires = []
    if (d / "fires.tsv").exists():
        text = (d / "fires.tsv").read_text(encoding="utf-8").splitlines()
        head = text[0].split("\t")
        fires = [dict(zip(head, line.split("\t"))) for line in text[1:] if line.strip()]
    status = {}
    for p in (d / "status").glob("*.json") if (d / "status").exists() else []:
        status[p.stem] = json.loads(p.read_text())
    return fires, status


def listing(limit=20):
    out = []
    for p in sorted(BATCHES.glob("app_*/app.json"), reverse=True)[:limit]:
        v = view(p.parent.name)
        counts = {}
        for r in v["rows"]:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        out.append({k: v[k] for k in ("name", "where", "state", "error", "created", "count", "cluster")}
                   | {"counts": counts})
    return out
