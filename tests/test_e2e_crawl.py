"""A real Scrapy crawl through a local fake mobile proxy.

The fake proxy blocks (403) until its rotation link has been called, and
reports a different exit IP after each rotation. The crawl runs in a
subprocess because a Twisted reactor can only start once per process.
"""

import json
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class FakeMobileProxy(BaseHTTPRequestHandler):
    state = {"generation": 0, "rotations": 0, "proxied": 0, "auth": set()}

    def log_message(self, *args):
        pass

    def _send(self, status, body):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        st = self.state
        if self.path == "/rotate":  # the rotation link, called directly
            st["rotations"] += 1
            st["generation"] += 1
            return self._send(200, "rotated")
        # Everything else arrives as a proxied absolute-URI request.
        st["proxied"] += 1
        st["auth"].add(self.headers.get("Proxy-Authorization", ""))
        if self.path.startswith("http://ipcheck.test"):
            return self._send(200, f"10.0.0.{st['generation']}")
        if st["generation"] == 0:
            return self._send(403, "blocked")
        return self._send(200, f"page {self.path} via gen {st['generation']}")


@pytest.fixture
def proxy_server():
    FakeMobileProxy.state = {"generation": 0, "rotations": 0, "proxied": 0, "auth": set()}
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMobileProxy)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()


SPIDER = textwrap.dedent(
    """
    import json, sys
    import scrapy
    from scrapy.crawler import CrawlerProcess

    port, out, reactor = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    results = []

    class S(scrapy.Spider):
        name = "s"
        start_urls = [f"http://target.test/{i}" for i in range(4)]

        def parse(self, response):
            results.append({"url": response.url, "status": response.status, "body": response.text})

    process = CrawlerProcess(settings={
        "LOG_LEVEL": "WARNING",
        "HTTPERROR_ALLOW_ALL": True,
        "RETRY_ENABLED": False,
        "DOWNLOADER_MIDDLEWARES": {"scrapy_sg_proxy.SingaporeProxyMiddleware": 740},
        "SGPROXY_URL": f"http://user:secret@127.0.0.1:{port}",
        "SGPROXY_ROTATE_URL": f"http://127.0.0.1:{port}/rotate",
        "SGPROXY_IP_CHECK_URL": "http://ipcheck.test/",
        "SGPROXY_ROTATE_TIMEOUT": 10,
        **({"TWISTED_REACTOR": reactor} if reactor != "default" else {}),
    })
    crawler = process.create_crawler(S)
    process.crawl(crawler)
    process.start()
    stats = {k: v for k, v in crawler.stats.get_stats().items() if k.startswith("sgproxy/")}
    json.dump({"results": results, "stats": stats}, open(out, "w"))
    """
)


@pytest.mark.parametrize(
    "reactor",
    ["default", "twisted.internet.asyncioreactor.AsyncioSelectorReactor", "twisted.internet.selectreactor.SelectReactor"],
)
def test_crawl_rotates_once_and_recovers(proxy_server, tmp_path, reactor):
    script = tmp_path / "crawl.py"
    script.write_text(SPIDER)
    out = tmp_path / "out.json"
    port = proxy_server.server_address[1]
    proc = subprocess.run(
        [sys.executable, str(script), str(port), str(out), reactor],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(out.read_text())

    statuses = sorted(r["status"] for r in data["results"])
    assert statuses == [200, 200, 200, 200], data
    assert all("via gen 1" in r["body"] for r in data["results"])
    st = FakeMobileProxy.state
    assert st["rotations"] == 1  # four blocked requests, one rotation
    assert data["stats"]["sgproxy/rotations"] == 1
    assert data["stats"].get("sgproxy/rotations_same_ip") is None
    # Scrapy turned the user:pass in the proxy URL into a Proxy-Authorization header.
    assert st["auth"] == {"Basic dXNlcjpzZWNyZXQ="}
