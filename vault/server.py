"""Local web app: http://127.0.0.1:8765 (only reachable from this computer). Python standard library only."""
import argparse
import csv
import io
import json
import os
import re
import shutil
import socket
import socketserver
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import NAME, REPO, VERSION
from .config import (DOMAIN_BASES, PRIORITY_FOLDERS, PRIORITY_LABELS, WEB_DIR, Vault, find_vault, list_drives,
                     lock_vault, open_in_file_manager, read_json, remember_vault, replace_retry, unlock_vault,
                     write_json)
from .downloader import Manager
from .hosts import long_path
from .registry import build_catalog

lock = threading.RLock()
folders_lock = threading.RLock()   # one setup_folders at a time (a refresh can run while Later changes)
vault = None          # Vault, or None until first-run setup is done
vault_lock = None     # open engine.lock of that vault (see config.lock_vault)
server_url = None     # http://127.0.0.1:<port>/ once the server is bound
mgr = None            # download Manager for the open vault
catalog = {"datasets": [], "count": 0}
by_id = {}
state = {}            # id -> {status, path (relative to vault), bytes, files, finished, error}
refresh_info = {"running": False, "error": None, "finished": None}
warnings = []         # problems the UI shows, e.g. Dataset_Index.csv open in Excel
DL_KEYS = ("max_parallel", "segments", "speed_limit_mbps", "auto_resume", "keep_awake")


class VaultBusy(Exception):
    """Another NeuroRadVault process has this vault open; str() is its address ('' when unknown)."""


def cache_dir():
    return vault.cache if vault else Path.home() / f".{NAME.lower()}" / "cache"


# ------------------------------------------------------------------ catalog

def set_catalog(c):
    global catalog, by_id
    with lock:
        catalog = c
        by_id = {d["id"]: d for d in c["datasets"]}


def refresh_catalog(force=False, wait=False):
    """Fetch the live registry + enrichment in the background (cache 24 h unless force)."""
    with lock:
        if refresh_info["running"]:
            return
        refresh_info.update(running=True, error=None)

    def go():
        try:
            set_catalog(build_catalog(cache_dir(), force=force))
            if vault:
                setup_folders()
        except Exception as e:
            refresh_info["error"] = str(e)[:300]
        finally:
            refresh_info.update(running=False, finished=time.strftime("%H:%M"))
    t = threading.Thread(target=go, daemon=True)
    t.start()
    if wait:
        t.join()


# ------------------------------------------------------------------ vault state

def load_state():
    global state
    s = read_json(vault.state_path, {})       # a damaged state.json falls back to state.json.bak
    state = s if isinstance(s, dict) else {}


def save_state():
    with lock:
        write_json(vault.state_path, state, backup=True)


def target_dir(ds):
    st = state.get(ds["id"])
    if st and st.get("path"):
        p = vault.abspath(st["path"]) if not os.path.isabs(st["path"]) else st["path"]
        if os.path.isdir(long_path(p)):
            return p
    return vault.abspath(vault.dataset_rel(ds))


def put_file(p, data):
    """Write a generated file if it changed; False when it is locked by another program."""
    try:
        with open(p, "rb") as f:
            if f.read() == data:
                return True
    except OSError:
        pass
    tmp = p + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        replace_retry(tmp, p, tries=3)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def text(s):
    return s.replace("\n", os.linesep).encode("utf-8")


def setup_folders(strict=False):
    """Create the domain/priority folders, .url shortcuts and Dataset_Index.csv."""
    with folders_lock:
        notes, failed, datasets = [], 0, catalog["datasets"]
        root = str(vault.root)
        for cat, cdir in vault.folders.items():
            target = os.path.join(root, cdir)
            pat = re.compile(r"^\d\d_(LATER_)?" + re.escape(DOMAIN_BASES[cat]) + "$")
            for fn in os.listdir(root):
                old = os.path.join(root, fn)
                if fn != cdir and pat.match(fn) and os.path.isdir(old) and not os.path.exists(target):
                    try:
                        os.rename(old, target)
                    except OSError as e:
                        if strict:
                            raise
                        notes.append(f"Folder {fn} could not be renamed to {cdir} ({e.strerror or e}); "
                                     "close any file open in it and restart the app.")
                        continue
                    with lock:
                        for st in state.values():
                            if st.get("path", "").startswith(fn + "/"):
                                st["path"] = cdir + st["path"][len(fn):]
                    save_state()
        expected = {}     # both keys via vault.abspath, so a priority folder is one key on every OS
        for cat, cdir in vault.folders.items():
            for p, pdir in PRIORITY_FOLDERS.items():
                d = vault.abspath(cdir + "/" + pdir)
                expected[d] = set()
                try:
                    os.makedirs(d, exist_ok=True)
                except OSError:
                    failed += 1
                    continue
                failed += not put_file(os.path.join(d, "_ABOUT.txt"), text(
                    f"{cat} / Priority {p}: {PRIORITY_LABELS[p]}\n"
                    + ("This whole domain is set to LATER (lower priority).\n" if "LATER_" in cdir else "")
                    + "\nEach .url file opens the dataset's official page.\n"
                    + ("Datasets downloaded with the app land in a folder with the same name.\n" if p <= 2 else
                       "These need a manual step: follow the link, then save the data in a folder here.\n")))
        for ds in datasets:
            d = vault.abspath(vault.dataset_rel(ds).rsplit("/", 1)[0])
            name = ds["folder"] + ".url"
            expected.setdefault(d, set()).add(name)
            link = ds.get("link")
            if not link and ds["fields"].get("doi", "").startswith("10."):
                link = "https://doi.org/" + ds["fields"]["doi"].split()[0].rstrip(",;")
            failed += not put_file(os.path.join(d, name),
                                   text("[InternetShortcut]\nURL=" + (link or f"https://github.com/{REPO}") + "\n"))
        for d, names in expected.items():
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    if fn.endswith(".url") and fn not in names:
                        try:
                            os.remove(os.path.join(d, fn))
                        except OSError:
                            pass
        buf = io.StringIO(newline="")
        w = csv.writer(buf)
        w.writerow(["priority", "priority_meaning", "domain", "dataset", "one_click", "host", "size_GB", "masks",
                    "modality", "subjects", "access_type", "official_link", "local_folder"])
        for ds in sorted(datasets, key=lambda d: (d["priority"], d["category"], d["name"])):
            w.writerow([f"P{ds['priority']}", PRIORITY_LABELS[ds["priority"]], ds["category"], ds["name"],
                        "yes" if ds["auto"] else "no", (ds["downloader"] or {}).get("type", ""),
                        f"{ds['size_bytes'] / 1e9:.2f}" if ds.get("size_bytes") else "", "yes" if ds["masks"] else "no",
                        ds["modality"], ds["subjects"], ds["access_type"], ds.get("link") or "", vault.dataset_rel(ds)])
        if not put_file(os.path.join(root, "Dataset_Index.csv"), buf.getvalue().encode("utf-8-sig")):
            notes.append("Dataset_Index.csv was not updated - it is open in another program (e.g. Excel). "
                         "Close it and it is updated at the next start or catalogue check.")
        readme = os.path.join(root, f"README_{NAME}.txt")
        if not os.path.exists(readme):
            failed += not put_file(readme, text(
                f"This folder is a {NAME} vault: neuroradiology datasets organised by domain and priority.\n\n"
                "01_..., 02_...   one folder per domain (NN_LATER_... = domains you set to Later)\n"
                "  P1_open_with_masks / P2_open_no_masks   one-click downloads land here\n"
                "  P3_manual_download / P4_restricted_access   .url links to the access pages\n"
                "Dataset_Index.csv   every dataset with its size, link and folder\n"
                ".vault\\            the app's own bookkeeping (download progress, queue, settings)\n\n"
                f"App: https://github.com/{REPO}\n"
                "Inspired by NeuroAIHub: https://github.com/NeuroAIHub-Registry/NeuroAIHub\n"))
        if failed:
            notes.append(f"{failed} folder note or .url link file(s) could not be written (open in another program?).")
        warnings[:] = notes


def set_later(later):
    """Rename domain folders for a new Later list; returns an error message on failure."""
    with folders_lock:
        old = vault.settings["later_domains"]
        vault.settings["later_domains"] = later
        vault.apply_domain_order()
        try:
            setup_folders(strict=True)
            return None
        except OSError as e:
            vault.settings["later_domains"] = old
            vault.apply_domain_order()
            setup_folders()
            return (f"Could not move the folder {os.path.basename(e.filename or '') or 'of a domain'} "
                    f"({e.strerror or e}). Close any file that is open in it and try again.")


def migrate_legacy():
    """Adopt a vault made by the first local version (<root>/_app/state.json, absolute paths, old ids)."""
    old = vault.root / "_app"
    if vault.state_path.exists() or not (old / "state.json").exists():
        return
    try:
        old_state = json.load(open(old / "state.json", encoding="utf-8"))
        old_cat = {d["id"]: d for d in json.load(open(old / "catalog.json", encoding="utf-8"))["datasets"]}
        old_settings = json.load(open(old / "settings.json", encoding="utf-8")) if (old / "settings.json").exists() else {}
    except Exception:
        return
    for k in DL_KEYS + ("later_domains",):
        if k in old_settings:
            vault.settings[k] = old_settings[k]
    vault.save_settings()
    vault.apply_domain_order()
    by_name = {(d["name"], d["fields"].get("doi", "")): d["id"] for d in catalog["datasets"]}
    for oid, st in old_state.items():
        od = old_cat.get(oid)
        nid = od and by_name.get((od["name"], od["fields"].get("doi", "")))
        if not nid:
            continue
        p = st.get("path", "").replace("\\", "/")
        m = re.search(r"(\d\d_[^/]+/P\d_[^/]+/[^/]+)$", p)
        rel = m.group(1) if m else None
        current = vault.dataset_rel(by_id[nid])
        if (not rel or not os.path.isdir(vault.abspath(rel))) and os.path.isdir(vault.abspath(current)):
            rel = current        # folder was renamed (e.g. Later) while the drive had another letter
        st["path"] = rel
        state[nid] = st
    try:
        q = json.load(open(old / "queue.json", encoding="utf-8"))
        ids = {oid: by_name.get((old_cat[oid]["name"], old_cat[oid]["fields"].get("doi", ""))) for oid in old_cat}
        write_json(vault.queue_path, [{**it, "id": ids[it["id"]]} for it in q if ids.get(it["id"])], backup=True)
    except Exception:
        pass
    save_state()


def running_url(v):
    """Address another NeuroRadVault window wrote for vault v (only a local http address is trusted)."""
    try:
        u = (v.meta / "engine.url").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return u if re.fullmatch(r"http://127\.0\.0\.1:\d{1,5}/", u) else ""


def open_vault(path):
    """Open (or switch to) the vault at path. Raises VaultBusy when another NeuroRadVault process has it."""
    global vault, mgr, vault_lock
    v = Vault(path)
    v.root.mkdir(parents=True, exist_ok=True)
    v.create()
    lk = vault_lock if vault and vault.root == v.root else lock_vault(v.meta)
    if not lk:
        raise VaultBusy(running_url(v))
    if mgr:
        mgr.close()
    if lk is not vault_lock:
        unlock_vault(vault_lock)
    vault, vault_lock = v, lk
    try:
        if server_url:
            (v.meta / "engine.url").write_text(server_url, encoding="utf-8")
    except OSError:
        pass
    try:
        load_state()
        migrate_legacy()
        setup_folders()
        # late-binding lookup: set_catalog replaces by_id on every refresh
        mgr = Manager(vault, lambda i: by_id.get(i), target_dir, state, save_state, vault.settings)
    except BaseException:
        unlock_vault(vault_lock)        # back to the setup screen rather than a half-open vault
        vault = mgr = vault_lock = None
        raise
    try:
        remember_vault(v.root)
    except OSError:
        pass


def team():
    """Contributors for the About page; a <id>.jpg/.png/.webp photo in web/team/ replaces the placeholder."""
    try:
        members = json.loads((WEB_DIR / "team.json").read_text(encoding="utf-8"))["members"]
    except Exception:
        return {"members": []}
    for m in members:
        for ext in ("jpg", "jpeg", "png", "webp"):
            if (WEB_DIR / "team" / f"{m['id']}.{ext}").is_file():
                m["photo"] = f"team/{m['id']}.{ext}"
                break
    return {"members": members}


def storage():
    if not vault:
        return None
    u = shutil.disk_usage(vault.root)
    with lock:
        done = [s for s in state.values() if s.get("status") == "done"]
        on_drive = sum(s.get("bytes") or 0 for s in done) + sum(
            s.get("done_bytes") or 0 for s in state.values() if s.get("status") != "done")
    snap = mgr.snapshot() if mgr else {"remaining_bytes": 0}
    return {"total": u.total, "used": u.used, "free": u.free, "vault_bytes": on_drive, "datasets": len(done),
            "queued_bytes": snap["remaining_bytes"], "free_after_queue": u.free - snap["remaining_bytes"]}


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        self.sent = True
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def answer(self, fn, *args):
        """Run a request handler so that every request gets an answer (no dropped connections)."""
        self.sent = False
        try:
            fn(*args)
        except ConnectionError:
            pass                        # the browser went away
        except Exception as e:
            traceback.print_exc()
            if not self.sent:
                self.send(500, {"error": f"{type(e).__name__}: {e}"[:400]})

    def do_GET(self):
        self.answer(self.get, self.path.split("?")[0])

    def do_POST(self):
        # a custom header forces a CORS preflight, so other sites cannot post here
        if self.headers.get("X-App") != NAME:
            return self.send(403, {"error": "missing header"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self.send(400, {"error": "The request must be a JSON object."})
        self.answer(self.post, self.path.split("?")[0], body)

    def get(self, path):
        if path in ("/", "/index.html"):
            return self.send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/favicon.svg":
            return self.send(200, (WEB_DIR / "favicon.svg").read_bytes(), "image/svg+xml")
        m = re.fullmatch(r"/fonts/([A-Za-z0-9-]+\.woff2)", path)
        if m and (WEB_DIR / "fonts" / m.group(1)).is_file():
            return self.send(200, (WEB_DIR / "fonts" / m.group(1)).read_bytes(), "font/woff2")
        if path == "/api/team":
            return self.send(200, team())
        m = re.fullmatch(r"/(team|icons)/([a-z0-9-]+\.(svg|jpg|jpeg|png|webp))", path)
        if m and (WEB_DIR / m.group(1) / m.group(2)).is_file():
            ctype = {"svg": "image/svg+xml", "png": "image/png", "webp": "image/webp"}.get(m.group(3), "image/jpeg")
            return self.send(200, (WEB_DIR / m.group(1) / m.group(2)).read_bytes(), ctype)
        if path == "/api/catalog":
            with lock:
                return self.send(200, catalog)
        if path == "/api/status":
            if not vault:
                return self.send(200, {"setup": True, "drives": list_drives(), "name": NAME, "version": VERSION,
                                       "home": str(Path.home())})
            with lock:
                local = {k: {kk: v.get(kk) for kk in ("status", "bytes", "done_bytes", "finished", "error")}
                         for k, v in state.items()}
            m = mgr
            return self.send(200, {
                "name": NAME, "version": VERSION, "repo": REPO, "root": str(vault.root), "storage": storage(),
                "dl": m.snapshot() if m else {"order": [], "jobs": {}, "total_speed": 0, "remaining_bytes": 0, "running": 0},
                "local": local, "refresh": refresh_info, "built": catalog.get("built"), "warnings": list(warnings),
                "registry_source": catalog.get("registry_source"), "enrichment_generated": catalog.get("enrichment_generated"),
                "settings": vault.settings, "folders": vault.folders})
        if path == "/api/drives":
            return self.send(200, {"drives": list_drives()})
        self.send(404, {"error": "not found"})

    def post(self, path, body):
        if path == "/api/setup":
            p = os.path.expanduser(str(body.get("path", "")).strip())
            if not p or not os.path.isabs(p):
                return self.send(400, {"error": "Please give a full folder path, e.g. D:\\NeuroData"})
            if mgr and mgr.snapshot()["running"]:
                return self.send(409, {"error": "Pause the running downloads before switching vaults."})
            try:
                open_vault(p)
            except VaultBusy as e:
                return self.send(409, {"error": f"This vault is already open in another {NAME} window"
                                                + (f" ({e})" if str(e) else "")})
            except OSError as e:
                return self.send(400, {"error": f"Could not open the folder: {e}"})
            return self.send(200, {"ok": True, "root": str(vault.root)})
        if not vault:
            return self.send(409, {"error": "Choose a folder for your vault first."})
        ids = body.get("ids", [])
        if path == "/api/download":
            return self.send(200, {"queued": mgr.enqueue(ids, verify=bool(body.get("verify")))})
        if path == "/api/pause":
            mgr.pause(ids)
        elif path == "/api/resume":
            mgr.resume(ids)
        elif path == "/api/remove":
            mgr.remove(ids)
        elif path == "/api/move":
            mgr.move(body.get("id"), body.get("where", "up"))
        elif path == "/api/pause_all":
            mgr.pause_all()
        elif path == "/api/resume_all":
            mgr.resume_all()
        elif path == "/api/open":
            ds = by_id.get(body.get("id"))
            target = str(vault.root)
            if ds:
                d = target_dir(ds)
                target = d if os.path.isdir(long_path(d)) else os.path.dirname(d)
            open_in_file_manager(target)
        elif path == "/api/settings":
            for k in DL_KEYS:
                if k in body:
                    vault.settings[k] = body[k]
            err = None
            if "later_domains" in body:
                later = [c for c in body["later_domains"] if c in DOMAIN_BASES]
                if sorted(later) != sorted(vault.settings["later_domains"]):
                    if mgr.snapshot()["running"]:
                        return self.send(409, {"error": "Pause running downloads before moving domain folders."})
                    err = set_later(later)       # on failure the folders and the list stay as they were
            vault.save_settings()
            mgr.apply_settings()
            if err:
                return self.send(409, {"error": err})
        elif path == "/api/refresh":
            refresh_catalog(force=True)
        else:
            return self.send(404, {"error": "not found"})
        self.send(200, {"ok": True})


class Server(ThreadingHTTPServer):
    # Windows: SO_REUSEADDR would let two servers share a port
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # skip HTTPServer's getfqdn(), which can hang on macOS
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def main(argv=None):
    global server_url
    ap = argparse.ArgumentParser(prog=NAME, description="One click away from downloading your desired neuroradiology dataset.")
    ap.add_argument("--vault", help="folder that holds your datasets (asked on first run)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("NRV_PORT", "8765")))
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    for port in range(args.port, args.port + 20):   # only picks a free port; the vault lock stops a second engine
        try:
            srv = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            print(f"Port {port} is in use, trying {port + 1}")
    else:
        print(f"No free port between {args.port} and {args.port + 19}; start with --port <number>.")
        return 1
    url = server_url = f"http://127.0.0.1:{srv.server_address[1]}/"
    found = find_vault(args.vault)
    # start from the local cache; GitHub is checked in the background
    set_catalog(build_catalog(Vault(found).cache if found else cache_dir(), offline=True))
    if found:
        try:
            open_vault(found)
        except VaultBusy as e:
            other = str(e)
            print(f"This vault ({Path(found).resolve()}) is already open in another {NAME} window"
                  + (f": {other}" if other else "."))
            if other and not args.no_browser:
                webbrowser.open(other)
            srv.server_close()
            return 0
        except Exception as e:
            print(f"Could not open the vault {found}: {e}")
    refresh_catalog()                                         # then check GitHub in the background
    print(f"{NAME} {VERSION} running at {url}")
    print(f"Vault: {vault.root}" if vault else "First run: choose where to keep your datasets in the browser.")
    print("Close this window to stop. Unfinished downloads continue automatically next time.")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
