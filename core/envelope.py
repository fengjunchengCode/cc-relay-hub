from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class RelayEnvelope:
    request_id: str
    sender: str
    target: str
    body: str
    created_at: float
    reply_to: Optional[str]
    ttl: int = 300
    expect_reply: bool = True

    def __post_init__(self):
        if not isinstance(self.body, str) or not self.body.strip():
            raise ValueError("relay message body must not be empty or whitespace-only")


@dataclass
class DeliveryReceipt:
    request_id: str
    status: str
    provider: str
    delivered_at: float
    error: Optional[str] = None


@dataclass
class RelayEvent:
    event_type: str
    request_id: Optional[str]
    agent_id: str
    content: str
    timestamp: float
    session_key: Optional[str] = None


@dataclass
class ProviderHealth:
    provider_status: str
    agent_status: str
    last_seen_at: Optional[float]
    last_delivery_at: Optional[float]
    details: str = ""


@dataclass
class CommandResult:
    status: str
    executed_at: float
    error: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
