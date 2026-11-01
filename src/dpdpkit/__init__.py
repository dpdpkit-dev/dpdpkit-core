"""dpdpkit — developer primitives for India's DPDP Act, 2023 and DPDP Rules, 2025.

dpdpkit helps you implement obligations; it does not provide legal advice or guarantee compliance.
"""

# Adapters ship subpackages (``dpdpkit.fastapi``, ``dpdpkit.django``) from separate distributions.
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)

__version__ = "0.1.0a1"

from .config import kit_from_config, load_config, sync_notice
from .deprecation import DpdpkitDeprecationWarning
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
from .incidents import IncidentService
from .kit import ContactResolver, Kit
from .ledger import FileRootSink, RootSink
from .models import (
    ClockState,
    ConsentStatus,
    Contact,
    DataItem,
    DeliveryStatus,
    Incident,
    IncidentClock,
    IncidentReport,
    IncidentSeverity,
    IncidentStatus,
    LegalBasis,
    NoticeStatus,
    PrincipalNotice,
    Processor,
    Purpose,
    ReportKind,
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
from .repository import IncidentRepository, InMemoryRepository, Repository

__all__ = [
    "ClockState",
    "ConfigError",
    "ConsentRequired",
    "ConsentStatus",
    "ConsoleNotifier",
    "Contact",
    "ContactResolver",
    "DataItem",
    "DeliveryStatus",
    "DpdpkitDeprecationWarning",
    "DpdpkitError",
    "FileRootSink",
    "InMemoryRepository",
    "Incident",
    "IncidentClock",
    "IncidentReport",
    "IncidentRepository",
    "IncidentService",
    "IncidentSeverity",
    "IncidentStatus",
    "InvalidTransition",
    "Kit",
    "LedgerTampered",
    "LegalBasis",
    "MemoryNotifier",
    "NotFound",
    "NoticeInvalid",
    "NoticeStatus",
    "Notifier",
    "NullNotifier",
    "Policy",
    "PolicyError",
    "PrincipalNotice",
    "Processor",
    "Purpose",
    "Registry",
    "ReportKind",
    "Repository",
    "RequestKind",
    "RequestStatus",
    "RetentionRule",
    "RetentionTrigger",
    "Role",
    "RootSink",
    "ScheduleStatus",
    "SmtpNotifier",
    "ValidationFailed",
    "__version__",
    "kit_from_config",
    "load_config",
    "sync_notice",
]
