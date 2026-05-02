"""Contracts re-export shim for the Iran VPS service (Track B, Step 2).

Imports the frozen Pydantic models, encode/decode helpers, and S2 key helpers
from ``kharej.contracts`` so that Iran-side code uses the *identical* types
without duplicating any logic.

Usage::

    from iran.contracts import JobCreate, encode, decode
    # …identical to importing from kharej.contracts, but scoped to iran/

See Also
--------
- Source of truth: ``kharej/contracts.py``
- Spec: ``docs/research/arvan-webui-migration/message-schema.md``
- Human overview: ``docs/research/arvan-webui-migration/CONTRACTS.md``
"""

from __future__ import annotations

from kharej.contracts import (
    CONTRACT_VERSION,
    MAX_MESSAGE_BYTES,
    RTUNES_PREFIX,
    AccessDecision,
    AdminAck,
    AdminClearcache,
    AdminCookiesUpdate,
    AdminSettingsUpdate,
    AnyMessage,
    CircuitBreakerState,
    Envelope,
    HealthPing,
    HealthPong,
    JobAccepted,
    JobCancel,
    JobCompleted,
    JobCreate,
    JobFailed,
    JobProgress,
    JobStatus,
    Platform,
    ProviderStatus,
    S2ObjectRef,
    UserBlockAdd,
    UserBlockRemove,
    UserWhitelistAdd,
    UserWhitelistRemove,
    decode,
    encode,
    make_media_key,
    make_part_key,
    make_thumb_key,
    make_tmp_prefix,
)

__all__ = [
    # Version / routing constants
    "CONTRACT_VERSION",
    "MAX_MESSAGE_BYTES",
    "RTUNES_PREFIX",
    # Enums
    "AccessDecision",
    "JobStatus",
    "Platform",
    # Sub-models
    "CircuitBreakerState",
    "Envelope",
    "ProviderStatus",
    "S2ObjectRef",
    # Job messages
    "JobAccepted",
    "JobCancel",
    "JobCompleted",
    "JobCreate",
    "JobFailed",
    "JobProgress",
    # User whitelist/block messages
    "UserBlockAdd",
    "UserBlockRemove",
    "UserWhitelistAdd",
    "UserWhitelistRemove",
    # Admin messages
    "AdminAck",
    "AdminClearcache",
    "AdminCookiesUpdate",
    "AdminSettingsUpdate",
    # Health messages
    "HealthPing",
    "HealthPong",
    # Discriminated union
    "AnyMessage",
    # Encode / decode helpers
    "decode",
    "encode",
    # S2 key helpers
    "make_media_key",
    "make_part_key",
    "make_thumb_key",
    "make_tmp_prefix",
]
