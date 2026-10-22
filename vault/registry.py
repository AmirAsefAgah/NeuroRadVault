"""The dataset catalogue, read from this project's repository with a local cache."""
import csv
import hashlib
import io
import json
import re
import time
from pathlib import Path

from . import BRANCH, REPO
from .config import BUNDLED_DATA, DOMAIN_BASES, write_json
from .hosts import get_json, http, safe_name

UPSTREAM_REPO = "NeuroAIHub-Registry/NeuroAIHub"      # mirrored monthly; the app itself never needs it
UPSTREAM_PATH = "web_app/assets/datasets"
CACHE_HOURS = 24
URL_RE = re.compile(r"https?://[^\s;,)\]]+")


# ------------------------------------------------------------------ shared rules (also used by enrich.py)

def classify_access(text):
    t = (text or "").lower()
    if t.startswith("restricted"):
        return "restricted"
    if re.search(r"regist|request|applica|agreement|\bdua\b|account|approv|apply|login|sign|terms|commons|dbgap|kaggle|synapse", t):
        return "open_conditional"
    return "open" if t.startswith("open") else "unknown"


def short_name(name):
    """Readable folder name: 'UPENN-GBM (...)' -> 'UPENN-GBM', 'Ischemic ... (ISLES): 2022' -> 'ISLES 2022'."""
    name = name.strip()
    head = re.split(r":\s", name, maxsplit=1)[0].strip()
    if head != name and 3 <= len(head) <= 25:
        return head
    m = re.match(r"^([^()]*?)\s*\(([^()\s]{2,24})\)\s*:?\s*(.*)$", name)
    if m and len(m.group(1)) > 25:
        rest = re.split(r"[(;]", m.group(3))[0].strip(" ,")
        return (m.group(2) + " " + rest[:24]).strip()
    lead = re.split(r"\s+\(|:\s|\s[—–-]\s", name, maxsplit=1)[0].strip()
    return lead if len(lead) >= 3 else name


def detect(urls):
    """Download route from a dataset's links, or None."""
    for u in urls:
        m = re.search(r"openneuro\.org/datasets/(ds\d{6})", u) or re.search(r"\b(ds\d{6})\b", u if "openneuro" in u else "")
        if m:
            return {"type": "openneuro", "arg": m.group(1)}
    for u in urls:
        m = re.search(r"cancerimagingarchive\.net/collection/([\w\-.]+)", u)
        if m:
            return {"type": "tcia", "arg": m.group(1).lower()}
        m = re.search(r"zenodo\.org/records?/(\d+)", u) or re.search(r"zenodo\.(\d+)", u)
        if m:
            return {"type": "zenodo", "arg": m.group(1)}
        m = re.search(r"m9\.figshare\.c\.(\d+)", u) or re.search(r"figshare\.com/collections/[^/]+/(\d+)", u)
        if m:
            return {"type": "figshare", "arg": ["collection", m.group(1)]}
        m = re.search(r"m9\.figshare\.(\d+)", u) or re.search(r"figshare\.com/articles/(?:[^/]+/)*?(\d{5,})", u)
        if m:
            return {"type": "figshare", "arg": ["article", m.group(1)]}
        m = re.search(r"(data\.mendeley\.com|digitalcommonsdata\.wustl\.edu)/datasets/(\w+)(?:/(\d+))?", u)
        if m:
            return {"type": "mendeley", "arg": ["https://" + m.group(1), m.group(2), m.group(3) or "1"]}
        m = re.search(r"10\.17632/(\w+)\.(\d+)", u)
        if m:
            return {"type": "mendeley", "arg": ["https://data.mendeley.com", m.group(1), m.group(2)]}
        m = re.search(r"huggingface\.co/datasets/([\w\-.]+/[\w\-.]+)", u)
        if m:
            return {"type": "huggingface", "arg": m.group(1)}
        m = re.search(r"osf\.io/(\w{5})/?(?:$|[?#])", u)
        if m:
            return {"type": "osf", "arg": m.group(1)}
    return None


def row_urls(r):
    urls = URL_RE.findall(r.get("url", ""))
    dois = re.findall(r"10\.\d{4,9}/[^\s;,()]+", r.get("doi", ""))
    if not urls and dois:
        urls = ["https://doi.org/" + dois[0].rstrip(".")]
    return urls


def stable_ids(rows):
    """Readable, stable id per row: slug of the name; duplicates get a short hash of DOI+URL."""
    ids, seen = [], {}
    for r in rows:
        base = re.sub(r"[^a-z0-9]+", "-", r["dataset_name"].lower()).strip("-")[:60] or "dataset"
        seen[base] = seen.get(base, 0) + 1
    used = set()
    for r in rows:
        base = re.sub(r"[^a-z0-9]+", "-", r["dataset_name"].lower()).strip("-")[:60] or "dataset"
        uid = base
        if seen[base] > 1:
            uid = base + "-" + hashlib.sha1((r.get("doi", "") + r.get("url", "")).encode()).hexdigest()[:6]
        n = 2
        while uid in used:
            uid, n = f"{uid}-{n}", n + 1
        used.add(uid)
        ids.append(uid)
    return ids


# ------------------------------------------------------------------ fetching with cache + fallback

CATEGORY_FILES = [f"{c}.csv" for c in DOMAIN_BASES]
ADDITIONS = "additions.csv"


def _fresh(p):
    return p.exists() and time.time() - p.stat().st_mtime < CACHE_HOURS * 3600


def parse_csv_bytes(data, category=None):
    """Rows of one CSV. Category files take the category from the file name; additions.csv has a column."""
    rows = []
    for row in csv.DictReader(io.StringIO(data.decode("utf-8-sig"))):
        row = {k.strip(): (v or "").strip() for k, v in row.items() if k}
        cat = category or row.get("category", "")
        if row.get("dataset_name") and cat in DOMAIN_BASES:
            row["category"] = cat
            rows.append(row)
    return rows


def read_folder(folder):
    rows = []
    for name in CATEGORY_FILES:
        f = folder / name
        if f.exists():
            rows += parse_csv_bytes(f.read_bytes(), name[:-4])
    f = folder / ADDITIONS
    if f.exists():
        rows += parse_csv_bytes(f.read_bytes())
    return rows


def fetch_registry(cache_dir, force=False, network=True):
    """Return (rows, source): the catalogue on GitHub (cached 24 h), else the bundled copy."""
    cache = Path(cache_dir) / "registry"
    stamp = cache / "fetched_at"
    if network and (force or not _fresh(stamp)):
        try:
            got = {}
            for n in CATEGORY_FILES + [ADDITIONS]:
                with http(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/data/registry/{n}", timeout=30) as r:
                    got[n] = r.read()
            if sum(len(parse_csv_bytes(v, None if n == ADDITIONS else n[:-4])) for n, v in got.items()) > 100:
                cache.mkdir(parents=True, exist_ok=True)
                for n, data in got.items():
                    (cache / n).write_bytes(data)
                stamp.write_text(str(time.time()))
        except Exception:
            pass
    for src, folder in (("project catalogue on GitHub", cache), ("bundled catalogue", BUNDLED_DATA / "registry")):
        rows = read_folder(folder) if folder.is_dir() else []
        if rows:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(stamp.read_text()))) if folder == cache and stamp.exists() else ""
            return rows, f"{src}{' (fetched ' + when + ')' if when else ''}"
    raise RuntimeError("no catalogue data found")


def fetch_upstream():
    """Used by the monthly update only: NeuroAIHub's current CSVs as {file name: bytes}, or None if unavailable."""
    try:
        names = set(CATEGORY_FILES)
        try:
            listing = get_json(f"https://api.github.com/repos/{UPSTREAM_REPO}/contents/{UPSTREAM_PATH}", timeout=20, retries=2)
            names |= {x["name"] for x in listing if x["name"].endswith(".csv")}
        except Exception:
            pass
        got = {}
        for n in sorted(names):
            with http(f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/main/{UPSTREAM_PATH}/{n}", timeout=30) as r:
                got[n] = r.read()
        total = sum(len(parse_csv_bytes(v, n[:-4])) for n, v in got.items() if n in CATEGORY_FILES)
        return got if total > 100 else None      # never replace the mirror with an empty or broken upstream
    except Exception:
        return None


def fetch_enrichment(cache_dir, force=False, network=True):
    p = Path(cache_dir) / "enrichment.json"
    if network and (force or not _fresh(p)):
        try:
            d = get_json(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/data/enrichment.json", timeout=30, retries=1)
            if d and "datasets" in d:
                write_json(p, d, indent=None)
        except Exception:
            pass
    for f in (p, BUNDLED_DATA / "enrichment.json"):
        if f.exists():
            try:
                with open(f, encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                continue
    return {"generated": None, "datasets": {}}


# ------------------------------------------------------------------ building the catalogue

def build_catalog(cache_dir, force=False, offline=False):
    rows, source = fetch_registry(cache_dir, force, network=not offline)
    enr = fetch_enrichment(cache_dir, force, network=not offline)
    known = enr.get("datasets", {})
    datasets, used = [], set()
    for uid, r in zip(stable_ids(rows), rows):
        urls = row_urls(r)
        e = known.get(uid)
        ds = {
            "id": uid, "category": r["category"], "name": r["dataset_name"], "short": short_name(r["dataset_name"]),
            "urls": urls, "link": urls[0] if urls else None,
            "fields": {k: v for k, v in r.items() if k != "category"},
            "access_class": classify_access(r.get("access_type")),
            "masks": r.get("segmentation_mask", "").lower().startswith("yes"),
            "modality": r.get("modality", ""), "subjects": r.get("subject_no", ""), "year": r.get("year", ""),
            "disease": r.get("disease", ""), "format": r.get("format", ""), "access_type": r.get("access_type", ""),
            "downloader": None, "size_bytes": None, "file_count": None, "checked": bool(e),
        }
        if e:
            for k in ("downloader", "size_bytes", "file_count", "size_note", "auto_error"):
                if e.get(k) is not None:
                    ds[k] = e[k]
            if e.get("link"):
                ds["link"] = e["link"]
        elif ds["access_class"] != "restricted":
            dl = detect(urls)
            if dl and dl["type"] != "tcia":   # TCIA needs the monthly check to map the collection name
                ds["downloader"] = dl
                ds["size_note"] = "new in the registry: size is measured when you download"
        ds["auto"] = bool(ds["downloader"])
        ds["priority"] = (1 if ds["masks"] else 2) if ds["auto"] else (4 if ds["access_class"] == "restricted" else 3)
        folder = safe_name(ds["short"], 40)
        if ds["downloader"] and ds["downloader"]["type"] == "openneuro":
            folder = safe_name(ds["short"], 30) + "_" + ds["downloader"]["arg"]
        base, n = folder, 2
        while (ds["category"], ds["priority"], folder.lower()) in used:
            folder, n = f"{base[:37]}_{n}", n + 1
        used.add((ds["category"], ds["priority"], folder.lower()))
        ds["folder"] = folder
        datasets.append(ds)
    return {"count": len(datasets), "registry_source": source, "enrichment_generated": enr.get("generated"),
            "built": time.strftime("%Y-%m-%d %H:%M:%S"), "datasets": datasets}
