"""Scrapy middleware for Singapore Mobile Proxy (or any mobile proxy with a rotation link)."""

from .middleware import SingaporeProxyMiddleware

__version__ = "0.1.0"

__all__ = ["SingaporeProxyMiddleware"]
