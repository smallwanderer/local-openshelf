import sys

import pytest

from dotori_sync import windows_menu


class _FakeKeyHandle:
    def __init__(self, store: dict, path: str) -> None:
        self._store = store
        self.path = path

    def __enter__(self) -> "_FakeKeyHandle":
        return self

    def __exit__(self, *exc_info) -> None:
        return None


class FakeWinReg:
    """Records registry operations in memory instead of touching HKCU."""

    HKEY_CURRENT_USER = "HKEY_CURRENT_USER"
    REG_SZ = "REG_SZ"

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.existing_keys: set[str] = set()

    def CreateKey(self, hive, path: str) -> _FakeKeyHandle:
        assert hive == self.HKEY_CURRENT_USER
        self.existing_keys.add(path)
        return _FakeKeyHandle(self.values, path)

    def SetValue(self, key: _FakeKeyHandle, sub_key: str, type_, value: str) -> None:
        assert sub_key == ""
        assert type_ == self.REG_SZ
        self.values[key.path] = value

    def DeleteKey(self, hive, path: str) -> None:
        assert hive == self.HKEY_CURRENT_USER
        if path not in self.existing_keys:
            raise FileNotFoundError(path)
        self.existing_keys.discard(path)
        self.values.pop(path, None)


@pytest.fixture
def fake_winreg(monkeypatch):
    """Redirects dotori_sync.windows_menu at an in-memory registry.

    Prevents tests from ever writing to the real Windows registry, on this
    machine or any other, regardless of the actual OS running the suite.
    """
    fake = FakeWinReg()
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(windows_menu.sys, "platform", "win32")
    return fake
