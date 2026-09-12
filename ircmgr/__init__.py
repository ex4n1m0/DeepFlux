"""DeepFlux Room subsystem (serverless community chat).

Named ``ircmgr`` for historical reasons: it once hosted an embedded IRC
client, and the room still renders through the same IRCState shape.
"""
from ircmgr.state import ChannelState, ChatMessage, IRCState, NetworkState

__all__ = [
    "ChannelState",
    "ChatMessage",
    "IRCState",
    "NetworkState",
]
