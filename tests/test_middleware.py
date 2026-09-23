import pytest
from proxy_rotator import RotationError, RotationResult
from scrapy.exceptions import NotConfigured
from scrapy.http import Request, Response
from scrapy.utils.test import get_crawler
from twisted.internet import defer

from scrapy_sg_proxy import SingaporeProxyMiddleware

PROXY = "http://user:pass@sg.example.com:8001"


class FakeProxy:
    proxy_url = PROXY

    def __init__(self, results=None):
        self.calls = 0
        self.results = list(results or [])

    def rotate(self, wait_for_new_ip=True, timeout=90):
        self.calls += 1
        if self.results:
            r = self.results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return RotationResult(old_ip=f"1.1.1.{self.calls}", new_ip=f"1.1.1.{self.calls + 1}", changed=True, waited=0)


def run_now(func, *args, **kwargs):
    # Stand-in for deferToThread: run inline, hand back a fired Deferred.
    try:
        return defer.succeed(func(*args, **kwargs))
    except Exception:
        return defer.fail()


def run(coro):
    """Drive a coroutine that never really waits (fakes fire synchronously)."""
    d = defer.ensureDeferred(coro)
    out = []
    d.addBoth(out.append)
    assert out, "coroutine did not finish synchronously"
    if isinstance(out[0], Exception) or hasattr(out[0], "raiseException"):
        out[0].raiseException()
    return out[0]


def make_mw(proxy=None, **kw):
    crawler = get_crawler()
    return SingaporeProxyMiddleware(proxy or FakeProxy(), stats=crawler.stats, run_in_thread=run_now, **kw), crawler.stats


def send(mw, url="https://example.com/", **meta):
    req = Request(url, meta=meta)
    assert run(mw.process_request(req)) is None
    return req


def test_sets_proxy_and_generation():
    mw, _ = make_mw()
    req = send(mw)
    assert req.meta["proxy"] == PROXY
    assert req.meta["sgproxy_generation"] == 0


def test_dont_proxy_and_foreign_proxy_left_alone():
    mw, _ = make_mw()
    assert "proxy" not in send(mw, dont_proxy=True).meta
    assert send(mw, proxy="http://other:1").meta["proxy"] == "http://other:1"
    assert send(mw, proxy=None).meta["proxy"] is None


def test_block_rotates_and_retries():
    proxy = FakeProxy()
    mw, stats = make_mw(proxy)
    req = send(mw)
    out = run(mw.process_response(req, Response(req.url, status=403)))
    assert isinstance(out, Request)
    assert out.dont_filter
    assert out.meta["sgproxy_retries"] == 1
    assert "proxy" not in out.meta
    assert proxy.calls == 1
    assert mw.generation == 1
    assert stats.get_value("sgproxy/rotations") == 1
    # The retry goes out on the new generation.
    assert run(mw.process_request(out)) is None
    assert out.meta["sgproxy_generation"] == 1


def test_ok_response_passes_through():
    proxy = FakeProxy()
    mw, _ = make_mw(proxy)
    req = send(mw)
    resp = Response(req.url, status=200)
    assert run(mw.process_response(req, resp)) is resp
    assert proxy.calls == 0


def test_many_blocked_responses_rotate_once():
    proxy = FakeProxy()
    mw, _ = make_mw(proxy)
    reqs = [send(mw, url=f"https://example.com/{i}") for i in range(5)]
    outs = [run(mw.process_response(r, Response(r.url, status=429))) for r in reqs]
    assert all(isinstance(o, Request) for o in outs)
    assert proxy.calls == 1
    assert mw.generation == 1


def test_gives_up_after_max_retries():
    proxy = FakeProxy()
    mw, stats = make_mw(proxy, max_retries=1)
    req = send(mw)
    retry = run(mw.process_response(req, Response(req.url, status=403)))
    run(mw.process_request(retry))
    resp = Response(retry.url, status=403)
    assert run(mw.process_response(retry, resp)) is resp
    assert proxy.calls == 1
    assert stats.get_value("sgproxy/gave_up") == 1


def test_rotation_error_still_retries_without_bumping_generation():
    proxy = FakeProxy(results=[RotationError("rotation link returned HTTP 410", 410, "gone")])
    mw, stats = make_mw(proxy)
    req = send(mw)
    out = run(mw.process_response(req, Response(req.url, status=403)))
    assert isinstance(out, Request)
    assert mw.generation == 0
    assert stats.get_value("sgproxy/rotation_errors") == 1


def test_same_ip_is_counted():
    proxy = FakeProxy(results=[RotationResult("9.9.9.9", "9.9.9.9", False, 0)])
    mw, stats = make_mw(proxy)
    req = send(mw)
    run(mw.process_response(req, Response(req.url, status=403)))
    assert stats.get_value("sgproxy/rotations_same_ip") == 1


def test_rotate_every_n_requests():
    proxy = FakeProxy()
    mw, _ = make_mw(proxy, rotate_every=3)
    gens = [send(mw, url=f"https://example.com/{i}").meta["sgproxy_generation"] for i in range(7)]
    assert gens == [0, 0, 0, 1, 1, 1, 2]
    assert proxy.calls == 2


def test_no_rotate_url_means_proxy_only():
    proxy = FakeProxy()
    mw, _ = make_mw(proxy, can_rotate=False)
    req = send(mw)
    resp = Response(req.url, status=403)
    assert run(mw.process_response(req, resp)) is resp
    assert proxy.calls == 0


def test_from_crawler_reads_settings():
    crawler = get_crawler(
        settings_dict={
            "SGPROXY_URL": PROXY,
            "SGPROXY_ROTATE_URL": "https://rotate.example.com/abc",
            "SGPROXY_ROTATE_ON": [403, 503],
            "SGPROXY_MAX_RETRIES": 4,
            "SGPROXY_MIN_INTERVAL": 30,
        }
    )
    mw = SingaporeProxyMiddleware.from_crawler(crawler)
    assert mw.proxy.proxy_url == PROXY
    assert mw.proxy.min_interval == 30
    assert mw.rotate_on == {403, 503}
    assert mw.max_retries == 4
    assert mw.can_rotate


def test_from_crawler_requires_proxy_url():
    with pytest.raises(NotConfigured):
        SingaporeProxyMiddleware.from_crawler(get_crawler())
