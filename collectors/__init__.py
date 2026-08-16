"""Apollo collectors — one module per data source family."""
from .base import Collector, HttpClient, register, REGISTRY  # noqa: F401
from . import rss            # noqa: F401
from . import gdelt          # noqa: F401
from . import market         # noqa: F401
from . import government     # noqa: F401
from . import equities       # noqa: F401
from . import keyed          # noqa: F401

__all__ = ["Collector", "HttpClient", "register", "REGISTRY"]
