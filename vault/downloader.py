"""Download queue: resumable segmented downloads, retries and checksum checks."""
import concurrent.futures as cf
import ctypes
import errno
import hashlib
import json
import logging
import os
import posixpath
import re
import threading
import time
import urllib.error
import zipfile

from .config import read_json, replace_retry, write_json
from .hosts import drop_conn, long_path, open_stream, resolve_manifest

CHUNK = 1 << 20
SEGMENT_MIN = 64 * 2**20          # files at least this big are split into segments
SYNC_BYTES = 16 * 2**20           # a segment's saved position only advances once its data is fsynced,
SYNC_SECS = 2                     # every 16 MB or 2 s
FREE_MARGIN = 20 * 10**9          # always keep 20 GB free
MANIFEST_MAX_AGE = 14 * 86400
MANIFEST_VERSION = 2              # cached file lists in another format are listed again
PERMANENT_HTTP = (401, 403, 404, 410)
CONNECTIONS = {"openneuro": 12, "tcia": 3}  # files downloaded at once per dataset; others default 4
RUNNING = ("resolving", "downloading", "verifying")

log = logging.getLogger("vault.downloads")
log.setLevel(logging.INFO)


def attach_log(path):
    for h in list(log.handlers):
        log.removeHandler(h)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h = logging.FileHandler(path, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)


class Paused(Exception):
    pass


class PermanentError(Exception):
    pass


class TryAgain(PermanentError):
    """Retried once from scratch; the second time the file fails with `final`."""

    def __init__(self, msg, kind, final=None):
        super().__init__(msg)
        self.kind, self.final = kind, final or msg


class SizeMismatch(TryAgain):
    def __init__(self, host, listed):
        what = f"host has {host} bytes" if host is not None else "host refused the byte range (HTTP 416)"
        super().__init__(f"{what}, listing said {listed}; use Verify & repair to re-list", "size")


class DiskFull(Exception):
    pass


def is_disk_full(e):
    return isinstance(e, OSError) and (e.errno == errno.ENOSPC or getattr(e, "winerror", None) in (112, 39))


def bad_name(e):
    """A local name this drive cannot store (invalid, too long): retrying will not help."""
    return isinstance(e, OSError) and not isinstance(e, urllib.error.URLError) and bool(e.filename) and (
        e.errno in (errno.EINVAL, errno.ENAMETOOLONG) or getattr(e, "winerror", None) in (123, 206))


# ------------------------------------------------------------------ host paths and sizes

_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED = re.compile(r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])\s*(\..*)?", re.I)


def safe_relpath(p):
    """Make a host file path safe to use on every OS."""
    out = []
    for c in re.split(r"[/\\]", str(p)):
        if c in ("", ".", ".."):
            continue
        c = _BAD_CHARS.sub("_", c).rstrip(" .")
        if _RESERVED.fullmatch(c):
            c = "_" + c
        out.append(c or "_")
    return "/".join(out) or "_"


def _numbered(path, n, keep_ext=True):
    """'a/b.nii.gz' -> 'a/b_2.nii.gz'."""
    d, _, name = path.rpartition("/")
    stem, ext = posixpath.splitext(name) if keep_ext else (name, "")
    if ext.lower() in (".gz", ".bz2", ".xz", ".zst"):
        stem, ext2 = posixpath.splitext(stem)
        ext = ext2 + ext
    return (d + "/" if d else "") + f"{stem}_{n}{ext}"


def prepare(files, warn=True):
    """Sanitise every host path once, and make the paths unique ignoring case (Windows, macOS)."""
    seen = set()
    for f in files:
        p = q = safe_relpath(f["path"])
        n = 2
        while q.lower() in seen:
            q = _numbered(p, n, f.get("kind") != "tcia")
            n += 1
        if q != p and warn:
            log.warning("two files share the name %s; the second is saved as %s", p, q)
        seen.add(q.lower())
        f["path"] = q
    return files


def file_target(dest, f):
    """The file's local path; refuses anything that would land outside the dataset folder."""
    t = os.path.join(dest, *f["path"].split("/"))
    d, a = os.path.abspath(dest), os.path.abspath(t)
    try:
        inside = a != d and os.path.commonpath([d, a]) == d
    except ValueError:            # another drive
        inside = False
    if not inside:
        raise PermanentError("unsafe file name from the host")
    return t


def _alloc(p):
    """Bytes a file takes on the drive (a sparse file on Linux/macOS only counts its written blocks)."""
    try:
        st = os.stat(p)
    except OSError:
        return 0
    blocks = getattr(st, "st_blocks", None)
    return min(st.st_size, blocks * 512) if blocks is not None else st.st_size


def _host_size(headers, status):
    """The file size the host reports: the total in Content-Range (206, 416), else a 200's Content-Length."""
    headers = headers or {}
    m = re.search(r"/\s*(\d+)\s*$", headers.get("Content-Range") or "")
    if m:
        return int(m.group(1))
    n = (headers.get("Content-Length") or "").strip()
    return int(n) if status == 200 and n.isdigit() else None


def _range_start(headers):
    m = re.match(r"\s*bytes\s+(\d+)-", headers.get("Content-Range") or "")
    return int(m.group(1)) if m else None


def _hashed(f):
    h = f.get("hash")
    return bool(h) and h[0] in ("md5", "sha256")


def _load_segments(meta, size):
    """Segments from a .part.json written for this size, else None (missing, damaged or another size)."""
    try:
        with open(meta, encoding="utf-8") as fh:
            m = json.load(fh)
        segs = [[int(a), int(b), int(p)] for a, b, p in m["segments"]]
        ok = m.get("size") == size and segs and segs[0][0] == 0 and segs[-1][1] == size and all(
            a <= p <= b for a, b, p in segs) and all(segs[k][1] == segs[k + 1][0] for k in range(len(segs) - 1))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return segs if ok else None


def _files_ok(files):
    return isinstance(files, list) and bool(files) and all(
        isinstance(f, dict) and {"url", "path", "size", "kind"} <= f.keys() for f in files)


# ------------------------------------------------------------------ speed limiter

class Throttle:
    def __init__(self):
        self.rate = 0  # bytes/s, 0 = unlimited
        self.tokens = 0.0
        self.t = time.time()
        self.lock = threading.Lock()

    def set(self, rate):
        with self.lock:
            self.rate = max(0, int(rate))
            self.tokens = 0.0
            self.t = time.time()

    def consume(self, n):
        while True:
            with self.lock:
                if not self.rate:
                    return
                now = time.time()
                self.tokens = min(self.rate, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= n or self.tokens >= self.rate * 0.5:
                    self.tokens -= n
                    return
                wait = (min(n, self.rate * 0.5) - self.tokens) / self.rate
            time.sleep(min(wait, 0.5))


# ------------------------------------------------------------------ keep awake (Windows)

def keep_awake(on):
    if os.name != "nt":
        return
    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))
    except Exception:
        pass


# ------------------------------------------------------------------ job progress

class Job:
    def __init__(self, ds_id, verify=False):
        self.id = ds_id
        self.status = "queued"       # queued, resolving, downloading, verifying, paused, done, error
        self.verify = verify          # True = re-list files from the host and repair
        self.note = ""
        self.error = None
        self.total_bytes = 0
        self.done_bytes = 0
        self.disk_bytes = 0           # bytes already on the drive: finished files + allocated .part files
        self.total_files = 0
        self.done_files = 0
        self.failed = []
        self.connections = 0
        self.speed = 0.0
        self.win = []
        self.started = None
        self.finished = None
        self.resume_after = False     # Resume clicked while it was still pausing
        self.auto_paused = False      # paused only because auto_resume is off: queue.json keeps it queued
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def add(self, n, file_done=False):
        with self.lock:
            self.done_bytes += n
            if file_done:
                self.done_files += 1
            now = time.time()
            self.win.append((now, self.done_bytes))
            while len(self.win) > 2 and now - self.win[0][0] > 10:
                self.win.pop(0)

    def disk(self, n):
        with self.lock:
            self.disk_bytes = max(0, self.disk_bytes + n)

    def conn(self, d):
        with self.lock:
            self.connections += d

    def snapshot(self):
        with self.lock:
            now = time.time()
            if self.win and now - self.win[-1][0] > 3:
                self.win.append((now, self.done_bytes))
            while len(self.win) > 2 and now - self.win[0][0] > 10:
                self.win.pop(0)
            (t0, b0), (t1, b1) = (self.win[0], self.win[-1]) if self.win else ((0, 0), (0, 0))
            self.speed = (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0
            left = max(0, self.total_bytes - self.done_bytes)
            return {"status": self.status, "note": self.note, "error": self.error, "total_bytes": self.total_bytes,
                    "done_bytes": self.done_bytes, "disk_bytes": self.disk_bytes, "total_files": self.total_files,
                    "done_files": self.done_files, "failed": self.failed[:20], "failed_count": len(self.failed),
                    "connections": self.connections, "speed": self.speed,
                    "eta": (left / self.speed) if self.speed > 1 else None,
                    "started": self.started, "finished": self.finished, "verify": self.verify}


# ------------------------------------------------------------------ the manager

class Manager:
    def __init__(self, vault, get_ds, target_dir, state, save_state, settings):
        self.vault = vault
        self.root = str(vault.root)
        self.queue_path = str(vault.queue_path)
        self.manifest_dir = str(vault.manifests)
        attach_log(str(vault.log_path))
        self.get_ds = get_ds              # id -> dataset dict, or None (the catalogue can change at run time)
        self.target_dir = target_dir      # dataset -> folder path
        self.state = state                # shared dict persisted by server (status/path per dataset)
        self.save_state = save_state
        self.settings = settings
        self.throttle = Throttle()
        self.lock = threading.RLock()
        self.qlock = threading.Lock()     # queue.json writes in the order of their snapshots
        self.order = []                   # ids in queue order (queued / running / paused / error)
        self.jobs = {}                    # id -> Job (includes finished ones from this session)
        self.unknown = []                 # queue items the catalogue does not know (yet), kept verbatim
        self.threads = {}
        self.wake = threading.Event()
        self.closed = False
        self.apply_settings()
        self._load_queue()
        threading.Thread(target=self._dispatcher, daemon=True).start()

    # ---- persistence
    def _load_queue(self):
        saved = read_json(self.queue_path, None)
        if saved is None:     # no queue file at all: adopt downloads that were running under the first version
            saved = [{"id": k, "status": "paused" if v.get("status") == "partial" else "queued"}
                     for k, v in self.state.items()
                     if v.get("status") in ("queued", "resolving", "downloading", "partial")]
        seen = set()
        for item in saved if isinstance(saved, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in seen:
                continue
            seen.add(item["id"])
            ds = self.get_ds(item["id"])
            if ds:
                self._restore(item, ds)
            else:
                self.unknown.append(item)
        self._save_queue()

    def _restore(self, item, ds):
        ds_id = item["id"]
        j = Job(ds_id, bool(item.get("verify")))
        was = item.get("status", "queued")
        if was in ("paused", "error"):
            j.status = was
        elif not self.settings.get("auto_resume", True):
            j.status, j.auto_paused = "paused", True
        j.error = item.get("error")
        st = self.state.get(ds_id) or {}
        j.total_bytes = st.get("bytes") or ds.get("size_bytes") or 0
        j.done_bytes = st.get("done_bytes") or 0
        j.disk_bytes = self._estimate_disk(ds, j.done_bytes)
        self.jobs[ds_id] = j
        if ds_id not in self.order:
            self.order.append(ds_id)

    def _adopt(self):
        """Queue items whose dataset the catalogue did not know come back once it does."""
        if not self.unknown:
            return
        back = []
        with self.lock:
            for item in list(self.unknown):
                ds = self.get_ds(item["id"])
                if ds:
                    self.unknown.remove(item)
                    if item["id"] not in self.order:
                        self._restore(item, ds)
                        back.append(item["id"])
        if back:
            log.info("back in the catalogue: %s", ", ".join(back))
            self._save_queue()

    def _qstatus(self, j):
        if j.status == "error":
            return "error"
        if j.status == "paused":
            return "queued" if j.auto_paused else "paused"
        if j.status in RUNNING and j.stop.is_set() and not j.resume_after and not self.closed:
            return "paused"           # still pausing: a restart must not resume it
        return "queued"

    def _save_queue(self):
        with self.qlock:
            with self.lock:
                items = [{"id": i, "status": self._qstatus(self.jobs[i]), "verify": self.jobs[i].verify,
                          "error": self.jobs[i].error} for i in self.order]
                items += [it for it in self.unknown if it["id"] not in self.order]
            write_json(self.queue_path, items, backup=True)

    def _estimate_disk(self, ds, fallback):
        """Bytes a restored job already has on the drive, from its cached file list (cheap stat calls)."""
        try:
            with open(os.path.join(self.manifest_dir, ds["id"] + ".json"), encoding="utf-8") as fh:
                m = json.load(fh)
            files = prepare([dict(f) for f in (m["files"] if isinstance(m, dict) else m)], warn=False)
            dest = self.target_dir(ds)
            return sum(self._disk_bytes(dest, f) for f in files) if os.path.isdir(long_path(dest)) else 0
        except Exception:
            return fallback

    def apply_settings(self):
        self.throttle.set(float(self.settings.get("speed_limit_mbps", 0) or 0) * 1e6)
        self.wake.set()

    # ---- public API
    def enqueue(self, ids, verify=False):
        self._adopt()
        added = []
        with self.lock:
            for i in ids:
                ds = self.get_ds(i)
                if not ds or not ds.get("auto"):
                    continue
                j = self.jobs.get(i)
                if j and (j.status == "queued" or j.status in RUNNING):
                    if j.status in RUNNING and j.stop.is_set():
                        j.resume_after = True        # still pausing (or just removed): run again once stopped
                    if i not in self.order:
                        self.order.append(i)
                        added.append(i)
                    continue
                self.jobs[i] = Job(i, verify)
                self.jobs[i].total_bytes = ds.get("size_bytes") or 0
                if i not in self.order:
                    self.order.append(i)
                self.unknown = [it for it in self.unknown if it["id"] != i]
                added.append(i)
        self._save_queue()
        self.wake.set()
        return added

    def pause(self, ids):
        with self.lock:
            for i in ids:
                j = self.jobs.get(i)
                if not j:
                    continue
                if j.status == "queued":
                    j.status = "paused"
                elif j.status in RUNNING:
                    j.note = "pausing…"
                    j.stop.set()
                j.resume_after = j.auto_paused = False
        self._save_queue()

    def resume(self, ids):
        self._adopt()
        with self.lock:
            for i in ids:
                j = self.jobs.get(i)
                if j and j.status in ("paused", "error"):
                    j.status, j.error, j.note, j.auto_paused = "queued", None, "", False
                    if i not in self.order:
                        self.order.append(i)
                elif j and j.status in RUNNING and j.stop.is_set():
                    j.resume_after = True            # still pausing: queued again once its worker has stopped
                    if i not in self.order:
                        self.order.append(i)
                elif not j:
                    self.enqueue([i])
        self._save_queue()
        self.wake.set()

    def remove(self, ids):
        self.pause(ids)
        changed = False
        with self.lock:
            for i in ids:
                if i in self.order:
                    self.order.remove(i)
                self.unknown = [it for it in self.unknown if it["id"] != i]
                j = self.jobs.get(i)
                if j and j.status in ("queued", "paused", "error"):
                    del self.jobs[i]
                st = self.state.get(i)
                if st and st.get("status") in ("queued", "resolving", "downloading"):
                    if st.get("done_bytes") or (j and j.done_bytes) or self._has_files(st):
                        st.update(status="partial", error="removed")
                    else:
                        del self.state[i]
                    changed = True
        if changed:
            self.save_state()
        self._save_queue()

    def _has_files(self, st):
        p = st.get("path")
        try:
            return bool(p) and bool(os.listdir(long_path(p if os.path.isabs(p) else self.vault.abspath(p))))
        except OSError:
            return False

    def move(self, ds_id, where):
        with self.lock:
            if ds_id not in self.order:
                return
            i = self.order.index(ds_id)
            self.order.pop(i)
            n = {"top": 0, "up": max(0, i - 1), "down": i + 1, "bottom": len(self.order)}[where]
            self.order.insert(min(n, len(self.order)), ds_id)
        self._save_queue()

    def close(self):
        """Stop this manager (used when switching to another vault)."""
        self.closed = True
        self.pause([i for i in self.order if self.jobs[i].status in RUNNING])
        self.wake.set()

    def pause_all(self):
        self.pause(list(self.order))

    def resume_all(self):
        self.resume(list(self.order))

    def snapshot(self):
        with self.lock:
            order = list(self.order)
            jobs = {i: j.snapshot() for i, j in self.jobs.items()}
        active = [v for v in jobs.values() if v["status"] in RUNNING]
        todo = [jobs[i] for i in order if jobs[i]["status"] != "done"]
        return {"order": order, "jobs": jobs, "total_speed": sum(v["speed"] for v in active),
                # new drive space the queue still needs (preallocated .part files already hold theirs)
                "remaining_bytes": sum(max(0, v["total_bytes"] - v["disk_bytes"]) for v in todo),
                "left_bytes": sum(max(0, v["total_bytes"] - v["done_bytes"]) for v in todo),   # still to download
                "running": len(active)}

    # ---- scheduling
    def _dispatcher(self):
        awake = False
        while not self.closed:
            self.wake.wait(2)
            self.wake.clear()
            running = []
            try:
                self._adopt()
                with self.lock:
                    running = [i for i in self.order if self.jobs[i].status in RUNNING]
                    limit = max(1, int(self.settings.get("max_parallel", 1)))
                    for i in self.order:
                        if len(running) >= limit:
                            break
                        j = self.jobs[i]
                        if j.status == "queued":
                            j.status = "resolving"
                            j.stop.clear()
                            running.append(i)
                            t = threading.Thread(target=self._run, args=(i,), daemon=True)
                            self.threads[i] = t
                            t.start()
            except Exception:
                log.exception("dispatcher")
            want = bool(running) and self.settings.get("keep_awake", True)
            if want != awake:
                keep_awake(want)
                awake = want

    # ---- one dataset
    def _manifest(self, ds, force):
        """The host's file list, cached in .vault/manifests/."""
        os.makedirs(self.manifest_dir, exist_ok=True)
        p = os.path.join(self.manifest_dir, ds["id"] + ".json")
        old = None
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    old = json.load(f)
            except (OSError, ValueError):      # damaged (power cut): list again
                log.warning("cached file list of %s is damaged; listing again", ds["id"])
                try:
                    os.remove(p)
                except OSError:
                    pass
            if isinstance(old, dict) and old.get("version") == MANIFEST_VERSION and _files_ok(old.get("files")) \
                    and not force and time.time() - os.path.getmtime(p) < MANIFEST_MAX_AGE:
                return old["files"]
            old = old.get("files") if isinstance(old, dict) else old    # old format or expired: a fallback only
            old = old if _files_ok(old) else None
        try:
            files = resolve_manifest(ds["downloader"])
        except (PermissionError, LookupError) as e:      # gated, not open access, no file list, unknown host
            raise PermanentError((str(e) or "the host refused the file list") if type(e) in (PermissionError, LookupError)
                                 else f"cannot list the files ({type(e).__name__}: {e})")
        except Exception as e:
            if isinstance(e, urllib.error.HTTPError) and e.code in PERMANENT_HTTP:
                raise PermanentError(f"host refused the file list (HTTP {e.code} {e.reason})")
            if force or not old:
                raise
            log.warning("listing %s failed (%r); using the older cached file list", ds["id"], e)
            return old
        if files:
            try:
                write_json(p, {"version": MANIFEST_VERSION, "files": files}, indent=None)
            except OSError as e:
                log.warning("could not cache the file list of %s: %r", ds["id"], e)
        return files

    def _forget_manifest(self, ds_id):
        try:
            os.remove(os.path.join(self.manifest_dir, ds_id + ".json"))
        except OSError:
            pass

    def _run(self, ds_id):
        j = self.jobs[ds_id]
        st = self.state.get(ds_id, {})
        try:
            j.started, j.error, j.failed = time.time(), None, []
            ds = self.get_ds(ds_id)
            if not ds:
                raise PermanentError("this dataset is no longer in the catalogue")
            if not ds.get("downloader"):
                raise PermanentError("this dataset has no one-click download")
            st = self.state.setdefault(ds_id, st)
            dest = self.target_dir(ds)
            st.update(status="downloading", path=self.vault.relpath(dest), error=None)
            self.save_state()
            log.info("start %s -> %s (verify=%s)", ds_id, dest, j.verify)
            files = None
            delay = 5
            while files is None:          # listing the files also survives network drops
                try:
                    j.note = "listing files…"
                    files = self._manifest(ds, force=j.verify)
                except PermanentError:
                    raise
                except Exception as e:
                    j.note = f"waiting for network ({type(e).__name__}); retry in {delay} s"
                    if j.stop.wait(delay):
                        raise Paused()
                    delay = min(60, delay * 2)
            if not files:
                raise PermanentError("the host listed no files")
            files = prepare(files)
            j.total_bytes = sum(f["size"] for f in files)
            j.total_files = len(files)
            j.disk_bytes = sum(self._disk_bytes(dest, f) for f in files)
            need = j.total_bytes - j.disk_bytes
            free = self._free()
            if need > free - FREE_MARGIN:
                raise DiskFull(f"needs {need / 1e9:.1f} GB more but the drive has {free / 1e9:.1f} GB free (20 GB kept in reserve)")
            j.done_bytes, j.done_files = 0, 0
            j.win = []
            j.status, j.note = "downloading", ""
            st.update(bytes=j.total_bytes, files=j.total_files)
            self.save_state()
            os.makedirs(long_path(dest), exist_ok=True)
            small = [f for f in files if f["kind"] == "tcia" or f["size"] < SEGMENT_MIN]
            big = [f for f in files if f not in small]
            workers = CONNECTIONS.get(ds["downloader"]["type"], 4)
            with cf.ThreadPoolExecutor(workers) as ex:
                futs = [ex.submit(self._file, j, dest, f, False) for f in small]
                for f in big:                     # big files one at a time, each with N segments
                    if j.stop.is_set():
                        break
                    self._file(j, dest, f, True)
                for fu in cf.as_completed(futs):
                    fu.result()
            if j.stop.is_set():
                raise Paused()
            if j.failed:
                raise PermanentError(f"{len(j.failed)} file(s) could not be downloaded: {j.failed[0]}")
            info = {k: v for k, v in ds.items() if k != "fields"}
            info["registry_fields"] = ds["fields"]
            info["downloaded"] = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(os.path.join(long_path(dest), "_dataset_info.json"), "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=1)
            j.status, j.note, j.finished, j.verify = "done", "", time.time(), False
            st.update(status="done", finished=time.strftime("%Y-%m-%d %H:%M:%S"), error=None, done_bytes=j.total_bytes)
            with self.lock:
                if ds_id in self.order:
                    self.order.remove(ds_id)
            log.info("done %s", ds_id)
        except Paused:
            j.status, j.note = "paused", ""
            st.update(status="partial", error="paused" if ds_id in self.order else "removed", done_bytes=j.done_bytes)
            log.info("paused %s", ds_id)
        except DiskFull as e:
            j.status, j.error, j.note = "error", f"Drive full: {e}", ""
            st.update(status="partial", error=j.error, done_bytes=j.done_bytes)
            log.warning("disk full %s: %s", ds_id, e)
        except Exception as e:
            j.status, j.error, j.note = "error", str(e)[:400], ""
            st.update(status="partial" if j.done_bytes else "error", error=j.error, done_bytes=j.done_bytes)
            if isinstance(e, PermanentError):
                log.warning("error %s: %s", ds_id, e)
            else:
                log.exception("error %s", ds_id)
        try:
            self.save_state()
        except Exception:
            log.exception("could not save the download state")
        with self.lock:
            if j.resume_after and j.status == "paused":
                j.status = "queued"           # Resume was clicked while it was pausing
            j.resume_after = False
        try:
            self._save_queue()
        except Exception:
            log.exception("could not save the queue")
        self.wake.set()

    def _free(self):
        import shutil
        return shutil.disk_usage(self.root).free

    @staticmethod
    def _disk_bytes(dest, f):
        """Bytes this file already takes on the drive: the final file, or its .part at its allocated size."""
        try:
            p = long_path(file_target(dest, f))
        except PermanentError:
            return 0
        if f["kind"] == "tcia":
            return f["size"] if os.path.exists(os.path.join(p, "_complete")) else _alloc(os.path.join(p, "_series.zip.part"))
        n = _alloc(p) or _alloc(p + ".part")
        return min(n, f["size"]) if f["size"] else n

    @staticmethod
    def _discard(j, *paths):
        for p in paths:
            if p:
                n = _alloc(p)
                try:
                    os.remove(p)
                except FileNotFoundError:
                    continue
                j.disk(-n)

    def _fail(self, j, f, why):
        j.failed.append(f"{f['path']}: {why}")
        log.warning("file failed %s: %s", f["path"], why)

    # ---- one file, with endless retry for transient problems
    def _file(self, j, dest, f, big):
        delay, refusals, again = 3, 0, set()
        while True:
            if j.stop.is_set():
                return
            try:
                target = file_target(dest, f)
                if not os.path.isdir(self.root):
                    raise OSError("drive not available")
                if f["kind"] == "tcia":
                    self._tcia(j, f, target)
                elif big and f["size"] >= SEGMENT_MIN:
                    self._segmented(j, f, target)
                else:
                    self._single(j, f, target)
                if j.note.startswith("waiting"):
                    j.note = ""
                return
            except Paused:
                return
            except DiskFull:
                j.stop.set()
                raise
            except TryAgain as e:
                if f["kind"] != "tcia":
                    lt = long_path(target)
                    self._discard(j, lt + ".part", lt + ".part.json", lt + ".part.json.tmp")
                if e.kind not in again:
                    again.add(e.kind)
                    log.info("%s: %s; trying once more", f["path"], e)
                    continue
                if isinstance(e, SizeMismatch):
                    self._forget_manifest(j.id)     # the next run lists the files again
                return self._fail(j, f, e.final)
            except PermanentError as e:
                return self._fail(j, f, str(e))
            except Exception as e:
                if is_disk_full(e):
                    j.stop.set()
                    raise DiskFull("drive is full")
                if isinstance(e, urllib.error.HTTPError) and e.code in PERMANENT_HTTP:
                    refusals += 1
                    if refusals >= 3:
                        j.failed.append(f"{f['path']}: HTTP {e.code}")
                        log.warning("file refused %s: HTTP %s", f["path"], e.code)
                        return
                elif bad_name(e) and os.path.isdir(self.root):
                    return self._fail(j, f, f"this drive cannot store a file under this name ({e.strerror or e})")
                what = "drive not available" if not os.path.isdir(self.root) else type(e).__name__
                j.note = f"waiting for network/host ({what}); retrying in {delay} s"
                log.info("retry %s in %ss: %r", f["path"], delay, e)
                if j.stop.wait(delay):
                    return
                delay = min(60, delay * 2)

    def _read_into(self, j, r, fh, on_bytes=None):
        """Copies the body to fh; True when the response ended normally (False = connection cut short)."""
        while True:
            if j.stop.is_set():
                raise Paused()
            b = r.read(CHUNK)
            if not b:
                return not getattr(r, "length", None)
            self.throttle.consume(len(b))
            try:
                fh.write(b)
            except OSError as e:
                if is_disk_full(e):
                    raise DiskFull("drive is full")
                raise
            j.add(len(b))
            j.disk(len(b))
            if on_bytes:
                on_bytes(len(b))

    def _untrusted(self, j, f, part, final, meta):
        """Check a .part that may be preallocated: True if it verifies, else delete it."""
        size = f["size"]
        if size and os.path.getsize(part) == size and _hashed(f):
            log.info("verifying %s: its .part has no usable sidecar", f["path"])
            j.add(size)
            self._finish(j, f, part, final, size, meta)
            return True
        log.info("discarding %s.part: no usable sidecar and no checksum to verify it", f["path"])
        self._discard(j, part, meta, meta + ".tmp")
        return False

    def _single(self, j, f, target):
        lt = long_path(target)
        size = f["size"]
        if os.path.exists(lt) and (not size or os.path.getsize(lt) == size):
            j.add(os.path.getsize(lt), file_done=True)
            return
        os.makedirs(os.path.dirname(lt), exist_ok=True)
        part, meta = lt + ".part", lt + ".part.json"
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if have and (os.path.exists(meta) or (size and have >= size)):
            if self._untrusted(j, f, part, lt, meta):
                return
            have = 0
        counted, complete = [0], True
        j.conn(1)
        try:
            try:
                r = open_stream(f["url"], headers={"Range": f"bytes={have}-"} if have else {})
            except urllib.error.HTTPError as e:
                if e.code != 416 or not have:
                    raise
                total = _host_size(e.headers, 416)
                if size and total is not None and total != size:
                    raise SizeMismatch(total, size)
                if size or total != have:
                    raise TryAgain("host refused to resume (HTTP 416)", "range")
                r = None                      # size unknown, and the host says the .part holds the whole file
            if r is None:
                j.add(have)
                counted[0] = have
            else:
                host = _host_size(r.headers, r.status)
                if size and host is not None and host != size:
                    drop_conn(f["url"])
                    raise SizeMismatch(host, size)
                base = have if (have and r.status == 206) else 0
                if base and _range_start(r.headers) not in (None, base):
                    drop_conn(f["url"])
                    raise IOError("host sent another byte range")
                if have and not base:
                    j.disk(-have)             # the host ignored Range: the .part starts over
                j.add(base)
                counted[0] = base
                try:
                    with open(part, "ab" if base else "wb") as fh:
                        complete = self._read_into(j, r, fh, lambda n: counted.__setitem__(0, counted[0] + n))
                except BaseException:
                    drop_conn(f["url"])
                    raise
        except Paused:
            raise
        except BaseException:
            j.add(-counted[0])   # the retry re-counts what is on disk
            raise
        finally:
            j.conn(-1)
        got = os.path.getsize(part)
        if size and got != size:
            j.add(-counted[0])
            if complete:
                raise SizeMismatch(got, size)
            raise IOError(f"incomplete ({got} of {size} bytes)")
        self._finish(j, f, part, lt, counted[0], meta)

    def _segmented(self, j, f, target):
        lt = long_path(target)
        size = f["size"]
        if os.path.exists(lt) and os.path.getsize(lt) == size:
            j.add(size, file_done=True)
            return
        os.makedirs(os.path.dirname(lt), exist_ok=True)
        part, meta = lt + ".part", lt + ".part.json"
        segs = None
        if os.path.exists(part):
            segs = _load_segments(meta, size) if os.path.getsize(part) == size else None
            if segs is None:
                if not os.path.exists(meta) and os.path.getsize(part) < size:
                    return self._single(j, f, target)   # partial from a single-stream download: keep going that way
                if self._untrusted(j, f, part, lt, meta):
                    return
        fresh = segs is None
        if fresh:
            try:
                r = open_stream(f["url"], headers={"Range": "bytes=0-0"})
            except urllib.error.HTTPError as e:
                if e.code != 416:
                    raise
                raise SizeMismatch(_host_size(e.headers, 416), size)
            host = _host_size(r.headers, r.status)
            if r.status == 206 and r.length == 1:
                r.read()
            else:
                drop_conn(f["url"])         # a host that ignores Range sends the whole file here: don't read it
            if host is not None and host != size:
                raise SizeMismatch(host, size)
            if r.status != 206:
                return self._single(j, f, target)
            n = max(1, min(int(self.settings.get("segments", 4)), size // (16 * 2**20)))
            step = size // n
            segs = [[k * step, size if k == n - 1 else (k + 1) * step, k * step] for k in range(n)]
        lock = threading.Lock()

        def save_meta():
            with lock:
                data = json.dumps({"size": size, "url": f["url"], "segments": segs})
            with open(meta + ".tmp", "w", encoding="utf-8") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            replace_retry(meta + ".tmp", meta)

        if fresh:                 # the sidecar exists before the preallocated .part does
            save_meta()
            with open(part, "wb") as fh:
                fh.truncate(size)
            j.disk(size)
        base = sum(s[2] - s[0] for s in segs)
        j.add(base)
        added, errors = [0], []
        try:
            self._run_segments(j, f, part, segs, lock, save_meta, errors, added)
        except Paused:
            raise
        except BaseException:
            j.add(-(base + added[0]))   # the retry re-counts what is on disk
            raise
        self._finish(j, f, part, lt, size, meta)

    def _run_segments(self, j, f, part, segs, lock, save_meta, errors, added):
        url, size = f["url"], f["size"]
        halt = threading.Event()      # stops every segment: pause, a failed segment, or a failed sidecar save

        def count(n):
            with lock:
                added[0] += n
            j.add(n)

        def sync(fh, s, n):           # s[2], what the sidecar saves, only moves past data that is on disk
            fh.flush()
            os.fsync(fh.fileno())
            with lock:
                s[2] += n
            return 0

        def run_seg(s):
            delay, ahead = 2, 0       # ahead: written and counted, not fsynced yet
            while s[2] < s[1] and not halt.is_set():
                if ahead:             # the last try failed before its bytes were durable: fetch them again
                    count(-ahead)
                    ahead = 0
                j.conn(1)
                try:
                    try:
                        r = open_stream(url, headers={"Range": f"bytes={s[2]}-{s[1] - 1}"})
                    except urllib.error.HTTPError as e:
                        if e.code == 416:
                            raise SizeMismatch(_host_size(e.headers, 416), size)
                        raise
                    if r.status != 206:
                        drop_conn(url)
                        raise IOError("server ignored the byte range")
                    host = _host_size(r.headers, 206)
                    if host is not None and host != size:
                        drop_conn(url)
                        raise SizeMismatch(host, size)
                    with open(part, "r+b") as fh:
                        fh.seek(s[2])
                        synced = time.time()
                        try:
                            while s[2] + ahead < s[1]:
                                if halt.is_set():
                                    drop_conn(url)
                                    break
                                b = r.read(min(CHUNK, s[1] - s[2] - ahead))
                                if not b:
                                    break
                                self.throttle.consume(len(b))
                                fh.write(b)
                                ahead += len(b)
                                count(len(b))
                                if ahead >= SYNC_BYTES or time.time() - synced >= SYNC_SECS:
                                    ahead, synced = sync(fh, s, ahead), time.time()
                        finally:
                            if ahead:
                                ahead = sync(fh, s, ahead)
                    if s[2] < s[1] and not halt.is_set():
                        drop_conn(url)
                        time.sleep(1)
                    delay = 2
                except Exception as e:
                    drop_conn(url)
                    if is_disk_full(e):
                        errors.append(DiskFull("drive is full"))
                        j.stop.set()
                        halt.set()
                        return
                    if isinstance(e, SizeMismatch) or (isinstance(e, urllib.error.HTTPError) and e.code in PERMANENT_HTTP):
                        errors.append(e)
                        halt.set()
                        return
                    j.note = f"waiting for network/host ({type(e).__name__}); retrying a segment in {delay} s"
                    if halt.wait(delay):
                        return
                    delay = min(60, delay * 2)
                finally:
                    j.conn(-1)

        threads = [threading.Thread(target=run_seg, args=(s,), daemon=True) for s in segs if s[2] < s[1]]
        for t in threads:
            t.start()
        last = time.time()
        try:
            while any(t.is_alive() for t in threads) and not halt.is_set():
                if j.stop.wait(0.5):
                    break
                if time.time() - last >= 2:
                    save_meta()
                    last = time.time()
        finally:
            halt.set()                # no segment thread may outlive this call (a retry would race it)
            for t in threads:
                t.join()
        save_meta()
        if errors:
            raise errors[0]
        if j.stop.is_set():
            raise Paused()
        if os.path.getsize(part) != size or any(s[2] < s[1] for s in segs):
            raise IOError("size mismatch after segments")

    def _finish(self, j, f, part, final, counted, meta=None):
        h = f.get("hash")
        if _hashed(f):
            j.note = f"verifying {os.path.basename(final)[:40]}…"
            hasher = hashlib.new(h[0])
            with open(part, "rb") as fh:
                while True:
                    if j.stop.is_set():
                        raise Paused()
                    b = fh.read(8 * CHUNK)
                    if not b:
                        break
                    hasher.update(b)
            if j.note.startswith("verifying"):
                j.note = ""
            if hasher.hexdigest().lower() != h[1].lower():
                self._discard(j, part, meta)
                j.add(-counted)
                log.warning("checksum mismatch %s", f["path"])
                raise TryAgain("checksum mismatch, downloading again", "checksum",
                               "checksum did not match the host's checksum (tried twice)")
        replace_retry(part, final)
        if meta and os.path.exists(meta):
            os.remove(meta)
        j.add(0, file_done=True)

    def _tcia(self, j, f, target):
        ldir = long_path(target)
        marker = os.path.join(ldir, "_complete")
        if os.path.exists(marker):
            j.add(f["size"], file_done=True)
            return
        os.makedirs(ldir, exist_ok=True)
        zpath = os.path.join(ldir, "_series.zip.part")
        got = [0]
        j.conn(1)
        try:
            r = open_stream(f["url"], timeout=300)
            with open(zpath, "wb") as fh:
                complete = self._read_into(j, r, fh, lambda n: got.__setitem__(0, got[0] + n))
            if not complete:
                raise IOError(f"incomplete series ({got[0]} bytes)")
            try:
                with zipfile.ZipFile(zpath) as z:
                    z.extractall(ldir)
            except zipfile.BadZipFile:
                raise TryAgain("NBIA did not send a zip for this series", "zip")
            os.remove(zpath)
            open(marker, "w").close()
            # the zip size differs from the reported DICOM size
            j.add(f["size"] - got[0], file_done=True)
            j.disk(f["size"] - got[0])
        except Paused:
            drop_conn(f["url"])
            raise
        except BaseException:
            drop_conn(f["url"])
            j.add(-got[0])
            try:
                self._discard(j, zpath)    # series zips do not resume
            except OSError:
                pass
            raise
        finally:
            j.conn(-1)
