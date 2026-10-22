"""Download engine against a deliberately unreliable local HTTP server (no internet needed)."""
import hashlib
import http.server
import json
import os
import random
import shutil
import tempfile
import threading
import time
import unittest

from vault import downloader
from vault.config import Vault

BIG = random.Random(1).randbytes(80 * 2**20)
SMALL = {f"s{i}.bin": random.Random(i + 10).randbytes(50_000) for i in range(12)}
FILES = {"big.bin": BIG, **SMALL, "bad.bin": b"x" * 5000}
DROP_AFTER = 9 * 2**20      # the server cuts every long transfer here
hits = {"n": 0}


class FlakyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        hits["n"] += 1
        if hits["n"] == 1:                       # the very first request fails with a server error
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        data = FILES[self.path.strip("/")]
        start, end = 0, len(data) - 1
        rng = self.headers.get("Range")
        if rng:
            a, b = rng.split("=")[1].split("-")
            start, end = int(a), int(b) if b else len(data) - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        else:
            self.send_response(200)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        body = data[start:end + 1]
        try:
            if len(body) > DROP_AFTER:
                self.wfile.write(body[:DROP_AFTER])
                self.wfile.flush()
                self.close_connection = True
                self.connection.shutdown(2)
                return
            self.wfile.write(body)
        except OSError:
            pass


class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FlakyHandler)
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.mkdtemp()
        cls.sets = {"t1": list(FILES), "t2": ["big.bin"], "t3": ["s1.bin"]}
        cls.base = f"http://127.0.0.1:{cls.port}/"
        downloader.resolve_manifest = cls.manifest
        cls.ds = {k: {"id": k, "auto": True, "downloader": {"type": "test", "arg": k}, "size_bytes": 0, "fields": {}}
                  for k in cls.sets}

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def manifest(cls, dl):
        return [{"url": cls.base + n, "path": f"sub/{n}", "size": len(FILES[n]), "kind": "file",
                 "hash": ["md5", hashlib.md5(FILES[n]).hexdigest() if n != "bad.bin" else "0" * 32]}
                for n in cls.sets[dl["arg"]]]

    def manager(self, settings=None):
        v = Vault(self.tmp)
        v.create()
        self.settings = settings or {"max_parallel": 1, "segments": 4, "speed_limit_mbps": 0, "auto_resume": True,
                                     "keep_awake": False}
        self.state = {}
        return downloader.Manager(v, self.ds.get, lambda d: os.path.join(self.tmp, d["id"]), self.state, lambda: None,
                                  self.settings)

    def wait(self, m, ds_id, statuses, timeout=120):
        t = time.time()
        while time.time() - t < timeout:
            j = m.snapshot()["jobs"].get(ds_id)
            if j and j["status"] in statuses:
                return j
            time.sleep(0.3)
        self.fail(f"timeout waiting for {ds_id}: {m.snapshot()['jobs'].get(ds_id)}")

    def md5(self, *parts):
        with open(os.path.join(self.tmp, *parts), "rb") as f:
            return hashlib.md5(f.read()).hexdigest()

    def test_1_flaky_server_segments_and_checksums(self):
        m = self.manager()
        m.enqueue(["t1"])
        j = self.wait(m, "t1", ("done", "error"))
        m.close()
        self.assertEqual(self.md5("t1", "sub", "big.bin"), hashlib.md5(BIG).hexdigest())
        for n, d in SMALL.items():
            self.assertEqual(self.md5("t1", "sub", n), hashlib.md5(d).hexdigest())
        self.assertEqual(j["status"], "error")                      # only because of bad.bin
        self.assertEqual(len(j["failed"]), 1)
        self.assertIn("checksum", j["failed"][0])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "t1", "sub", "bad.bin")))

    def test_2_pause_restart_resume(self):
        m = self.manager({"max_parallel": 1, "segments": 4, "speed_limit_mbps": 8, "auto_resume": True, "keep_awake": False})
        m.enqueue(["t2"])
        self.wait(m, "t2", ("downloading",))
        time.sleep(2.5)
        m.pause(["t2"])
        self.wait(m, "t2", ("paused",))
        m.close()
        meta = os.path.join(self.tmp, "t2", "sub", "big.bin.part.json")
        with open(meta) as f:
            saved = sum(s[2] - s[0] for s in json.load(f)["segments"])
        self.assertGreater(saved, 2 * 2**20)
        self.assertLess(saved, len(BIG))
        with open(Vault(self.tmp).queue_path) as f:      # simulate a crash: the queue says "queued"
            q = json.load(f)
        for it in q:
            it["status"] = "queued"
        with open(Vault(self.tmp).queue_path, "w") as f:
            json.dump(q, f)
        m2 = self.manager()
        j = self.wait(m2, "t2", ("done", "error"))
        m2.close()
        self.assertEqual(j["status"], "done")
        self.assertEqual(self.md5("t2", "sub", "big.bin"), hashlib.md5(BIG).hexdigest())
        self.assertFalse(os.path.exists(meta))

    def test_3_unreachable_host_waits_then_recovers(self):
        good = self.base
        type(self).base = "http://127.0.0.1:9/"           # nothing listens there
        m = self.manager()
        m.enqueue(["t3"])
        time.sleep(5)
        j = m.snapshot()["jobs"]["t3"]
        self.assertEqual(j["status"], "downloading")
        self.assertIn("waiting", j["note"])
        m.pause(["t3"])
        self.wait(m, "t3", ("paused",))
        type(self).base = good
        os.remove(os.path.join(Vault(self.tmp).manifests, "t3.json"))   # URLs changed in this test only
        m.resume(["t3"])
        j = self.wait(m, "t3", ("done", "error"))
        m.close()
        self.assertEqual(j["status"], "done")


if __name__ == "__main__":
    unittest.main()
