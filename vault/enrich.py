"""Monthly catalogue update: refresh the mirror, then measure sizes and download routes."""
import concurrent.futures as cf
import json
import re
import sys
import time
import urllib.error

from .config import BUNDLED_DATA, write_json
from .hosts import get_json, http, resolve_manifest, tcia_series
from .registry import (ADDITIONS, UPSTREAM_REPO, classify_access, detect, fetch_upstream, read_folder, row_urls,
                       stable_ids)

ADDITIONS_HEADER = ("category,dataset_name,doi,url,year,access_type,institution,country,modality,resolution,subject_no,"
                    "slice_scan_no,age_range,acquisition_protocol,format,segmentation_mask,preprocessing,disease,"
                    "healthy_control,staging_information,clinical_data,histopathology,lab_data\n")
TRANSIENT = (urllib.error.URLError, TimeoutError, ConnectionError, OSError)


def log(*a):
    print(*a, flush=True)


def resolve_redirect(url):
    try:
        with http(url, timeout=30, headers={"Accept": "text/html"}) as r:
            return r.geturl()
    except Exception as e:
        return getattr(e, "url", None) or getattr(e, "filename", None) or url


def openneuro_sizes(ids):
    out, ids = {}, list(ids)
    for i in range(0, len(ids), 25):
        chunk = ids[i:i + 25]
        q = "{" + " ".join(f'd{j}: dataset(id:"{d}"){{ id latestSnapshot {{ tag size }} }}' for j, d in enumerate(chunk)) + "}"
        try:
            d = get_json("https://openneuro.org/crn/graphql", data=json.dumps({"query": q}).encode(),
                         headers={"Content-Type": "application/json"}, timeout=120)
        except Exception as e:
            log("  ! OpenNeuro size query failed:", e)
            continue
        for j, acc in enumerate(chunk):
            v = (d.get("data") or {}).get(f"d{j}")
            if v and v.get("latestSnapshot"):
                out[acc] = (int(v["latestSnapshot"]["size"] or 0), v["latestSnapshot"]["tag"])
    return out


def probe(rec):
    dl = rec["downloader"]
    try:
        if dl["type"] == "tcia":
            series = tcia_series(dl["arg"])
            if not series:
                raise LookupError("no public DICOM series in the NBIA API (hosted elsewhere, e.g. Aspera/NIfTI)")
            rec["size_bytes"] = sum(int(s.get("FileSize") or 0) for s in series)
            rec["file_count"] = len(series)
            rec["size_note"] = f"{len(series)} DICOM series"
        else:
            files = resolve_manifest(dl)
            if sum(f["size"] for f in files) < 5_000_000:
                raise LookupError("host lists no data files (only docs/licence); data is hosted elsewhere")
            rec["size_bytes"] = sum(f["size"] for f in files)
            rec["file_count"] = len(files)
    except Exception as e:
        transient = isinstance(e, TRANSIENT) and not isinstance(e, PermissionError) and not (isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 404, 410))
        rec["_transient"] = transient
        rec["auto_error"] = f"{dl['type']}: {e}"[:200]
        rec["downloader"] = None
    return rec


def main(out_dir=BUNDLED_DATA):
    t0 = time.time()
    reg = out_dir / "registry"
    reg.mkdir(parents=True, exist_ok=True)
    upstream = fetch_upstream()
    if upstream:
        for n, data in upstream.items():
            (reg / n).write_bytes(data)          # verbatim mirror
        log(f"mirror refreshed from {UPSTREAM_REPO} ({len(upstream)} files)")
    else:
        log(f"! {UPSTREAM_REPO} unreachable or empty: keeping the last mirrored copy")
    if not (reg / ADDITIONS).exists():
        (reg / ADDITIONS).write_text(ADDITIONS_HEADER, encoding="utf-8")
    rows = read_folder(reg)
    source = "project catalogue" + (f" (mirror refreshed {time.strftime('%Y-%m-%d')})" if upstream else " (upstream unavailable)")
    if len(rows) < 100:
        raise SystemExit("catalogue looks broken (fewer than 100 datasets); not publishing")
    log(f"{len(rows)} datasets in the catalogue")
    prev = {}
    try:
        with open(out_dir / "enrichment.json", encoding="utf-8") as f:
            prev = json.load(f).get("datasets", {})
    except Exception:
        pass

    tcia_map = {}
    try:
        vals = get_json("https://services.cancerimagingarchive.net/nbia-api/services/v1/getCollectionValues")
        tcia_map = {re.sub(r"[^a-z0-9]", "", v["Collection"].lower()): v["Collection"] for v in vals}
    except Exception as e:
        log("  ! TCIA collection list failed:", e)

    recs = {}
    doi_urls = set()
    for uid, r in zip(stable_ids(rows), rows):
        urls = row_urls(r)
        recs[uid] = {"_urls": urls, "_restricted": classify_access(r.get("access_type")) == "restricted",
                     "downloader": None, "size_bytes": None, "file_count": None}
        doi_urls |= {u for u in urls if re.match(r"https?://(dx\.)?doi\.org/", u)}
    log(f"resolving {len(doi_urls)} DOI links")
    with cf.ThreadPoolExecutor(8) as ex:
        resolved = dict(zip(doi_urls, ex.map(resolve_redirect, doi_urls)))

    for uid, rec in recs.items():
        if rec["_restricted"]:
            continue
        urls = rec["_urls"] + [resolved.get(u, u) for u in rec["_urls"]]
        dl = detect(urls)
        if dl and dl["type"] == "tcia":
            key = re.sub(r"[^a-z0-9]", "", dl["arg"])
            if key in tcia_map:
                dl["arg"] = tcia_map[key]
            else:
                rec["auto_error"] = f"tcia: collection '{dl['arg']}' not in the public NBIA API (download from the TCIA page)"
                dl = None
        rec["downloader"] = dl

    on = {r["downloader"]["arg"]: (uid, r) for uid, r in recs.items() if r["downloader"] and r["downloader"]["type"] == "openneuro"}
    log(f"OpenNeuro sizes for {len(on)} datasets")
    sizes = openneuro_sizes(on)
    for acc, (uid, rec) in on.items():
        if acc in sizes and sizes[acc][0] > 0:
            rec["size_bytes"], tag = sizes[acc]
            rec["size_note"] = f"OpenNeuro {acc} v{tag}"
            rec["link"] = f"https://openneuro.org/datasets/{acc}"
        elif prev.get(uid, {}).get("downloader"):
            recs[uid] = {**prev[uid]}
        else:
            rec["auto_error"], rec["downloader"] = f"openneuro: {acc} not found", None

    others = [(uid, r) for uid, r in recs.items() if r["downloader"] and r["downloader"]["type"] != "openneuro"]
    log(f"probing {len(others)} datasets on other hosts")
    with cf.ThreadPoolExecutor(6) as ex:
        for (uid, _), rec in zip(others, ex.map(lambda x: probe(x[1]), others)):
            if rec.get("_transient") and prev.get(uid, {}).get("downloader"):
                recs[uid] = {**prev[uid]}          # host hiccup this week: keep last week's answer
            log(f"  {(recs[uid]['downloader'] or {}).get('type', 'x'):11s} {(recs[uid].get('size_bytes') or 0) / 1e9:8.2f} GB  {uid[:50]}")

    clean = {uid: {k: v for k, v in r.items() if not k.startswith("_") and v is not None} for uid, r in recs.items()}
    write_json(out_dir / "enrichment.json", {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()) + " UTC",
        "registry_source": source, "count": len(clean), "datasets": clean})
    auto = sum(1 for r in clean.values() if r.get("downloader"))
    log(f"done in {time.time() - t0:.0f}s: {len(clean)} datasets, {auto} one-click")
    return 0


if __name__ == "__main__":
    sys.exit(main())
