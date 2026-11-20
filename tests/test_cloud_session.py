from datetime import datetime, timedelta
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
import requests

from app import cloud_cycle, cloud_session
from app.api_client import APIError


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
    monkeypatch.setattr(cloud_session, "handoff", Mock())
    monkeypatch.setattr(cloud_session, "record_standby", Mock())
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
def test_closed_market_waits_and_hands_off_without_orders(monkeypatch, value):
    session_clock(monkeypatch, value)
    cycle, handoff = Mock(), Mock()
    monkeypatch.setattr(cloud_session.subprocess, "run", cycle)
    monkeypatch.setattr(cloud_session, "handoff", handoff)
    cloud_session.run_session()
    cycle.assert_not_called()
    handoff.assert_called_once()


def test_late_start_runs_immediately_and_closes_at_three(monkeypatch):
    clock = session_clock(monkeypatch, "2026-11-19T05:59:30+00:00")
    monkeypatch.setattr(cloud_session, "SESSION_SECONDS", 660)
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


def test_reserved_successor_is_released_without_another_dispatch(monkeypatch, capsys):
    session_clock(monkeypatch, "2026-11-19T08:59:00+09:00")
    monkeypatch.setenv("CLOUD_CONTINUATION_RESERVED", "true")
    monkeypatch.setattr(cloud_session, "SESSION_SECONDS", 630)
    cycle = Mock()
    monkeypatch.setattr(cloud_session.subprocess, "run", cycle)
    cloud_session.run_session()
    cloud_session.handoff.assert_not_called()
    cycle.assert_not_called()
    assert "handoff_ready" in capsys.readouterr().out


def test_handoff_uses_workflow_token_and_serial_trade_job(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    post = Mock()
    monkeypatch.setattr(cloud_session.requests, "post", post)
    cloud_session.handoff()
    args = post.call_args.kwargs
    assert args["json"] == {"ref": "main", "inputs": {"trade": True, "session": True}}
    post.return_value.raise_for_status.assert_called_once()


def test_handoff_retries_network_failure(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    post = Mock(side_effect=[requests.Timeout(), Mock()])
    sleep = Mock()
    monkeypatch.setattr(cloud_session.requests, "post", post)
    monkeypatch.setattr(cloud_session.timer, "sleep", sleep)
    cloud_session.handoff()
    assert post.call_count == 2
    sleep.assert_called_once_with(5)


def test_handoff_reports_exhausted_retries(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "test-token")
    post = Mock(side_effect=requests.Timeout())
    monkeypatch.setattr(cloud_session.requests, "post", post)
    monkeypatch.setattr(cloud_session.timer, "sleep", Mock())
    with pytest.raises(RuntimeError, match="four attempts"):
        cloud_session.handoff()
    assert post.call_count == 4


@pytest.mark.parametrize("value, opening", [
    ("2026-11-20T08:00:00+09:00", "2026-11-20T09:00:00+09:00"),
    ("2026-11-20T15:00:00+09:00", "2026-11-23T09:00:00+09:00"),
    ("2026-11-21T08:00:00+09:00", "2026-11-23T09:00:00+09:00"),
    ("2026-11-22T23:59:00+09:00", "2026-11-23T09:00:00+09:00"),
])
def test_next_open_skips_night_and_weekend(value, opening):
    assert cloud_session.next_market_open(datetime.fromisoformat(value)) == datetime.fromisoformat(opening)


def test_successive_sessions_resume_on_monday_without_cron(monkeypatch):
    clock = session_clock(monkeypatch, "2026-11-20T15:00:00+09:00")
    opened = []

    def cycle(*args, **kwargs):
        opened.append(clock.value)
        raise StopIteration("market opened")

    monkeypatch.setattr(cloud_session.subprocess, "run", cycle)
    with pytest.raises(StopIteration, match="market opened"):
        for _ in range(15):
            cloud_session.run_session()
    assert opened == [datetime.fromisoformat("2026-11-23T09:00:00+09:00")]
    assert cloud_session.handoff.call_count >= 10


@pytest.mark.parametrize("failure", [subprocess.CalledProcessError(78, "cycle")])
def test_failed_cycle_stops_before_repeating_orders(monkeypatch, failure):
    session_clock(monkeypatch, "2026-11-19T10:00:00+09:00")
    cycle = Mock(side_effect=failure)
    monkeypatch.setattr(cloud_session.subprocess, "run", cycle)
    with pytest.raises(type(failure)):
        cloud_session.run_session()
    cycle.assert_called_once()


@pytest.mark.parametrize("failure", [subprocess.CalledProcessError(75, "cycle"),
    subprocess.CalledProcessError(1, "cycle"), subprocess.TimeoutExpired("cycle", 600)])
def test_data_failure_retries_after_one_minute(monkeypatch, capsys, failure):
    clock = session_clock(monkeypatch, "2026-11-19T10:00:00+09:00")
    starts = []

    def cycle(*args, **kwargs):
        starts.append(clock.value)
        if len(starts) == 1:
            raise failure
        clock.value = clock.value.replace(hour=15)

    monkeypatch.setattr(cloud_session.subprocess, "run", cycle)
    cloud_session.run_session()
    assert (starts[1] - starts[0]).total_seconds() == 60
    assert "cycle_retry" in capsys.readouterr().out


def test_recent_peer_cycle_keeps_its_original_due_time(monkeypatch):
    clock = session_clock(monkeypatch, "2026-11-20T09:05:00+09:00")
    starts = []

    def cycle(*args, **kwargs):
        starts.append(clock.value)
        if len(starts) == 1:
            output = json.dumps({"status": "recent_cycle", "next_cycle_at": "2026-11-20T09:15:00+09:00"})
            return subprocess.CompletedProcess("cycle", 0, stdout=output)
        clock.value = clock.value.replace(hour=15)
        return subprocess.CompletedProcess("cycle", 0, stdout="")

    monkeypatch.setattr(cloud_session.subprocess, "run", cycle)
    cloud_session.run_session()
    assert [v.strftime("%H:%M") for v in starts] == ["09:05", "09:15"]


def test_data_failure_preserves_ready_checkpoint_without_success_timestamp(monkeypatch):
    session_clock(monkeypatch, "2026-11-19T10:00:00+09:00")
    for field in ("CLOUD_PROBE", "CLOUD_RESEARCH", "RECOVERY_SHA"):
        monkeypatch.delenv(field, raising=False)
    checkpoint = Mock()
    checkpoint.restore.return_value = "ready"
    saved = []

    def save(directory, status, **kwargs):
        saved.append(status)
        metadata = json.loads((directory / "cycle.json").read_text())
        assert "started_at" not in metadata
        assert metadata["durable_orders"]

    checkpoint.save.side_effect = save
    monkeypatch.setattr(cloud_cycle, "Checkpoint", Mock(return_value=checkpoint))
    api = Mock()
    api.login.return_value = {"teamId": 5}
    monkeypatch.setattr(cloud_cycle, "TradingAPI", Mock(return_value=api))
    store = MagicMock()
    store.unresolved_orders.return_value = 0
    monkeypatch.setattr(cloud_cycle, "Store", Mock(return_value=store))
    engine = Mock()
    engine.run_cycle.return_value = [{"action": "HOLD", "quantity": 0, "execution": "DATA_ERROR"}]
    monkeypatch.setattr(cloud_cycle, "TradingEngine", Mock(return_value=engine))
    with pytest.raises(SystemExit) as result:
        cloud_cycle.main()
    assert result.value.code == 75
    assert saved == ["running", "ready"]
    api.place_order.assert_not_called()


@pytest.mark.parametrize("status", [None, 429, 503, 401])
def test_login_retries_transient_failures_and_stops_bad_credentials(monkeypatch, status):
    session_clock(monkeypatch, "2026-11-19T10:00:00+09:00")
    for field in ("CLOUD_PROBE", "CLOUD_RESEARCH", "RECOVERY_SHA"):
        monkeypatch.delenv(field, raising=False)
    api = Mock()
    api.login.side_effect = APIError("failure", status=status)
    monkeypatch.setattr(cloud_cycle, "TradingAPI", Mock(return_value=api))
    expected = APIError if status == 401 else SystemExit
    with pytest.raises(expected) as result:
        cloud_cycle.main()
    if expected is SystemExit:
        assert result.value.code == 75
    api.place_order.assert_not_called()


@pytest.mark.parametrize("age", [0, 899])
def test_recent_checkpoint_prevents_duplicate_cycle(monkeypatch, age):
    clock = session_clock(monkeypatch, "2026-11-19T10:00:00+09:00")
    for field in ("CLOUD_PROBE", "CLOUD_RESEARCH", "RECOVERY_SHA"):
        monkeypatch.delenv(field, raising=False)
    checkpoint = Mock()
    checkpoint.restore.return_value = "ready"

    def restore(directory, **kwargs):
        previous = clock.value - timedelta(seconds=age)
        (directory / "cycle.json").write_text(json.dumps({"started_at": previous.isoformat()}))

    def restore_ready(directory, **kwargs):
        restore(directory, **kwargs)
        return "ready"

    checkpoint.restore.side_effect = restore_ready
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
