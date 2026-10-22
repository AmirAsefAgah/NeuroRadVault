"""Local web app: one engine per vault, folder layout and index files, every request answered (no internet)."""
import copy
import http.client
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from vault import config, downloader
from vault import server as srv
from vault.registry import build_catalog

APP = Path(__file__).resolve().parent.parent
# the real main() in a child process, without the GitHub check, printing what it would open in the browser
CHILD = ("import sys, vault.server as s\n"
         "s.refresh_catalog = lambda *a, **k: None\n"
         "s.webbrowser.open = lambda u, *a, **k: print('BROWSER ' + u, flush=True)\n"
         "sys.exit(s.main(sys.argv[1:]))\n")


def small_catalog():
    """The bundled catalogue cut to two datasets per domain and priority (quick to lay out on disk)."""
    with tempfile.TemporaryDirectory() as cache:
        c = build_catalog(cache, offline=True)
    seen, keep = {}, []
    for d in c["datasets"]:
        k = (d["category"], d["priority"])
        if seen.get(k, 0) < 2:
            seen[k] = seen.get(k, 0) + 1
            keep.append(d)
    return {**c, "datasets": keep, "count": len(keep)}


CATALOG = small_catalog()


def call(port, method, path, body=None, raw=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)      # no proxy, whatever the system says
    try:
        c.request(method, path, body=raw if raw is not None else None if body is None else json.dumps(body),
                  headers={"X-App": "NeuroRadVault", "Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"null")
    finally:
        c.close()


def excel_open(path):
    """Hold a file the way Excel does (read/write, others may only read: no write, rename or delete).
    Returns the function that closes it."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    h = k32.CreateFileW(str(path), 0xC0000000, 1, None, 3, 0x80, None)   # GENERIC_READ|WRITE, FILE_SHARE_READ
    if h == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return lambda: k32.CloseHandle(h)


class AppCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(config, "USER_CONFIG", self.tmp / "user.json")   # never touch the real pointer file
        p.start()
        self.addCleanup(p.stop)
        srv.set_catalog(copy.deepcopy(CATALOG))
        self.addCleanup(self.close_vault)
        self.root = self.make_vault("vault")

    def make_vault(self, name):
        root = self.tmp / name
        config.write_json(root / ".vault" / "settings.json", {"auto_resume": False, "keep_awake": False})
        return root

    @staticmethod
    def close_vault():
        if srv.mgr:
            srv.mgr.close()
        config.unlock_vault(srv.vault_lock)
        for h in list(downloader.log.handlers):      # the download log keeps a file in the vault open
            h.close()
            downloader.log.removeHandler(h)
        srv.vault = srv.mgr = srv.vault_lock = None
        srv.warnings[:] = []

    def serve(self):
        httpd = srv.Server(("127.0.0.1", 0), srv.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd.server_address[1]


class OneEnginePerVaultTest(AppCase):
    def child(self, *args):
        env = {**os.environ, "NRV_CONFIG": str(self.tmp / "user.json"), "PYTHONIOENCODING": "utf-8",
               "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.Popen([sys.executable, "-u", "-c", CHILD, *args], cwd=str(APP), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace")

    def start_first(self):
        """The first window, as its own process; returns its address once it serves."""
        p = self.child("--vault", str(self.root), "--port", "0", "--no-browser")
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.wait, 10)
        self.addCleanup(p.kill)
        lines = queue.Queue()

        def pump():
            for line in p.stdout:
                lines.put(line)
            lines.put(None)
        threading.Thread(target=pump, daemon=True).start()
        out = []
        while True:
            try:
                line = lines.get(timeout=30)
            except queue.Empty:
                line = None
            if line is None:
                self.fail("the first window did not start:\n" + "".join(out))
            out.append(line)
            if " running at " in line:
                return p, line.split(" running at ")[1].strip()

    def test_second_window_on_the_same_vault_hands_over_to_the_first(self):
        first, url = self.start_first()
        second = self.child("--vault", str(self.root), "--port", "0")
        try:
            out, _ = second.communicate(timeout=30)
        finally:
            second.kill()
            second.wait()
        self.assertEqual(second.returncode, 0, out)
        self.assertIn("already open in another NeuroRadVault window", out)
        self.assertIn("BROWSER " + url, out)                        # sent to the window that has the vault
        self.assertNotIn("running at", out)

        # here too: open_vault and switching vault in the UI are refused, naming the other window
        with self.assertRaises(srv.VaultBusy) as e:
            srv.open_vault(self.root)
        self.assertEqual(str(e.exception), url)
        self.assertIsNone(srv.vault)
        code, r = call(self.serve(), "POST", "/api/setup", {"path": str(self.root)})
        self.assertEqual(code, 409)
        self.assertEqual(r["error"], f"This vault is already open in another NeuroRadVault window ({url})")

        first.kill()
        first.wait(10)
        for _ in range(50):                                          # the OS drops the lock with the process
            try:
                srv.open_vault(self.root)
                break
            except srv.VaultBusy:
                time.sleep(0.1)
        self.assertEqual(srv.vault.root, self.root.resolve())

    def test_start_up_uses_the_vaults_own_catalogue_cache(self):
        reg = self.root / ".vault" / "cache" / "registry"
        reg.mkdir(parents=True)
        (reg / "Neoplasm.csv").write_text("dataset_name,url,access_type,segmentation_mask\n"
                                          "Startup Probe One,https://example.org/one,Open,Yes\n"
                                          "Startup Probe Two,https://example.org/two,Open,No\n", encoding="utf-8")
        first, url = self.start_first()
        code, cat = call(int(url.rstrip("/").rsplit(":", 1)[1]), "GET", "/api/catalog")
        self.assertTrue(cat["registry_source"].startswith("project catalogue"), cat["registry_source"])
        self.assertEqual(sorted(d["id"] for d in cat["datasets"]), ["startup-probe-one", "startup-probe-two"])

    def test_switching_vault_releases_the_old_lock(self):
        other = self.make_vault("other")
        srv.open_vault(self.root)
        self.assertIsNone(config.lock_vault(self.root / ".vault"))   # held by this app
        srv.open_vault(self.root)                                    # re-opening the same vault keeps it
        self.assertIsNone(config.lock_vault(self.root / ".vault"))
        srv.open_vault(other)
        f = config.lock_vault(self.root / ".vault")
        self.assertIsNotNone(f)
        config.unlock_vault(f)
        self.assertIsNone(config.lock_vault(other / ".vault"))


class SetupFoldersTest(AppCase):
    def test_each_priority_folder_has_a_url_per_dataset(self):
        stray = self.root / "01_Neoplasm" / "P3_manual_download"
        stray.mkdir(parents=True)
        (stray / "Dataset_No_Longer_Listed.url").write_text("[InternetShortcut]\nURL=https://x\n")
        srv.open_vault(self.root)
        srv.setup_folders()                                          # a catalogue refresh keeps them
        want = {}
        for ds in srv.catalog["datasets"]:
            want.setdefault(srv.vault.dataset_rel(ds).rsplit("/", 1)[0], set()).add(ds["folder"] + ".url")
        for cdir in srv.vault.folders.values():
            for pdir in config.PRIORITY_FOLDERS.values():
                rel = cdir + "/" + pdir
                got = {f for f in os.listdir(srv.vault.abspath(rel)) if f.endswith(".url")}
                self.assertEqual(got, want.get(rel, set()), rel)
        ds = next(d for d in srv.catalog["datasets"] if d.get("link"))
        body = Path(srv.vault.abspath(srv.vault.dataset_rel(ds)) + ".url").read_text(encoding="utf-8")
        self.assertIn("URL=" + ds["link"], body)

    def test_files_open_in_another_program_do_not_stop_the_app(self):
        real_open = open
        about = os.path.join("01_Neoplasm", "P1_open_with_masks", "_ABOUT.txt")

        def busy_open(p, mode="r", *a, **k):
            s = os.fspath(p) if not isinstance(p, int) else ""
            if "w" in mode and ("Dataset_Index.csv" in s or about in s):
                raise PermissionError(13, "Permission denied", s)
            return real_open(p, mode, *a, **k)
        with mock.patch("builtins.open", busy_open):
            srv.open_vault(self.root)                                # start-up
            self.assertIsNotNone(srv.mgr)
            srv.setup_folders()                                      # catalogue refresh
        self.assertEqual(len(srv.warnings), 2, srv.warnings)
        self.assertIn("Dataset_Index.csv was not updated", srv.warnings[0])
        self.assertIn("1 folder note", srv.warnings[1])
        self.assertFalse((self.root / "Dataset_Index.csv").exists())
        self.assertEqual([f for f in os.listdir(self.root) if f.endswith(".tmp")], [])
        code, st = call(self.serve(), "GET", "/api/status")
        self.assertEqual((code, st["warnings"]), (200, srv.warnings))
        srv.setup_folders()                                          # closed again
        self.assertEqual(srv.warnings, [])
        self.assertTrue((self.root / "Dataset_Index.csv").exists())

    @unittest.skipUnless(os.name == "nt", "Windows share modes")
    def test_index_open_in_excel(self):
        srv.open_vault(self.root)
        idx = self.root / "Dataset_Index.csv"
        before = idx.read_bytes()
        close = excel_open(idx)
        try:
            srv.set_catalog({**CATALOG, "datasets": CATALOG["datasets"][1:]})    # the index has to change
            srv.setup_folders()
            self.assertIn("Dataset_Index.csv was not updated", " ".join(srv.warnings))
            code, r = call(self.serve(), "POST", "/api/setup", {"path": str(self.root)})   # vault (re)open
            self.assertEqual(code, 200, r)
            self.assertIsNotNone(srv.mgr)
        finally:
            close()
        self.assertEqual(idx.read_bytes(), before)
        self.assertFalse(Path(str(idx) + ".tmp").exists())
        srv.setup_folders()
        self.assertEqual(srv.warnings, [])
        self.assertNotEqual(idx.read_bytes(), before)


class LaterToggleTest(AppCase):
    def setUp(self):
        super().setUp()
        srv.open_vault(self.root)
        self.port = self.serve()
        self.ds = next(d for d in srv.catalog["datasets"] if d["category"] == "Cerebrovascular")
        dest = Path(srv.target_dir(self.ds))
        dest.mkdir(parents=True)
        (dest / "scan.nii.gz").write_bytes(b"x")
        srv.state[self.ds["id"]] = {"status": "done", "path": srv.vault.relpath(dest), "bytes": 1}
        srv.save_state()
        self.before = sorted(os.listdir(self.root))

    def assert_nothing_moved(self, code, r):
        self.assertEqual(code, 409, r)
        self.assertIn("01_Neoplasm", r["error"])
        self.assertEqual(sorted(os.listdir(self.root)), self.before)
        self.assertEqual(srv.vault.settings["later_domains"], [])
        self.assertEqual(config.read_json(srv.vault.settings_path, {})["later_domains"], [])
        self.assertEqual(srv.vault.folders["Neoplasm"], "01_Neoplasm")
        self.assertTrue(srv.state[self.ds["id"]]["path"].startswith("02_Cerebrovascular/"))
        self.assertTrue(os.path.isdir(srv.target_dir(self.ds)))

    def test_failed_rename_puts_every_folder_back(self):
        real = os.rename

        def rename(a, b):        # Neoplasm is renamed last, after the six other domains moved up
            if os.path.basename(a) == "01_Neoplasm":
                raise PermissionError(13, "Access is denied", a, None, b)
            return real(a, b)
        with mock.patch("os.rename", rename):
            self.assert_nothing_moved(*call(self.port, "POST", "/api/settings", {"later_domains": ["Neoplasm"]}))

    @unittest.skipUnless(os.name == "nt", "only Windows refuses to rename a folder holding an open file")
    def test_open_file_in_a_domain_folder(self):
        f = self.root / "01_Neoplasm" / "notes.txt"
        f.write_text("x")
        with open(f):
            self.assert_nothing_moved(*call(self.port, "POST", "/api/settings", {"later_domains": ["Neoplasm"]}))
        code, r = call(self.port, "POST", "/api/settings", {"later_domains": ["Neoplasm"]})    # closed: works
        self.assertEqual((code, r), (200, {"ok": True}))
        self.assertTrue((self.root / "07_LATER_Neoplasm" / "notes.txt").exists())
        self.assertTrue(srv.state[self.ds["id"]]["path"].startswith("01_Cerebrovascular/"))


class RequestsTest(AppCase):
    def test_bad_json_gets_400(self):
        port = self.serve()
        for raw in (b"{oops", b"[1, 2]", b'"text"'):
            code, r = call(port, "POST", "/api/download", raw=raw)
            self.assertEqual(code, 400, raw)
            self.assertIn("error", r)

    def test_unexpected_error_gets_500_with_the_message(self):
        port = self.serve()
        with mock.patch.object(srv, "open_vault", side_effect=RuntimeError("disk on fire")), \
                mock.patch("traceback.print_exc"):
            code, r = call(port, "POST", "/api/setup", {"path": str(self.root)})
        self.assertEqual(code, 500)
        self.assertIn("disk on fire", r["error"])


class CatalogueTest(AppCase):
    def test_downloads_use_the_refreshed_catalogue(self):
        srv.open_vault(self.root)
        srv.mgr._run = lambda ds_id: None                            # never download from the internet here
        new = copy.deepcopy(CATALOG)
        auto = next(d for d in new["datasets"] if d["auto"])
        new["datasets"].append({**copy.deepcopy(auto), "id": "brand-new-dataset", "folder": "Brand_New"})
        auto["downloader"] = {"type": "zenodo", "arg": "999"}        # the monthly update moved it
        srv.set_catalog(new)
        self.assertEqual(srv.mgr.get_ds(auto["id"])["downloader"], {"type": "zenodo", "arg": "999"})
        code, r = call(self.serve(), "POST", "/api/download", {"ids": ["brand-new-dataset"]})
        self.assertEqual((code, r), (200, {"queued": ["brand-new-dataset"]}))


class StateFileTest(AppCase):
    def test_damaged_state_json_falls_back_to_the_backup(self):
        sp = self.root / ".vault" / "state.json"
        config.write_json(sp, {"ds1": {"status": "done", "path": "01_Neoplasm/P1_open_with_masks/A", "bytes": 5}})
        config.write_json(sp, {}, backup=True)                       # .bak keeps the version before
        sp.write_bytes(b"\x00" * 64)                                 # what a power cut can leave on exFAT
        srv.open_vault(self.root)
        self.assertEqual(srv.state["ds1"]["status"], "done")

    def test_saving_state_keeps_a_backup(self):
        srv.open_vault(self.root)
        srv.state["ds1"] = {"status": "done"}
        srv.save_state()
        srv.save_state()
        bak = self.root / ".vault" / "state.json.bak"
        self.assertEqual(json.loads(bak.read_text(encoding="utf-8")), {"ds1": {"status": "done"}})


if __name__ == "__main__":
    unittest.main()
