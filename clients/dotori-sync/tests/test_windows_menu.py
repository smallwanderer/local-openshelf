import pytest

from dotori_sync import windows_menu


def test_install_context_menu_registers_background_and_directory_keys(fake_winreg, monkeypatch):
    monkeypatch.setattr(windows_menu.sys, "argv", [r"C:\Users\a\bin\dotori-sync.exe"])

    windows_menu.install_context_menu()

    background_label = fake_winreg.values[windows_menu._BACKGROUND_KEY]
    directory_label = fake_winreg.values[windows_menu._DIRECTORY_KEY]
    assert background_label == windows_menu.MENU_LABEL
    assert directory_label == windows_menu.MENU_LABEL

    background_cmd = fake_winreg.values[windows_menu._BACKGROUND_KEY + r"\command"]
    directory_cmd = fake_winreg.values[windows_menu._DIRECTORY_KEY + r"\command"]
    assert r'"C:\Users\a\bin\dotori-sync.exe" watch "%V"' in background_cmd
    assert r'"C:\Users\a\bin\dotori-sync.exe" watch "%1"' in directory_cmd
    assert background_cmd.startswith("cmd.exe /c ")
    assert background_cmd.endswith('|| pause"')


def test_install_falls_back_to_module_invocation_for_a_py_entrypoint(fake_winreg, monkeypatch):
    monkeypatch.setattr(windows_menu.sys, "argv", [r"C:\Users\a\src\dotori_sync\__main__.py"])
    monkeypatch.setattr(windows_menu.sys, "executable", r"C:\Users\a\venv\python.exe")

    windows_menu.install_context_menu()

    command = fake_winreg.values[windows_menu._DIRECTORY_KEY + r"\command"]
    assert r'"C:\Users\a\venv\python.exe" -m dotori_sync watch "%1"' in command


def test_uninstall_removes_keys_that_were_installed(fake_winreg, monkeypatch):
    monkeypatch.setattr(windows_menu.sys, "argv", [r"C:\bin\dotori-sync.exe"])
    windows_menu.install_context_menu()
    assert fake_winreg.existing_keys

    windows_menu.uninstall_context_menu()

    assert fake_winreg.existing_keys == set()
    assert fake_winreg.values == {}


def test_uninstall_is_a_no_op_when_nothing_was_installed(fake_winreg):
    windows_menu.uninstall_context_menu()  # must not raise
    assert fake_winreg.existing_keys == set()


def test_install_raises_off_windows(monkeypatch):
    monkeypatch.setattr(windows_menu.sys, "platform", "linux")

    with pytest.raises(RuntimeError):
        windows_menu.install_context_menu()

    with pytest.raises(RuntimeError):
        windows_menu.uninstall_context_menu()
