# scrapy-sg-proxy

[![tests](https://github.com/Xavierfok/scrapy-sg-proxy/actions/workflows/tests.yml/badge.svg)](https://github.com/Xavierfok/scrapy-sg-proxy/actions/workflows/tests.yml)

A Scrapy downloader middleware for **mobile proxies with a rotation link**. It sends your requests through one proxy endpoint. When a site answers 403 or 429, it calls the rotation link, waits until the modem is back on a new IP, and retries the request.

I built it for [Singapore Mobile Proxy](https://singaporemobileproxy.com/?utm_source=github&utm_medium=repo&utm_campaign=scrapy_sg_proxy), which I run. It works with any provider whose rotation link changes the IP on a plain `GET`.

## Why not just a proxy list?

A mobile proxy is one `host:port` on a 4G modem. There's nothing to cycle through: you change the IP by hitting a link, the modem redials (usually 10 to 30 seconds), and the carrier may even hand back the same IP. The usual Scrapy rotating-proxy middlewares assume a pool, so they don't fit. This one does three things they don't:

- **Rotates once per block, not once per blocked response.** When a site starts blocking, every request in flight comes back 403 at the same time. Each request remembers which IP it went out on, so only the first one rotates and the rest are just retried on the new IP.
- **Waits for the new IP to actually show up** before retrying, and records when the carrier handed back the same one.
- **Doesn't block the reactor.** The redial wait runs in a thread.

The rotation logic (cooldowns, `429 Retry-After` on the rotation link, polling for the new IP) comes from [proxy-rotator](https://github.com/Xavierfok/proxy-rotator) (`pip install proxy-rotator-sg`).

## Install

```bash
pip install scrapy-sg-proxy
```

## Configure

In `settings.py`:

```python
DOWNLOADER_MIDDLEWARES = {
    # Must run before Scrapy's HttpProxyMiddleware (750).
    "scrapy_sg_proxy.SingaporeProxyMiddleware": 740,
}

SGPROXY_URL = "http://user:pass@your-proxy-host:port"
SGPROXY_ROTATE_URL = "https://your-rotation-link"
```

Singapore Mobile Proxy customers get both values from the client dashboard: the port's HTTP (or SOCKS) address with its login, and the port's rotation link.

| Setting | Default | What it does |
|---------|---------|--------------|
| `SGPROXY_URL` | required | The proxy, with credentials. |
| `SGPROXY_ROTATE_URL` | none | The rotation link. Leave it out and the middleware only sets the proxy. |
| `SGPROXY_ROTATE_ON` | `[403, 429]` | Statuses that trigger a rotation. |
| `SGPROXY_MAX_RETRIES` | `2` | Rotations tried for one request before the blocked response is passed on to your spider. |
| `SGPROXY_ROTATE_EVERY` | `0` (off) | Also rotate after this many requests. |
| `SGPROXY_MIN_INTERVAL` | `0` | Seconds to leave between rotations. Set it to your provider's limit so you wait locally instead of getting refused. |
| `SGPROXY_WAIT_FOR_NEW_IP` | `True` | Poll until the exit IP changes before retrying. |
| `SGPROXY_ROTATE_TIMEOUT` | `90` | Max seconds to wait for the new IP. |
| `SGPROXY_IP_CHECK_URL` | `https://api.ipify.org` | Returns the caller's IP as plain text. |

Per request, `meta={"dont_proxy": True}` skips the proxy, and a different `proxy` you set in `meta` is left alone.

Stats land under `sgproxy/`: `rotations`, `rotations_same_ip`, `rotation_errors`, `retries`, `gave_up`.

If you keep Scrapy's own `RetryMiddleware` on, it still handles connection errors (a request that lands mid-redial can fail that way). This middleware only acts on the statuses in `SGPROXY_ROTATE_ON`.

## About Singapore Mobile Proxy

Dedicated 4G lines on the Singtel and M1 networks in Singapore, one customer per line. There's a free 24-hour trial (10 GB), then $4 for a day, $13 for a week, or monthly from $40. [See plans](https://singaporemobileproxy.com/client/plans?days=1&utm_source=github&utm_medium=repo&utm_campaign=scrapy_sg_proxy).

## Tests

`pytest` runs unit tests plus a real Scrapy crawl through a local fake proxy that blocks until its rotation link is called. The crawl runs on the default reactor, the asyncio reactor and the select reactor, and CI covers Scrapy 2.13 and the latest release. Nothing in the suite touches a real carrier.

## Related

- [proxy-rotator](https://github.com/Xavierfok/proxy-rotator): the rotation logic, for plain `requests` code
- [langchain-singapore-proxy](https://github.com/Xavierfok/langchain-singapore-proxy): the same proxy as LangChain tools
- [singapore-proxy-mcp](https://github.com/Xavierfok/singapore-proxy-mcp): the same proxy for Claude, Cursor and other MCP clients

## License

MIT
