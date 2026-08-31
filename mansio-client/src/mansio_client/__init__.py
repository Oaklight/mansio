"""mansio-client — lightweight agent SDK for mansio message bus."""

__version__ = "0.1.0"

from mansio_client.client import MansioClient
from mansio_client.federation import FederationLink  # experimental, see federation.py
from mansio_client.injectors import (
    ClaudeCodeInjector,
    Injector,
    MailboxInjector,
    OpenClawInjector,
    WebhookInjector,
)
from mansio_client.transport import HttpTransport, MansioAPIError
from mansio_client.types import ClaimResult, Message, UserPresence

__all__ = [
    "ClaimResult",
    "ClaudeCodeInjector",
    "FederationLink",
    "HttpTransport",
    "Injector",
    "MailboxInjector",
    "MansioAPIError",
    "MansioClient",
    "Message",
    "OpenClawInjector",
    "UserPresence",
    "WebhookInjector",
]
