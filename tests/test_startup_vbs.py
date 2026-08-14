"""Tests for the Windows startup script's encoding.

Windows Script Host reads a .vbs as system ANSI, or as UTF-16 when it finds a
BOM. It does not read UTF-8. Getting this wrong fails in two different ways,
both of them silent or cryptic, so both are pinned here.
"""

from __future__ import annotations

import codecs
import shutil
import subprocess
from pathlib import Path

import pytest

from game_save_genie.cli import encode_vbs


def test_startup_script_is_utf16_with_a_bom() -> None:
    data = encode_vbs('WScript.Quit 0\r\n')
    assert data.startswith(codecs.BOM_UTF16_LE)
    assert data.decode("utf-16") == 'WScript.Quit 0\r\n'


def test_startup_script_never_carries_a_utf8_bom() -> None:
    """A UTF-8 BOM makes WSH refuse the file with 'Invalid character'
    (800A0408) at line 1 char 1, before a single statement runs."""
    assert not encode_vbs("WScript.Quit 0\r\n").startswith(codecs.BOM_UTF8)


def test_a_non_ascii_path_survives_the_round_trip() -> None:
    """The silent failure. Encoded as UTF-8 this is read as ANSI, so the path
    turns into mojibake and the startup script points nowhere - and because
    the script opens with On Error Resume Next, nothing is ever reported."""
    script = r'WshShell.Run """C:\Users\José\gsg.exe"" auto", 0, False' + "\r\n"
    assert encode_vbs(script).decode("utf-16") == script


# The rest need a real Windows Script Host. Everything above is pure bytes and
# runs everywhere, including CI, which has no cscript on the Linux legs.
CSCRIPT = shutil.which("cscript")
needs_wsh = pytest.mark.skipif(CSCRIPT is None, reason="no Windows Script Host")


def _run(tmp_path: Path, data: bytes) -> int:
    path = tmp_path / "probe.vbs"
    path.write_bytes(data)
    assert CSCRIPT is not None
    # check=False on purpose: a non-zero exit is the thing under test.
    return subprocess.run(
        [CSCRIPT, "//nologo", "//B", str(path)],
        capture_output=True, text=True, check=False,
    ).returncode


@needs_wsh
def test_what_we_write_actually_runs(tmp_path: Path) -> None:
    assert _run(tmp_path, encode_vbs("WScript.Quit 0\r\n")) == 0


@needs_wsh
def test_a_utf8_bom_really_does_break_it(tmp_path: Path) -> None:
    """Reproduces the reported failure, so the fix is anchored to the bug."""
    assert _run(tmp_path, codecs.BOM_UTF8 + b"WScript.Quit 0\r\n") != 0


@needs_wsh
def test_non_ascii_is_read_correctly_and_utf8_is_not(tmp_path: Path) -> None:
    """'café' is 4 characters. WSH reading UTF-8 bytes as ANSI counts 5."""
    script = 'WScript.Quit Len("caf\u00e9")\r\n'
    assert _run(tmp_path, encode_vbs(script)) == 4
    assert _run(tmp_path, script.encode("utf-8")) == 5
