"""Native messaging package for Chrome extension integration."""
from __future__ import annotations

from .host import NativeMessagingHost
from .register import register_native_host, unregister_native_host

__all__ = ["NativeMessagingHost", "register_native_host", "unregister_native_host"]
