from pathlib import Path

from dotori_cli.http_client import DotoriClientError

from dotori_sync.daemon import SyncRunner, WatchLoop


class FakeSyncClient:
    """Mirrors dotori-cli's own sync test double: dotori-sync never talks to
    the sync HTTP contract directly, only through dotori_cli.sync.run_sync,
    so exercising that boundary is enough to prove no logic was duplicated.
    """

    def __init__(self, actions=None, *, diff_error: Exception | None = None):
        self.actions = actions or []
        self.diff_error = diff_error
        self.calls = []

    def sync_diff(self, *, root_name, entries):
        self.calls.append(("diff", root_name, entries))
        if self.diff_error is not None:
            raise self.diff_error
        return {
            "ok": True,
            "actions": self.actions,
            "sync_id": "sync-1",
            "root_name": root_name,
            "root_uid": "root-1",
        }

    def sync_mkdir(self, **kwargs):
        self.calls.append(("mkdir", kwargs))
        return {"ok": True, "root_uid": "root-1", "node_uid": "folder-1"}

    def sync_upload(self, file_path: Path, **kwargs):
        self.calls.append(("upload", file_path, kwargs))
        return {"ok": True, "root_uid": "root-1", "node_uid": "file-1"}

    def sync_delete(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return {"ok": True, "root_uid": "root-1", "deleted": 1}

    def sync_confirm(self, **kwargs):
        self.calls.append(("confirm", kwargs))
        return {"ok": True}


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _runner(tmp_path, client, **kwargs) -> SyncRunner:
    (tmp_path / "report.txt").write_text("hello", encoding="utf-8")
    return SyncRunner(client, tmp_path, root_name="docs", **kwargs)


# ── SyncRunner ───────────────────────────────────────────────────────────


def test_run_once_applies_and_reports_success(tmp_path):
    client = FakeSyncClient([{"action": "upload", "rel_path": "report.txt"}])
    runner = _runner(tmp_path, client)

    payload = runner.run_once()

    assert payload["ok"] is True
    assert payload["applied"] == 1
    assert runner.last_error is None
    assert runner.run_count == 1
    assert [call[0] for call in client.calls] == ["diff", "upload", "confirm"]


def test_run_once_swallows_client_errors_so_the_daemon_survives(tmp_path):
    client = FakeSyncClient(diff_error=DotoriClientError("boom", status_code=500))
    runner = _runner(tmp_path, client)

    payload = runner.run_once()

    assert payload["ok"] is False
    assert "boom" in payload["error"]
    assert runner.last_error is not None
    assert runner.run_count == 1


def test_run_once_swallows_unexpected_errors(tmp_path, monkeypatch):
    client = FakeSyncClient([{"action": "upload", "rel_path": "report.txt"}])
    runner = _runner(tmp_path, client)
    monkeypatch.setattr(
        "dotori_sync.daemon.run_sync",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("kaboom")),
    )

    payload = runner.run_once()

    assert payload["ok"] is False
    assert "kaboom" in payload["error"]


# ── WatchLoop scheduling ─────────────────────────────────────────────────


def test_tick_does_not_fire_before_debounce_or_fallback_elapses(tmp_path):
    client = FakeSyncClient([])
    runner = _runner(tmp_path, client)
    clock = FakeClock()
    loop = WatchLoop(
        runner,
        debounce_seconds=5.0,
        fallback_interval_seconds=300.0,
        now_fn=clock.time,
    )
    loop._last_run = 0.0  # pretend an initial pass already happened

    loop.notify()
    clock.advance(2.0)

    assert loop.tick() is False
    assert runner.run_count == 0


def test_tick_fires_once_debounce_window_is_quiet(tmp_path):
    client = FakeSyncClient([])
    runner = _runner(tmp_path, client)
    clock = FakeClock()
    loop = WatchLoop(runner, debounce_seconds=5.0, fallback_interval_seconds=300.0, now_fn=clock.time)
    loop._last_run = 0.0

    loop.notify()
    clock.advance(6.0)

    assert loop.tick() is True
    assert runner.run_count == 1
    # A second tick with no new activity and the fallback not yet due does nothing.
    clock.advance(1.0)
    assert loop.tick() is False
    assert runner.run_count == 1


def test_burst_of_notifies_collapses_into_a_single_pass(tmp_path):
    client = FakeSyncClient([])
    runner = _runner(tmp_path, client)
    clock = FakeClock()
    loop = WatchLoop(runner, debounce_seconds=5.0, fallback_interval_seconds=300.0, now_fn=clock.time)
    loop._last_run = 0.0

    for _ in range(10):
        loop.notify()
        clock.advance(1.0)  # never quiet long enough to fire mid-burst
        assert loop.tick() is False

    clock.advance(6.0)
    assert loop.tick() is True
    assert runner.run_count == 1


def test_fallback_interval_fires_without_any_activity(tmp_path):
    client = FakeSyncClient([])
    runner = _runner(tmp_path, client)
    clock = FakeClock()
    loop = WatchLoop(runner, debounce_seconds=5.0, fallback_interval_seconds=300.0, now_fn=clock.time)
    loop._last_run = 0.0

    clock.advance(299.0)
    assert loop.tick() is False
    clock.advance(2.0)
    assert loop.tick() is True
    assert runner.run_count == 1


def test_run_forever_runs_an_initial_pass_then_polls_until_stopped(tmp_path):
    client = FakeSyncClient([])
    runner = _runner(tmp_path, client)
    clock = FakeClock()
    loop = WatchLoop(
        runner,
        debounce_seconds=5.0,
        fallback_interval_seconds=1000.0,
        now_fn=clock.time,
    )

    ticks = {"n": 0}

    def fake_sleep(seconds: float) -> None:
        ticks["n"] += 1
        clock.advance(seconds)
        if ticks["n"] == 3:
            loop.notify()
        if ticks["n"] >= 12:
            loop.stop()

    loop._sleep = fake_sleep
    loop.run_forever(poll_seconds=1.0)

    # One immediate pass on start, one more once the notify()'d activity
    # goes quiet for the debounce window; the fallback interval never fires.
    assert runner.run_count == 2
