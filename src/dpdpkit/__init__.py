"""dpdpkit — developer primitives for India's DPDP Act, 2023 and DPDP Rules, 2025.

dpdpkit helps you implement obligations; it does not provide legal advice or guarantee compliance.
"""

# Adapters ship subpackages (``dpdpkit.fastapi``, ``dpdpkit.django``) from separate distributions.
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)

__version__ = "0.1.0.dev0"

from .config import kit_from_config, load_config, sync_notice
from .errors import (
    ConfigError,
    ConsentRequired,
    DpdpkitError,
    InvalidTransition,
    LedgerTampered,
    NotFound,
    NoticeInvalid,
    PolicyError,
    ValidationFailed,
)
from .kit import ContactResolver, Kit
from .models import (
    ConsentStatus,
    Contact,
    DataItem,
    LegalBasis,
    Processor,
    Purpose,
    RequestKind,
    RequestStatus,
    RetentionRule,
    RetentionTrigger,
    Role,
    ScheduleStatus,
)
from .notify import ConsoleNotifier, MemoryNotifier, Notifier, NullNotifier, SmtpNotifier
from .policy import Policy
from .registry import Registry
from .repository import InMemoryRepository, Repository

__all__ = [
    "ConfigError",
    "ConsentRequired",
    "ConsentStatus",
    "ConsoleNotifier",
    "Contact",
    "ContactResolver",
    "DataItem",
    "DpdpkitError",
    "InMemoryRepository",
    "InvalidTransition",
    "Kit",
    "LedgerTampered",
    "LegalBasis",
    "MemoryNotifier",
    "NotFound",
    "NoticeInvalid",
    "Notifier",
    "NullNotifier",
    "Policy",
    "PolicyError",
    "Processor",
    "Purpose",
    "Registry",
    "Repository",
    "RequestKind",
    "RequestStatus",
    "RetentionRule",
    "RetentionTrigger",
    "Role",
    "ScheduleStatus",
    "SmtpNotifier",
    "ValidationFailed",
    "__version__",
    "kit_from_config",
    "load_config",
    "sync_notice",
]
