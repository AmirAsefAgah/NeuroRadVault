"""HTTP helpers and per-host file listers ("manifests") for every host with a public download API."""
import base64
import http.client as httpclient
import io
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

from . import NAME, REPO, VERSION

UA = f"{NAME}/{VERSION} (+https://github.com/{REPO})"

def long_path(p):
    """Windows extended-length path so deep BIDS trees don't hit MAX_PATH."""
    p = os.path.abspath(p)
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + p
    return p


def http(url, data=None, headers=None, timeout=60, method=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    return urllib.request.urlopen(req, timeout=timeout)


_tls = threading.local()


def proxy_for(scheme, netloc):
    """The http(s) proxy urllib would use for this URL, or None."""
    px = urllib.request.getproxies().get(scheme)
    if not px or urllib.request.proxy_bypass(netloc.rpartition("@")[2]):
        return None
    p = urllib.parse.urlsplit(px if "://" in px else "http://" + px)
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    auth = None
    if p.username is not None:
        cred = urllib.parse.unquote(p.username) + ":" + urllib.parse.unquote(p.password or "")
        auth = "Basic " + base64.b64encode(cred.encode("utf-8")).decode("ascii")
    return p.scheme, p.hostname, port, auth


def _connect(u, timeout):
    """New connection to u's host, through the proxy urllib would use (looked up once per connection)."""
    px = proxy_for(u.scheme, u.netloc)
    if px is None:
        cls = httpclient.HTTPSConnection if u.scheme == "https" else httpclient.HTTPConnection
        return cls(u.netloc, timeout=timeout), None
    pscheme, phost, pport, auth = px
    if u.scheme == "https":              # CONNECT tunnel; TLS runs end to end with the real host
        c = httpclient.HTTPSConnection(phost, pport, timeout=timeout)
        c.set_tunnel(u.hostname, u.port or 443, headers={"Proxy-Authorization": auth} if auth else None)
    else:                                # plain http: ask the proxy for the absolute URL
        cls = httpclient.HTTPSConnection if pscheme == "https" else httpclient.HTTPConnection
        c = cls(phost, pport, timeout=timeout)
    return c, px


def open_stream(url, headers=None, timeout=120, _hops=0):
    """GET over a per-thread keep-alive connection, through the system proxy, following redirects."""
    u = urllib.parse.urlsplit(url)
    conns = _tls.__dict__.setdefault("conns", {})
    key = (u.scheme, u.netloc)           # -> (connection, proxy it goes through)
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    for attempt in (0, 1):
        if key not in conns:
            conns[key] = _connect(u, timeout)
        c, px = conns[key]
        sel, hp = path, h
        if px and u.scheme == "http":
            sel = urllib.parse.urlunsplit((u.scheme, u.netloc, u.path or "/", u.query, ""))
            hp = {**h, "Proxy-Authorization": px[3]} if px[3] else h
        try:
            c.request("GET", sel, headers=hp)
            r = c.getresponse()
            break
        except (httpclient.HTTPException, OSError):
            c.close()
            conns.pop(key, None)
            if attempt:
                raise
    _tls.last = (url, key)
    if r.status in (301, 302, 303, 307, 308) and _hops < 8:
        loc = r.getheader("Location")
        r.read()
        r = open_stream(urllib.parse.urljoin(url, loc), headers, timeout, _hops + 1)
        _tls.last = (url, _tls.last[1])  # drop_conn(url) also drops the host the body came from
        return r
    if r.status >= 400:
        r.read()
        raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, io.BytesIO())   # fp=None breaks attribute access on 3.9/3.10
    return r


def drop_conn(url):
    u = urllib.parse.urlsplit(url)
    conns = _tls.__dict__.setdefault("conns", {})
    last = getattr(_tls, "last", None)
    for key in {(u.scheme, u.netloc), last[1] if last and last[0] == url else None}:
        c = conns.pop(key, None)
        if c:
            c[0].close()


def get_json(url, data=None, headers=None, timeout=60, retries=3):
    last = None
    for i in range(retries):
        try:
            with http(url, data=data, headers=headers, timeout=timeout) as r:
                body = r.read()
                return json.loads(body) if body.strip() else None
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404, 410):
                raise
            last = e
        except Exception as e:  # network hiccup
            last = e
        time.sleep(1.5 * (i + 1))
    raise last


def safe_name(s, maxlen=48):
    s = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "", s)
    s = re.sub(r"\s+", "_", s.strip())
    s = re.sub(r"_+", "_", s).strip("._")
    return s[:maxlen].rstrip("._") or "dataset"




# ---------------------------------------------------------------------------
# Manifest resolvers: return list of dicts {url, path, size, kind}
#   kind "file" -> plain HTTP GET saved to path
#   kind "tcia" -> NBIA series zip, extracted into path (a directory)
# ---------------------------------------------------------------------------

def openneuro_files(acc):
    files, token = [], None
    while True:
        q = {"list-type": "2", "prefix": acc + "/"}
        if token:
            q["continuation-token"] = token
        url = "https://s3.amazonaws.com/openneuro.org?" + urllib.parse.urlencode(q)
        with http(url) as r:
            xml = r.read().decode("utf-8")
        for block in re.findall(r"<Contents>(.*?)</Contents>", xml, re.S):
            key = re.search(r"<Key>(.*?)</Key>", block).group(1)
            size = re.search(r"<Size>(\d+)</Size>", block).group(1)
            etag = (re.search(r"<ETag>(.*?)</ETag>", block) or [None, ""])[1].replace("&quot;", "").strip('"')
            key = key.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")
            rel = key[len(acc) + 1:]
            if not rel or rel.startswith(".datalad/") or rel.endswith("/"):
                continue
            files.append({"url": "https://s3.amazonaws.com/openneuro.org/" + urllib.parse.quote(key),
                          "path": rel, "size": int(size), "kind": "file",
                          "hash": ["md5", etag] if re.fullmatch(r"[0-9a-f]{32}", etag) else None})
        m = re.search(r"<NextContinuationToken>(.*?)</NextContinuationToken>", xml)
        if not m:
            break
        token = m.group(1).replace("&amp;", "&")
    return files


NBIA = "https://services.cancerimagingarchive.net/nbia-api/services/v1/"


def tcia_series(collection):
    return get_json(NBIA + "getSeries?" + urllib.parse.urlencode({"Collection": collection}), timeout=180) or []


def tcia_files(collection):
    out = []
    for s in tcia_series(collection):
        desc = safe_name(str(s.get("SeriesDescription") or s.get("Modality") or "series"), 40)
        num = s.get("SeriesNumber", "")
        rel = "/".join([safe_name(s.get("PatientID", "unknown"), 60),
                        safe_name(str(s.get("StudyDate", ""))[:10] + "_" + s["StudyInstanceUID"][-6:], 30),
                        f"{num}_{desc}_{s['SeriesInstanceUID'][-6:]}"])
        out.append({"url": NBIA + "getImage?SeriesInstanceUID=" + s["SeriesInstanceUID"],
                    "path": rel, "size": int(s.get("FileSize") or 0), "kind": "tcia"})
    return out


def zenodo_files(rec):
    d = get_json(f"https://zenodo.org/api/records/{rec}")
    if d.get("metadata", {}).get("access_right", "open") != "open":
        raise PermissionError("Zenodo record is not open access")
    return [{"url": f["links"]["self"], "path": f["key"], "size": int(f["size"]), "kind": "file",
             "hash": f["checksum"].split(":", 1) if str(f.get("checksum", "")).startswith("md5:") else None}
            for f in d.get("files", [])]


MULTI_EXT = (".nii.gz", ".tar.gz", ".tar.bz2", ".tar.xz")


def with_id(name, fid):
    """brain_T1.nii.gz, 123 -> brain_T1_123.nii.gz (compound extensions kept whole)."""
    ext = next((e for e in MULTI_EXT if name.lower().endswith(e)), None) or os.path.splitext(name)[1]
    stem = name[:len(name) - len(ext)] if ext else name
    return f"{stem}_{fid}{name[len(stem):]}"


def figshare_article_files(aid, prefix=""):
    d = get_json(f"https://api.figshare.com/v2/articles/{aid}")
    files = [f for f in d.get("files", []) if not f.get("is_link_only")]
    # figshare articles often repeat a file name once per subject
    seen = Counter(f["name"].casefold() for f in files)
    out = []
    for f in files:
        name = with_id(f["name"], f["id"]) if seen[f["name"].casefold()] > 1 else f["name"]
        md5 = f.get("computed_md5") or f.get("supplied_md5")
        out.append({"url": f["download_url"], "path": prefix + name, "size": int(f["size"]), "kind": "file",
                    "hash": ["md5", md5] if md5 else None})
    return out


def figshare_files(spec):
    kind, fid = spec
    if kind == "article":
        return figshare_article_files(fid)
    arts, page = {}, 1
    while True:
        got = get_json(f"https://api.figshare.com/v2/collections/{fid}/articles?page_size=100&page={page}")
        if not got:
            break
        for a in got:
            arts.setdefault(a["id"], a)
        page += 1
    seen = Counter(safe_name(a["title"], 60).casefold() for a in arts.values())
    out = []
    for a in arts.values():
        pre = safe_name(a["title"], 60)
        out += figshare_article_files(a["id"], (f"{pre}_{a['id']}" if seen[pre.casefold()] > 1 else pre) + "/")
    return out


def mendeley_files(spec):
    base, did, ver = spec
    out = []

    def walk(folder_id, prefix):
        files = get_json(f"{base}/public-api/datasets/{did}/files?folder_id={folder_id}&version={ver}") or []
        if not isinstance(files, list):
            raise LookupError("Mendeley-style API returned no file list")
        for f in files:
            cd = f.get("content_details") or {}
            if cd.get("download_url"):
                out.append({"url": cd["download_url"], "path": prefix + f["filename"],
                            "size": int(f.get("size") or cd.get("size") or 0), "kind": "file",
                            "hash": ["sha256", cd["sha256_hash"]] if cd.get("sha256_hash") else None})
    walk("root", "")
    try:
        folders = get_json(f"{base}/public-api/datasets/{did}/folders/{ver}") or []
    except urllib.error.HTTPError:
        folders = []
    byid = {f["id"]: f for f in folders}

    def fpath(f):
        parts = [f["name"]]
        while f.get("parent_id") in byid:
            f = byid[f["parent_id"]]
            parts.append(f["name"])
        return "/".join(reversed(parts)) + "/"
    for f in folders:
        walk(f["id"], fpath(f))
    return out


def hf_files(repo):
    info = get_json(f"https://huggingface.co/api/datasets/{repo}")
    if info.get("gated") or info.get("private") or info.get("disabled"):
        raise PermissionError("Hugging Face dataset is gated; accept its terms on the website first")
    tree = get_json(f"https://huggingface.co/api/datasets/{repo}/tree/main?recursive=true", timeout=120)
    return [{"url": f"https://huggingface.co/datasets/{repo}/resolve/main/" + urllib.parse.quote(t["path"]),
             "path": t["path"], "size": int(t.get("size") or 0), "kind": "file",
             "hash": ["sha256", t["lfs"]["oid"]] if (t.get("lfs") or {}).get("oid") else None}
            for t in tree if t.get("type") == "file"]


def osf_files(node):
    out = []

    def walk(url, prefix):
        while url:
            d = get_json(url)
            for it in d.get("data", []):
                a = it["attributes"]
                if a["kind"] == "folder":
                    walk(it["relationships"]["files"]["links"]["related"]["href"], prefix + a["name"] + "/")
                else:
                    md5 = ((a.get("extra") or {}).get("hashes") or {}).get("md5")
                    out.append({"url": it["links"]["download"], "path": prefix + a["name"],
                                "size": int(a.get("size") or 0), "kind": "file", "hash": ["md5", md5] if md5 else None})
            url = (d.get("links") or {}).get("next")
    walk(f"https://api.osf.io/v2/nodes/{node}/files/osfstorage/", "")
    return out


RESOLVERS = {
    "openneuro": openneuro_files,
    "tcia": tcia_files,
    "zenodo": zenodo_files,
    "figshare": figshare_files,
    "mendeley": mendeley_files,
    "huggingface": hf_files,
    "osf": osf_files,
}


def resolve_manifest(dl):
    return RESOLVERS[dl["type"]](dl["arg"])
