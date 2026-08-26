"""Embedded IRC client subsystem for DeepFlux.

Named ``ircmgr`` (mirroring ``dlmgr``) because the PyPI dependency that
implements the wire protocol is itself named ``irc`` — a local ``irc/``
package would shadow it.
"""
from ircmgr.state import ChannelState, ChatMessage, IRCState, NetworkState
from ircmgr.client import IRCClientCore

__all__ = [
    "ChannelState",
    "ChatMessage",
    "IRCState",
    "NetworkState",
    "IRCClientCore",
]
