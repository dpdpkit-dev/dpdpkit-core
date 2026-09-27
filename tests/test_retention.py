from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dpdpkit import InMemoryRepository, Kit, MemoryNotifier, Policy, Registry, ScheduleStatus, sync_notice
from dpdpkit.models import DeliveryStatus, RetentionTrigger

from .conftest import T0, FakeClock


def _schedule(kit: Kit, principal: str, purpose: str):  # type: ignore[no-untyped-def]
    return next(s for s in kit.retention.schedules(principal, active_only=False)[::-1] if s.purpose == purpose)


def test_activity_creates_inactivity_schedules(kit: Kit) -> None:
    kit.retention.record_activity("u1")
    by_purpose = {s.purpose: s for s in kit.retention.schedules("u1")}
    assert set(by_purpose) == {"account", "analytics"}
    assert by_purpose["analytics"].erase_at == T0 + dt.timedelta(days=30)
    assert by_purpose["analytics"].warn_at == T0 + dt.timedelta(days=30) - dt.timedelta(hours=48)


def test_happy_path_warns_then_erases_after_window(kit, clock, notifier, erased) -> None:  # type: ignore[no-untyped-def]
    kit.retention.record_activity("u1")
    clock.advance(days=28)
    run = kit.retention.run()
    assert run.warned == 1 and run.erased == 0
    assert len(notifier.sent) == 1 and notifier.sent[0].to == "u1@example.in"
    assert "erased" in notifier.sent[0].subject
    s = _schedule(kit, "u1", "analytics")
    assert s.status is ScheduleStatus.WARNED

    clock.advance(hours=47, minutes=59)
    assert kit.retention.run().erased == 0
    assert erased == []

    clock.advance(minutes=1)
    assert kit.retention.run().erased == 1
    assert erased == [("u1", ["analytics"])]
    s = _schedule(kit, "u1", "analytics")
    assert s.status is ScheduleStatus.ERASED and s.processor_fanout == {"crm": "notified"}
    actions = [a.action for a in kit.ledger.audit_entries(subject_id=s.id)]
    assert actions.index("retention.erasure.started") < actions.index("retention.erasure.completed")
    kit.ledger.verify()


def test_failed_warning_never_erases(kit, clock, notifier, erased) -> None:  # type: ignore[no-untyped-def]
    notifier.status = DeliveryStatus.FAILED
    kit.retention.record_activity("u1")
    clock.advance(days=28)
    run = kit.retention.run()
    assert run.needs_attention == 1
    clock.advance(days=400)
    kit.retention.run()
    assert erased == []
    [attention] = [s for s in kit.retention.needs_attention() if s.purpose == "analytics"]
    assert "not delivered" in (attention.attention_reason or "")


def test_missing_contact_needs_attention(kit, clock, erased) -> None:  # type: ignore[no-untyped-def]
    kit.retention.record_activity("u-unknown")
    clock.advance(days=29)
    kit.retention.run()
    assert erased == []
    assert any("no contact" in (s.attention_reason or "") for s in kit.retention.needs_attention())


def test_queued_warning_waits_for_confirmation(kit, clock, notifier, erased) -> None:  # type: ignore[no-untyped-def]
    notifier.status = DeliveryStatus.QUEUED
    kit.retention.record_activity("u1")
    clock.advance(days=28)
    assert kit.retention.run().warning_pending == 1
    clock.advance(days=10)
    kit.retention.run()
    assert erased == []
    s = _schedule(kit, "u1", "analytics")
    assert s.status is ScheduleStatus.WARNING_PENDING
    s = kit.retention.confirm_warning_delivery(s.warning_receipt_id, True)
    assert s.status is ScheduleStatus.WARNED
    assert s.erase_at == clock.now + dt.timedelta(hours=48)
    clock.advance(hours=48)
    assert kit.retention.run().erased == 1


def test_activity_after_warning_cancels_erasure(kit, clock, erased) -> None:  # type: ignore[no-untyped-def]
    kit.retention.record_activity("u1")
    clock.advance(days=28, hours=1)
    kit.retention.run()
    warned = _schedule(kit, "u1", "analytics")
    assert warned.status is ScheduleStatus.WARNED
    clock.advance(hours=1)
    kit.retention.record_activity("u1")
    old = kit.repository.get_schedule(kit.tenant_id, warned.id)
    assert old.status is ScheduleStatus.CANCELLED
    new = next(s for s in kit.retention.schedules("u1") if s.purpose == "analytics")
    assert new.erase_at == clock.now + dt.timedelta(days=30)
    clock.advance(days=5)
    kit.retention.run()
    assert erased == []


def test_activity_between_warning_and_run_cancels_even_without_middleware(kit, clock, erased) -> None:  # type: ignore[no-untyped-def]
    kit.retention.record_activity("u1")
    clock.advance(days=28)
    kit.retention.run()
    clock.advance(hours=1)
    kit.repository.record_activity(kit.tenant_id, "u1", clock.now)  # activity recorded directly
    clock.advance(days=3)
    kit.retention.run()
    assert erased == []


def test_legal_hold_blocks_and_release_resumes(kit, clock, notifier, erased) -> None:  # type: ignore[no-untyped-def]
    kit.retention.record_activity("u1")
    hold = kit.retention.place_hold("u1", "tax audit", actor="dpo", purpose="analytics")
    clock.advance(days=29)
    assert kit.retention.run().held == 1
    assert notifier.sent == []
    kit.retention.release_hold(hold.id, actor="dpo")
    run = kit.retention.run()
    assert run.warned == 1
    clock.advance(days=3)
    kit.retention.run()
    assert erased == [("u1", ["analytics"])]


def test_handler_failure_needs_attention_then_retry(kit, clock) -> None:  # type: ignore[no-untyped-def]
    attempts: list[str] = []

    def flaky(principal: str, purposes: list[str]) -> None:
        attempts.append(principal)
        if len(attempts) == 1:
            raise RuntimeError("database down")

    kit.retention.register_handler(flaky)
    kit.retention.record_activity("u1")
    clock.advance(days=28)
    kit.retention.run()
    clock.advance(days=2)
    assert kit.retention.run().needs_attention == 1
    s = _schedule(kit, "u1", "analytics")
    assert "database down" in s.attention_reason
    s = kit.retention.resolve(s.id, "retry", actor="dpo")
    assert s.status is ScheduleStatus.SCHEDULED
    kit.retention.run()  # re-warns
    clock.advance(hours=48)
    assert kit.retention.run().erased == 1


def test_no_registered_handler_needs_attention(kit, clock) -> None:  # type: ignore[no-untyped-def]
    kit.retention.record_activity("u1")
    clock.advance(days=31)
    kit.retention.run()
    clock.advance(days=3)
    kit.retention.run()
    assert any("no erasure handler" in (s.attention_reason or "") for s in kit.retention.needs_attention())


def test_policy_change_changes_behaviour_without_code(clock, config, contacts) -> None:  # type: ignore[no-untyped-def]
    data = Policy.load("dpdp-rules-2025.v1").as_dict()
    data["id"] = "dpdp-rules-2025.v2"
    data["retention"]["erasure_pre_notice_hours"] = 72
    notifier = MemoryNotifier()
    kit = Kit(
        InMemoryRepository(),
        Policy(data),
        registry=Registry.from_config(config),
        notifier=notifier,
        clock=clock,
        signing_key="k",
        contact_resolver=contacts.get,
    )
    sync_notice(kit, config)
    kit.retention.record_activity("u1")
    s = next(s for s in kit.retention.schedules("u1") if s.purpose == "analytics")
    assert s.erase_at - s.warn_at == dt.timedelta(hours=72)
    clock.advance(days=27)
    assert kit.retention.run().warned == 1


def test_third_schedule_counts_from_commencement(clock, config, contacts) -> None:  # type: ignore[no-untyped-def]
    config["purposes"].append(
        {"id": "shop", "title": "Shop", "data_items": ["email"], "retention": {"third_schedule_class": "ecommerce"}}
    )
    clock.now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    kit = Kit(
        InMemoryRepository(), "dpdp-rules-2025.v1", registry=Registry.from_config(config), clock=clock, signing_key="k"
    )
    kit.retention.record_activity("u1")
    s = next(s for s in kit.retention.schedules("u1") if s.purpose == "shop")
    start = dt.datetime(2027, 5, 13, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))
    assert s.erase_at == start + dt.timedelta(days=1096)


def test_withdrawal_trigger_erases_after_warning(kit, clock, erased) -> None:  # type: ignore[no-untyped-def]
    kit.consent.grant("u1", "marketing")
    kit.consent.withdraw("u1", "marketing")
    kit.retention.run()
    s = _schedule(kit, "u1", "marketing")
    assert s.trigger is RetentionTrigger.WITHDRAWAL and s.status is ScheduleStatus.WARNED
    clock.advance(hours=48)
    kit.retention.run()
    assert ("u1", ["marketing"]) in erased


def test_preview_and_log_floor(kit, clock) -> None:  # type: ignore[no-untyped-def]
    kit.retention.record_activity("u1")
    items = kit.retention.preview(horizon=dt.timedelta(days=31))
    assert [(i.purpose, i.action) for i in items] == [("analytics", "warn"), ("analytics", "erase")]
    assert kit.retention.log_floor(T0) == T0 + dt.timedelta(days=365)
    assert not kit.retention.log_purgeable(T0)
    clock.advance(days=365)
    assert kit.retention.log_purgeable(T0)


def test_hold_requires_reason(kit: Kit) -> None:
    with pytest.raises(Exception):
        kit.retention.place_hold("u1", "  ", actor="dpo")


# ------------------------------------------------------------------ property test

step = st.one_of(
    st.tuples(st.just("advance"), st.integers(min_value=1, max_value=24 * 12)),
    st.tuples(st.just("activity"), st.sampled_from(["u1", "u2"])),
    st.tuples(st.just("run"), st.just(0)),
    st.tuples(st.just("delivery"), st.sampled_from(list(DeliveryStatus))),
)


@settings(max_examples=80, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(steps=st.lists(step, min_size=1, max_size=40))
def test_property_never_erase_without_delivered_warning_and_full_window(
    make_kit: Any, notifier: MemoryNotifier, steps: list[tuple[str, Any]]
) -> None:
    clock = FakeClock()
    kit = make_kit(clock=clock)
    notifier.status = DeliveryStatus.DELIVERED
    erased_at: dict[str, list[dt.datetime]] = {}
    kit.retention.register_handler(lambda p, purposes: erased_at.setdefault(p, []).append(clock.now))
    activity: dict[str, list[dt.datetime]] = {}
    for principal in ("u1", "u2"):
        kit.retention.record_activity(principal)
        activity.setdefault(principal, []).append(clock.now)
    for op, arg in steps:
        if op == "advance":
            clock.advance(hours=arg)
        elif op == "activity":
            kit.retention.record_activity(arg)
            activity.setdefault(arg, []).append(clock.now)
        elif op == "delivery":
            notifier.status = arg
        else:
            kit.retention.run()
        clock.advance(days=3)
        kit.retention.run()

    pre = kit.policy.erasure_pre_notice
    for s in kit.retention.schedules(active_only=False):
        if s.status is not ScheduleStatus.ERASED:
            continue
        assert s.warned_at is not None and s.erased_at is not None
        assert s.erased_at >= s.warned_at + pre
        receipt = kit.repository.get_delivery(kit.tenant_id, s.warning_receipt_id)
        assert receipt is not None and receipt.status is DeliveryStatus.DELIVERED
        assert not any(s.warned_at < t <= s.erased_at for t in activity.get(s.principal, []))
    kit.ledger.verify()
