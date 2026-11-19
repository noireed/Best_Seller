from datetime import datetime, timedelta
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import cloud_cycle, cloud_session


def session_clock(monkeypatch, value):
    clock = SimpleNamespace(value=datetime.fromisoformat(value), elapsed=0)

    class Clock:
        @staticmethod
        def now(tz):
            return clock.value.astimezone(tz)

    def advance(seconds):
        clock.value += timedelta(seconds=seconds)
        clock.elapsed += seconds

    monkeypatch.setattr(cloud_session, "datetime", Clock)
    monkeypatch.setattr(cloud_cycle, "datetime", Clock)
    monkeypatch.setattr(cloud_session.timer, "sleep", advance)
    monkeypatch.setattr(cloud_session.timer, "monotonic", lambda: clock.elapsed)
    return clock


def test_waits_for_open_and_repeats_without_cron(monkeypatch, capsys):
    clock = session_clock(monkeypatch, "2026-11-19T08:59:30+09:00")
    started = []

    def cycle(*args, **kwargs):
        started.append(clock.value)
        if len(started) == 2:
            clock.value = clock.value.replace(hour=15)

    monkeypatch.setattr(cloud_session.subprocess, "run", cycle)
    cloud_session.run_session()
    assert [v.strftime("%H:%M") for v in started] == ["09:00", "09:15"]
    assert "waiting_for_open" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["2026-11-19T15:00:00+09:00", "2026-11-21T09:00:00+09:00"])
def test_closed_market_never_orders_or_hands_off(monkeypatch, value):
    session_clock(monkeypatch, value)
    cycle, handoff = Mock(), Mock()
    monkeypatch.setattr(cloud_session.subprocess, "run", cycle)
    monkeypatch.setattr(cloud_session, "handoff", handoff)
    cloud_session.run_session()
    cycle.assert_not_called()
    handoff.assert_not_called()


def test_late_start_runs_immediately_and_closes_at_three(monkeypatch):
    clock = session_clock(monkeypatch, "2026-11-19T05:59:30+00:00")
    cycle = Mock()
    monkeypatch.setattr(cloud_session.subprocess, "run", cycle)
    cloud_session.run_session()
    cycle.assert_called_once()
    assert clock.value.astimezone(cloud_session.KST).hour == 15


def test_session_hands_off_before_runner_limit(monkeypatch):
    session_clock(monkeypatch, "2026-11-19T08:59:00+09:00")
    monkeypatch.setattr(cloud_session, "SESSION_SECONDS", 630)
    cycle, handoff = Mock(), Mock()
    monkeypatch.setattr(cloud_session.subprocess, "run", cycle)
    monkeypatch.setattr(cloud_session, "handoff", handoff)
    cloud_session.run_session()
    handoff.assert_called_once()
    cycle.assert_not_called()


def test_handoff_uses_workflow_token_and_serial_trade_job(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    post = Mock()
    monkeypatch.setattr(cloud_session.requests, "post", post)
    cloud_session.handoff()
    args = post.call_args.kwargs
    assert args["json"] == {"ref": "main", "inputs": {"trade": True, "session": True}}
    post.return_value.raise_for_status.assert_called_once()


@pytest.mark.parametrize("failure", [subprocess.CalledProcessError(1, "cycle"), subprocess.TimeoutExpired("cycle", 600)])
def test_failed_cycle_stops_before_repeating_orders(monkeypatch, failure):
    session_clock(monkeypatch, "2026-11-19T10:00:00+09:00")
    cycle = Mock(side_effect=failure)
    monkeypatch.setattr(cloud_session.subprocess, "run", cycle)
    with pytest.raises(type(failure)):
        cloud_session.run_session()
    cycle.assert_called_once()


@pytest.mark.parametrize("age", [0, 899])
def test_recent_checkpoint_prevents_duplicate_cycle(monkeypatch, age):
    clock = session_clock(monkeypatch, "2026-11-19T10:00:00+09:00")
    for field in ("CLOUD_PROBE", "CLOUD_RESEARCH", "RECOVERY_SHA"):
        monkeypatch.delenv(field, raising=False)
    checkpoint = Mock()

    def restore(directory):
        previous = clock.value - timedelta(seconds=age)
        (directory / "cycle.json").write_text(json.dumps({"started_at": previous.isoformat()}))

    checkpoint.restore.side_effect = restore
    monkeypatch.setattr(cloud_cycle, "Checkpoint", Mock(return_value=checkpoint))
    api = Mock()
    api.login.return_value = {"teamId": 5}
    monkeypatch.setattr(cloud_cycle, "TradingAPI", Mock(return_value=api))
    store = Mock()
    monkeypatch.setattr(cloud_cycle, "Store", store)
    cloud_cycle.main()
    store.assert_not_called()
    checkpoint.save.assert_not_called()


def test_older_checkpoint_restores_without_cycle_metadata(tmp_path, monkeypatch):
    import io
    import zipfile
    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    monkeypatch.setenv("AQUA_STATE_KEY", key.decode())
    monkeypatch.setenv("GH_TOKEN", "test-token")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("status", "ready")
        archive.writestr("trading.db", b"old-state")
    checkpoint = cloud_cycle.Checkpoint()
    response = Mock(content=Fernet(key).encrypt(buffer.getvalue()))
    checkpoint.session = Mock()
    checkpoint.session.get.return_value = response
    checkpoint.restore(tmp_path)
    assert (tmp_path / "trading.db").read_bytes() == b"old-state"
    assert not (tmp_path / "cycle.json").exists()
