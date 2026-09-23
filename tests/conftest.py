# The unit tests drive the middleware's coroutines by hand, and Scrapy's
# deferred helpers need to know which reactor is in use. Install Twisted's
# default (non-asyncio) one. The end-to-end crawl runs in subprocesses and
# picks its own reactor there.
from twisted.internet import reactor  # noqa: F401
