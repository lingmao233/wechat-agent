"""Platform adapters for public browsing and guarded outbound plans."""

from .base import PlatformAdapter
from .browser import (
    BrowserPlatformAdapter,
    BrowserPlatformError,
    PlatformChallengeDetected,
    PlatformLoginRequired,
    PlatformWriteBlocked,
)
from .models import BrowseItem, PlatformAction, PlatformComment, PlatformName
from .service import PlatformService

__all__ = [
    "BrowseItem",
    "BrowserPlatformAdapter",
    "BrowserPlatformError",
    "PlatformAction",
    "PlatformAdapter",
    "PlatformChallengeDetected",
    "PlatformComment",
    "PlatformLoginRequired",
    "PlatformName",
    "PlatformService",
    "PlatformWriteBlocked",
]
