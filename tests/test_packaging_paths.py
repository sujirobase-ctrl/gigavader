"""Regression tests for launching from WindowsApps and TLS policy resources."""

from pathlib import Path

import pytest

import gui
import build_support


def test_gui_default_is_independent_of_launcher_directory(tmp_path, monkeypatch):
    home = tmp_path / "user"
    launcher = tmp_path / "Program Files" / "WindowsApps" / "launcher"
    launcher.mkdir(parents=True)
    monkeypatch.chdir(launcher)
    monkeypatch.setattr(gui.Path, "home", lambda: home)
    assert gui.default_download_directory() == home / "Downloads" / "BUIDL"


def test_gui_rejects_unwritable_destination_before_download(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("Read-only launcher directory")
    monkeypatch.setattr(gui.tempfile, "TemporaryFile", denied)
    with pytest.raises(ValueError, match="pasta de destino"):
        gui.validate_output_directory(str(tmp_path / "unwritable"))


def test_failed_legacy_session_does_not_restore_windowsapps_destination():
    document = {'options': {'download_path': r'C:\Program Files\WindowsApps\Codex\Downloads'}}
    assert gui.recovery_output_directory(document) == str(gui.default_download_directory())
    assert document['options']['download_path'] == str(gui.default_download_directory())
    prepared = {'prepared': {'plan': []}, 'options': {'download_path': r'C:\Program Files\WindowsApps\Codex\Downloads'}}
    assert gui.recovery_output_directory(prepared) == prepared['options']['download_path']


def test_custom_ssl_runtime_policy_file_must_be_shipped(tmp_path, monkeypatch):
    source = tmp_path / "ssl.py"
    source.write_text("load('codex-rejected-cas.pem')", encoding="utf-8")
    monkeypatch.setattr(build_support.ssl, "__file__", str(source))
    with pytest.raises(RuntimeError, match="TLS policy file is missing"):
        build_support.ssl_runtime_data()
    resource = tmp_path / "codex-rejected-cas.pem"
    resource.write_text("test fixture", encoding="utf-8")
    assert build_support.ssl_runtime_data() == [(str(resource), ".")]


def test_standard_python_ssl_needs_no_extra_policy_file(tmp_path, monkeypatch):
    source = tmp_path / "ssl.py"
    source.write_text("# standard Python ssl implementation", encoding="utf-8")
    monkeypatch.setattr(build_support.ssl, "__file__", str(source))
    assert build_support.ssl_runtime_data() == []
