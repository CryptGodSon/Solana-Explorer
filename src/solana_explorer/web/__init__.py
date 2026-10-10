"""Browser-based block explorer (``solana-explorer web``)."""

from .server import ExplorerWebApp, serve

__all__ = ["ExplorerWebApp", "serve"]
