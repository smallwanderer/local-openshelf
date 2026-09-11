import time

from dotori_sync.watcher import start_observer


def test_observer_reports_activity_on_new_file(tmp_path):
    events = []
    observer = start_observer(tmp_path, lambda: events.append(time.monotonic()))
    try:
        (tmp_path / "new-file.txt").write_text("hello", encoding="utf-8")

        deadline = time.monotonic() + 5.0
        while not events and time.monotonic() < deadline:
            time.sleep(0.1)

        assert events, "watchdog did not report the new file within the timeout"
    finally:
        observer.stop()
        observer.join()
