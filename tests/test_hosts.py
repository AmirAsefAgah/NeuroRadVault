"""Host helpers: file transfers take the same proxy as listings; Figshare names never collide on disk."""
import base64
import http.server
import os
import threading
import unittest
from unittest import mock

from vault import hosts

seen = []          # (method, path, headers, client port) as the proxy got them


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    """Forwarding proxy stand-in: answers plain-http requests itself, records CONNECT and refuses it."""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        seen.append(("GET", self.path, dict(self.headers), self.client_address[1]))
        if self.path.endswith("/hop"):        # send the client to a host it must reach directly
            self.send_response(302)
            self.send_header("Location", self.server.direct + "/x")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b"via proxy"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self):
        seen.append(("CONNECT", self.path, dict(self.headers), self.client_address[1]))
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.close_connection = True


class DirectHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b"direct"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(handler):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def reset_conns():
    for c, _ in hosts._tls.__dict__.get("conns", {}).values():
        c.close()
    hosts._tls.__dict__.clear()


class ProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.direct = serve(DirectHandler)
        cls.proxy = serve(ProxyHandler)
        cls.proxy.direct = f"http://127.0.0.1:{cls.direct.server_address[1]}"
        cls.px = f"127.0.0.1:{cls.proxy.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        for s in (cls.direct, cls.proxy):
            s.shutdown()
            s.server_close()

    def setUp(self):
        env = mock.patch.dict(os.environ)     # restores every variable afterwards
        env.start()
        self.addCleanup(env.stop)
        for k in [k for k in os.environ if k.lower().endswith("_proxy")]:
            del os.environ[k]
        reset_conns()
        self.addCleanup(reset_conns)
        seen.clear()

    def get(self, url):
        r = hosts.open_stream(url, timeout=10)
        return r.read()

    def test_http_file_goes_through_the_proxy_with_credentials(self):
        os.environ["HTTP_PROXY"] = f"http://user:p%40ss@{self.px}"
        url = "http://files.only-via-proxy.invalid/sub/x.bin?a=1"
        self.assertEqual(self.get(url), b"via proxy")
        self.assertEqual(self.get(url), b"via proxy")
        (m1, p1, h1, port1), (_, p2, _, port2) = seen
        self.assertEqual((m1, p1, p2), ("GET", url, url))           # absolute URL, as proxies expect
        self.assertEqual(h1["Host"], "files.only-via-proxy.invalid")
        self.assertEqual(h1["Proxy-Authorization"], "Basic " + base64.b64encode(b"user:p@ss").decode())
        self.assertEqual(port1, port2)                                # keep-alive to the proxy
        hosts.drop_conn(url)
        self.assertEqual(hosts._tls.conns, {})
        self.get(url)
        self.assertNotEqual(seen[-1][3], port1)                       # dropped -> a fresh connection

    def test_https_file_tunnels_through_the_proxy(self):
        os.environ["HTTPS_PROXY"] = f"http://u:pw@{self.px}"
        with self.assertRaises(OSError):                              # our stand-in refuses the tunnel
            hosts.open_stream("https://secure.only-via-proxy.invalid:8443/f.bin", timeout=10)
        self.assertTrue(seen)
        method, path, h, _ = seen[0]
        self.assertEqual((method, path), ("CONNECT", "secure.only-via-proxy.invalid:8443"))
        self.assertEqual(h["Proxy-Authorization"], "Basic " + base64.b64encode(b"u:pw").decode())

    def test_no_proxy_and_unusable_proxies_go_direct(self):
        os.environ["HTTP_PROXY"] = os.environ["HTTPS_PROXY"] = f"http://{self.px}"
        os.environ["NO_PROXY"] = "*"
        self.assertIsNone(hosts.proxy_for("https", "example.org"))
        self.assertEqual(self.get(self.proxy.direct + "/x"), b"direct")
        self.assertEqual(seen, [])
        del os.environ["NO_PROXY"]
        self.assertEqual(hosts.proxy_for("https", "example.org")[1:3], ("127.0.0.1", self.proxy.server_address[1]))
        os.environ["HTTPS_PROXY"] = "socks5://127.0.0.1:1080"          # urllib can't use it either
        self.assertIsNone(hosts.proxy_for("https", "example.org"))

    def test_redirect_to_another_host_rechecks_the_proxy(self):
        os.environ["HTTP_PROXY"] = f"http://{self.px}"
        os.environ["NO_PROXY"] = "127.0.0.1"
        url = "http://files.only-via-proxy.invalid/hop"
        self.assertEqual(self.get(url), b"direct")                    # first hop proxied, second direct
        self.assertEqual([s[1] for s in seen], [url])
        self.assertEqual(len(hosts._tls.conns), 2)
        hosts.drop_conn(url)                                          # also drops the host the body came from
        self.assertEqual(hosts._tls.conns, {})


ARTICLE = {"files": [
    {"id": 11, "name": "brain_T1.nii.gz", "size": 1, "download_url": "u11", "computed_md5": "a" * 32},
    {"id": 12, "name": "brain_T1.nii.gz", "size": 2, "download_url": "u12", "computed_md5": "b" * 32},
    {"id": 13, "name": "Brain_T1.NII.GZ", "size": 3, "download_url": "u13"},
    {"id": 14, "name": "notes.txt", "size": 4, "download_url": "u14"},
    {"id": 15, "name": "README", "size": 5, "download_url": "u15"},
    {"id": 16, "name": "README", "size": 6, "download_url": "u16"},
    {"id": 17, "name": "notes.txt", "size": 7, "download_url": "u17", "is_link_only": True},
]}


def fake_figshare(url, **kw):
    if "/collections/" in url:
        page = int(url.rsplit("page=", 1)[1])
        return {1: [{"id": 101, "title": "Same  title"}, {"id": 102, "title": "Other"}],
                2: [{"id": 103, "title": "same title"}, {"id": 101, "title": "Same  title"}]}.get(page, [])
    return ARTICLE if url.endswith("/articles/7") else {"files": [{"id": 1, "name": "a.csv", "size": 1, "download_url": "x"}]}


class FigshareTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(hosts, "get_json", fake_figshare)
        p.start()
        self.addCleanup(p.stop)

    def test_repeated_names_get_the_file_id(self):
        man = hosts.figshare_files(["article", "7"])
        self.assertEqual([m["path"] for m in man], ["brain_T1_11.nii.gz", "brain_T1_12.nii.gz", "Brain_T1_13.NII.GZ",
                                                   "notes.txt", "README_15", "README_16"])
        self.assertEqual(man[0]["hash"], ["md5", "a" * 32])
        self.assertEqual(man, hosts.figshare_files(["article", "7"]))  # deterministic

    def test_compound_extensions_stay_whole(self):
        for name, want in [("brain_T1.nii.gz", "brain_T1_9.nii.gz"), ("a.tar.bz2", "a_9.tar.bz2"),
                           ("b.TAR.XZ", "b_9.TAR.XZ"), ("c.tar.gz", "c_9.tar.gz"), ("d.gz", "d_9.gz"),
                           ("e.csv", "e_9.csv"), ("f", "f_9")]:
            self.assertEqual(hosts.with_id(name, 9), want)

    def test_collection_articles_with_the_same_title_get_their_id(self):
        man = hosts.figshare_files(["collection", "5"])
        self.assertEqual([m["path"] for m in man], ["Same_title_101/a.csv", "Other/a.csv", "same_title_103/a.csv"])


if __name__ == "__main__":
    unittest.main()
