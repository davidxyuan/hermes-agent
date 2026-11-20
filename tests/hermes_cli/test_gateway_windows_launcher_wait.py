"""The logon task's ``gateway.vbs`` waits for the gateway and exits with its code (#146976).

The launcher used to ``Run`` the gateway with ``bWaitOnReturn=False``: wscript returned at once, the
task ended "successfully" seconds after logon, and ``RestartOnFailure`` could never see the gateway die.
"""
import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli import gateway_windows
from hermes_cli.gateway_runtime_service_identity import _vbs_identity


def _launcher(tmp_path, monkeypatch, python):
    monkeypatch.setattr(gateway_windows, "_resolve_detached_python", lambda exe: (str(python), tmp_path / "venv", []))
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home, gateway_windows._build_gateway_vbs_script(str(python), str(tmp_path), str(home), "")


def _pre_146976(script: str) -> str:
    """The launcher installs made before #146976 still run: the same template, returning at once."""
    lines = script.split("\r\n")
    run = next(i for i, line in enumerate(lines) if line.startswith("code = sh.Run("))
    lines[2] = "Dim sh, env, existing_pp"
    lines[run] = "sh.Run " + lines[run][len("code = sh.Run("):-len(", 0, True)")] + ", 0, False"
    del lines[run + 1:run + 4]
    return "\r\n".join(lines)


@pytest.mark.platforms("any")
def test_installer_generations_verify(tmp_path, monkeypatch):
    home, script = _launcher(tmp_path, monkeypatch, tmp_path / "python.exe")
    # Reconstruct the previous waiting template, then its earlier fire-and-forget version.
    lines = script.splitlines(keepends=True)
    loop = lines.index("Dim attempt, attempts\r\n")
    run = next(i for i, line in enumerate(lines) if line.startswith("code = sh.Run"))
    stop = lines.index("If code = 0 Then Exit For\r\n")
    previous = "".join(lines[:loop] + lines[run:stop] + lines[lines.index("Next\r\n") + 1:])
    for text in (script, previous, _pre_146976(previous)):
        configured, argv = _vbs_identity(text)
        assert configured == str(home)
        assert argv[0] == str(tmp_path / "python.exe") and argv[-2:] == ["gateway", "run"]
    assert _vbs_identity(script, require_task_retry=True) == _vbs_identity(previous)
    with pytest.raises(ValueError):
        _vbs_identity(previous, require_task_retry=True)


@pytest.mark.platforms("any")
@pytest.mark.parametrize("edit", [
    lambda s: s.replace("WScript.Quit code", "WScript.Quit 0"),
    lambda s: s.replace("If code = 78 Then code = 0", "If code <> 0 Then code = 0"),
    lambda s: s.replace("gateway.pid", "other.pid"),
    lambda s: s.replace("WScript.Sleep 60000", "WScript.Sleep 1"),
    lambda s: s.replace("--retry-task", "--other-argument"),
    lambda s: s.replace("\r\nWScript.Quit code", ""),
])
def test_an_edited_exit_mapping_is_not_the_installers(tmp_path, monkeypatch, edit):
    _home, script = _launcher(tmp_path, monkeypatch, tmp_path / "python.exe")
    with pytest.raises(ValueError):
        _vbs_identity(edit(script))


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("code, pid_file, expected", [
    (3, False, 3),     # a crash: the task's RestartOnFailure must see it
    (1, False, 1),     # an uncaught error after the gateway removed its own record
    (1, True, 0),      # another gateway owns the profile, or a CLI stop force-killed this one
    (78, False, 0),    # fatal config: restarting every minute would loop
    (0, False, 0),
])
def test_wscript_waits_for_the_gateway_and_returns_its_mapped_exit(tmp_path, monkeypatch, code, pid_file, expected):
    """Real wscript.exe running the installer's launcher over a stub interpreter that exits ``code``
    after writing a marker: wscript must not return before the stub finished."""
    stub = tmp_path / "stub.cmd"
    marker = tmp_path / "finished"
    stub.write_text(f'@ping -n 3 127.0.0.1 >nul\r\n@echo done> "{marker}"\r\n@exit /b {code}\r\n', encoding="ascii")
    home, script = _launcher(tmp_path, monkeypatch, stub)
    if pid_file:
        (home / "gateway.pid").write_text("{}", encoding="utf-8")
    vbs = tmp_path / "gateway.vbs"
    vbs.write_text(script, encoding="utf-8", newline="")
    wscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"
    done = subprocess.run([str(wscript), "//B", "//Nologo", str(vbs)], timeout=60, check=False,
                          stdin=subprocess.DEVNULL, capture_output=True)
    assert marker.exists(), "wscript returned before the gateway exited"
    assert done.returncode == expected
