"""The :class:`Kit` facade: one object wiring policy, registry, repository, notifier and services."""

from __future__ import annotations

import copy
import datetime as dt
import secrets
import warnings
from collections.abc import Callable, Iterable, Mapping

from ._util import Clock, as_utc, utcnow
from .consent import ConsentService
from .errors import ConfigError, PolicyError
from .events import EventBus, WebhookDispatcher
from .export import ExportService
from .ledger import Ledger
from .models import DEFAULT_TENANT, Contact
from .notices import NoticeService
from .notify import Notifier, NullNotifier
from .policy import Policy
from .registry import Registry
from .repository import Repository
from .retention import RetentionService
from .rights import IdentityVerifier, RightsService

ContactResolver = Callable[[str], Contact | None]
"""Maps a principal reference to contact details for notifications. Core never stores them."""


class Kit:
    """Entry point to dpdpkit.

    >>> kit = Kit(repository=InMemoryRepository(), policy="dpdp-rules-2025.v1")  # doctest: +SKIP
    >>> kit.consent.grant("u_42", "marketing")                                     # doctest: +SKIP
    """

    def __init__(
        self,
        repository: Repository,
        policy: Policy | str,
        *,
        registry: Registry | None = None,
        notifier: Notifier | None = None,
        overlays: Iterable[str] = (),
        tenant_id: str = DEFAULT_TENANT,
        signing_key: bytes | str | None = None,
        contact: Mapping[str, str] | None = None,
        contact_resolver: ContactResolver | None = None,
        identity_verifier: IdentityVerifier | None = None,
        clock: Clock | None = None,
        events: EventBus | None = None,
        webhooks: WebhookDispatcher | None = None,
    ) -> None:
        if isinstance(policy, str):
            policy = Policy.load(policy, overlays)
        elif list(overlays):
            raise PolicyError("pass overlays when loading by id, or merge them into the Policy yourself")
        if not isinstance(policy, Policy):
            raise PolicyError("a valid policy pack is required; dpdpkit does not run with defaults")
        if not isinstance(repository, Repository):
            raise ConfigError("repository does not implement dpdpkit.repository.Repository")

        self.repository = repository
        self.policy = policy
        self.registry = registry or Registry()
        self.notifier: Notifier = notifier or NullNotifier()
        self.tenant_id = tenant_id
        self.contact: dict[str, str] = dict(contact or {})
        self.contact_resolver = contact_resolver
        self.identity_verifier = identity_verifier
        self._clock = clock or utcnow
        self.events = events or EventBus()
        self.webhooks = webhooks
        if webhooks is not None:
            webhooks.attach(self.events)

        if signing_key is None:
            warnings.warn(
                "dpdpkit: no signing_key given; using an ephemeral key. Consent receipts will not verify "
                "after a restart. Pass a stable secret in production.",
                RuntimeWarning,
                stacklevel=2,
            )
            signing_key = secrets.token_bytes(32)
        self.signing_key = signing_key.encode() if isinstance(signing_key, str) else signing_key

        self._bind()

    def _bind(self) -> None:
        self.ledger = Ledger(self)
        self.notices = NoticeService(self)
        self.consent = ConsentService(self)
        self.rights = RightsService(self)
        self.retention = RetentionService(self)
        self.export = ExportService(self)

    def now(self) -> dt.datetime:
        return as_utc(self._clock())

    def for_tenant(self, tenant_id: str) -> Kit:
        """A view of this kit bound to another tenant. Handlers and collectors are shared."""
        other = copy.copy(self)
        other.tenant_id = tenant_id
        other._bind()
        other.retention._handlers = self.retention._handlers
        other.export._collectors = self.export._collectors
        return other

    def __repr__(self) -> str:
        return f"Kit(tenant={self.tenant_id!r}, policy={self.policy.id!r})"
