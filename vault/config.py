"""Paths, settings and the vault folder."""
import errno
import json
import os
import re
import shutil
import string
import sys
import threading
import time
from pathlib import Path

from . import NAME

CODE_DIR = Path(__file__).resolve().parent
REPO_DIR = CODE_DIR.parent
BUNDLED_DATA = REPO_DIR / "data"
WEB_DIR = CODE_DIR / "web"
USER_CONFIG = Path(os.environ.get("NRV_CONFIG", Path.home() / f".{NAME.lower()}" / "config.json"))

# NeuroAIHub category -> folder base name, display order
DOMAIN_BASES = {
    "Neoplasm": "Neoplasm",
    "Cerebrovascular": "Cerebrovascular",
    "Neurodegenerative": "Neurodegenerative_Inflammatory",
    "Spinal": "Spine",
    "Other": "Other_Normative",
    "Neurodevelopmental": "Neurodevelopmental",
    "Psychiatric": "Psychiatric",
}
DOMAIN_LABELS = {
    "Neoplasm": "Neoplasm", "Cerebrovascular": "Cerebrovascular", "Neurodegenerative": "Neurodegenerative / Inflammatory",
    "Spinal": "Spine", "Other": "Other / Normative", "Neurodevelopmental": "Neurodevelopmental", "Psychiatric": "Psychiatric",
}
PRIORITY_FOLDERS = {1: "P1_open_with_masks", 2: "P2_open_no_masks", 3: "P3_manual_download", 4: "P4_restricted_access"}
PRIORITY_LABELS = {
    1: "Open, segmentation masks, one-click download",
    2: "Open, one-click download, no masks",
    3: "Open but needs a manual step (registration, Kaggle, Synapse, challenge site, TCIA Aspera)",
    4: "Restricted (data use agreement or application)",
}
DEFAULT_SETTINGS = {
    "later_domains": [], "max_parallel": 1, "segments": 4, "speed_limit_mbps": 0,
    "auto_resume": True, "keep_awake": True,
}


def read_json(p, default):
    """Read JSON, falling back to the .bak copy when the file is missing or damaged."""
    p = Path(p)
    for i in range(10):          # a virus scanner can hold the file for a moment
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            break
        except PermissionError:
            if i == 9:
                raise
            time.sleep(0.1 * (i + 1))
        except (OSError, ValueError):
            try:
                os.replace(p, p.with_name(p.name + ".corrupt-" + time.strftime("%Y%m%d-%H%M%S")))
            except OSError:
                pass
            break
    bak = p.with_name(p.name + ".bak")
    try:
        with open(bak, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


_write_lock = threading.Lock()


def replace_retry(src, dst, tries=20):
    """os.replace, retried: on Windows a virus scanner or indexer can hold a file for a moment."""
    for i in range(tries):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if i == tries - 1:
                raise
            time.sleep(0.05 * (i + 1))


def write_json(p, data, indent=1, backup=False):
    """Atomic, thread-safe JSON write; backup=True keeps the previous file as .bak."""
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    with _write_lock:
        tmp = p.with_suffix(p.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if backup and p.exists():
            try:
                shutil.copyfile(p, p.with_name(p.name + ".bak"))
            except OSError:
                pass
        replace_retry(tmp, p)


class Vault:
    """A data folder plus its private .vault/ directory."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.meta = self.root / ".vault"
        self.state_path = self.meta / "state.json"
        self.queue_path = self.meta / "queue.json"
        self.settings_path = self.meta / "settings.json"
        self.cache = self.meta / "cache"
        self.manifests = self.meta / "manifests"
        self.log_path = self.meta / "download.log"
        saved = read_json(self.settings_path, None)
        if not isinstance(saved, dict):
            # settings lost: take the Later domains from the folders on disk, so nothing gets renamed
            saved = {"later_domains": self._later_on_disk()}
        self.settings = {**DEFAULT_SETTINGS, **saved}
        self.folders = {}
        self.apply_domain_order()

    def _later_on_disk(self):
        try:
            names = os.listdir(self.root)
        except OSError:
            return []
        return [c for c, base in DOMAIN_BASES.items()
                if any(re.fullmatch(r"\d\d_LATER_" + re.escape(base), n) for n in names)]

    # ---- settings / folder names
    def save_settings(self):
        write_json(self.settings_path, self.settings, backup=True)

    def apply_domain_order(self):
        later = [c for c in DOMAIN_BASES if c in self.settings["later_domains"]]
        focus = [c for c in DOMAIN_BASES if c not in later]
        self.folders = {c: f"{i:02d}_" + ("LATER_" if c in later else "") + DOMAIN_BASES[c]
                        for i, c in enumerate(focus + later, 1)}

    def dataset_rel(self, ds):
        return "/".join([self.folders[ds["category"]], PRIORITY_FOLDERS[ds["priority"]], ds["folder"]])

    def abspath(self, rel):
        return str(self.root.joinpath(*rel.split("/")))

    def relpath(self, p):
        try:
            return Path(p).resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(p)

    def create(self):
        self.meta.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":  # hide .vault on Windows
            try:
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(str(self.meta), 0x02)
            except Exception:
                pass
        if not self.settings_path.exists():
            self.save_settings()


# ---- one download engine per vault

def lock_vault(meta):
    """Exclusive lock so only one process downloads into a vault; None if it is taken."""
    f = open(Path(meta) / "engine.lock", "a+b")
    try:
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, "EDEADLOCK", errno.EACCES)):
            f.close()
            return None
        # a file system without locks: carry on unguarded rather than refuse the vault
    return f


def unlock_vault(f):
    if not f:
        return
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    f.close()


# ---- user pointer to the last vault

def load_user_config():
    return read_json(USER_CONFIG, {})


def save_user_config(cfg):
    write_json(USER_CONFIG, cfg)


def remember_vault(root):
    cfg = load_user_config()
    cfg["vault"] = str(Path(root).resolve())
    recent = [r for r in cfg.get("recent", []) if r != cfg["vault"]]
    cfg["recent"] = [cfg["vault"]] + recent[:4]
    save_user_config(cfg)


def find_vault(cli_path=None):
    """CLI argument, else the vault this app folder sits in (portable drive), else the remembered vault."""
    if cli_path:
        return Path(cli_path)
    if (REPO_DIR.parent / ".vault").is_dir():      # portable: the app sits inside a vault (e.g. <drive>/Vault/_app)
        return REPO_DIR.parent
    cfg = load_user_config()
    for p in [cfg.get("vault")] + cfg.get("recent", []):
        if p and (Path(p) / ".vault").is_dir():
            return Path(p)
    return None


# ---- drives for the setup screen

def list_drives():
    out = []
    if os.name == "nt":
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        for i, letter in enumerate(string.ascii_uppercase):
            if mask & (1 << i):
                root = f"{letter}:\\"
                dtype = ctypes.windll.kernel32.GetDriveTypeW(root)
                if dtype not in (2, 3):  # removable or fixed
                    continue
                label = ctypes.create_unicode_buffer(261)
                ctypes.windll.kernel32.GetVolumeInformationW(root, label, 261, None, None, None, None, 0)
                out.append((root, label.value or ("Removable" if dtype == 2 else "Local disk")))
    else:
        cands = [("/", "System"), (str(Path.home()), "Home")]
        for base in ("/Volumes", "/media/" + os.environ.get("USER", ""), "/run/media/" + os.environ.get("USER", ""), "/mnt"):
            if os.path.isdir(base):
                cands += [(os.path.join(base, d), d) for d in sorted(os.listdir(base))]
        out = [(p, l) for p, l in cands if os.path.isdir(p)]
    drives = []
    for path, label in out:
        try:
            u = shutil.disk_usage(path)
        except OSError:
            continue
        drives.append({"path": path, "label": label, "total": u.total, "free": u.free})
    seen, uniq = set(), []
    for d in drives:  # mac/linux: several mount points can be the same disk
        key = (d["total"], d["free"]) if os.name != "nt" else d["path"]
        if key not in seen:
            seen.add(key)
            uniq.append(d)
    return uniq


def open_in_file_manager(path):
    if os.name == "nt":
        os.startfile(path)
    elif sys.platform == "darwin":
        import subprocess
        subprocess.Popen(["open", path])
    else:
        import subprocess
        subprocess.Popen(["xdg-open", path])
