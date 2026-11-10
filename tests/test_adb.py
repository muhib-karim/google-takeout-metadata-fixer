"""ADB handling: quoting of device-shell commands, device state, timeouts."""

from __future__ import annotations

import json
import shlex
import sys

import pytest

import google_takeout_metadata_fixer as fixer
from tests.fake_adb import FakeAdb
from tests.takeout_fixtures import sidecar, write_zip

ALBUM = "Takeout/Google Photos/Album/"
AWKWARD_DIR = "/sdcard/My Takeout"
AWKWARD_ZIP = 'takeout-$(echo INJECTED)-`id`-"q" (1).zip'


def test_only_the_device_state_counts_as_ready():
    output = (
        "List of devices attached\n"
        "SYNTH0001\tdevice\n"
        "SYNTH0002\tunauthorized\n"
        "SYNTH0003\toffline\n"
        "????????????\tno permissions (missing udev rules?); see [http://developer.android.com/tools/device.html]\n"
        "device-in-serial\trecovery\n"
        "\n"
    )
    # Regression: any line containing the word "device" counted, including "no permissions ... device.html"
    assert fixer.parse_adb_devices(output) == ["SYNTH0001"]
    assert fixer.parse_adb_devices("List of devices attached\n\n") == []


@pytest.mark.parametrize("entry", ["a b.json", 'Mum "best" [1].jpg.json', "what?*.json", "back\\slash.json", "$(x)`y`.json"])
def test_entry_names_survive_shell_and_unzip_quoting(entry):
    command = f"unzip -p {fixer.sh_quote('/sdcard/z.zip')} {fixer.sh_quote(fixer.unzip_literal(entry))}"
    argv = shlex.split(command)
    assert argv[:3] == ["unzip", "-p", "/sdcard/z.zip"] and len(argv) == 4
    # what unzip receives matches only the literal name
    import fnmatch

    pattern = argv[3].replace("\\\\", "\x00").replace("\\", "").replace("\x00", "\\")
    assert pattern == entry
    assert not fnmatch.fnmatchcase("other.json", argv[3])


def _phone(tmp_path, jpeg_bytes, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    names = ["plain.jpg", 'Mum "best" [1].jpg', "what is this?.jpg", "100% $HOME `x`.jpg"]
    members = {}
    for i, name in enumerate(names):
        members[ALBUM + name] = jpeg_bytes
        members[ALBUM + name + ".supplemental-metadata.json"] = json.dumps(
            sidecar(timestamp=1_000_000_000 + i * 86400, title=name), indent=2
        ).encode()
        (out / name).write_bytes(jpeg_bytes)
    local_zip = write_zip(tmp_path / "phone.zip", members)
    adb = FakeAdb({f"{AWKWARD_DIR}/{AWKWARD_ZIP}": local_zip}).install(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["prog", "--output", str(out), "--phone-dir", AWKWARD_DIR])
    return adb, out, names


@pytest.mark.exiftool
def test_awkward_names_are_passed_to_the_device_as_data(tmp_path, monkeypatch, capsys, jpeg_bytes):
    # Regression: the directory was unquoted (a space split it), and zip and entry names sat
    # inside double quotes, where $( ), backticks and quotes are still interpreted by the shell.
    adb, out, names = _phone(tmp_path, jpeg_bytes, monkeypatch)
    adb.fail.add("stream")  # force every sidecar to be fetched by name

    fixer.main()

    assert "fixed=4" in capsys.readouterr().out
    remote = f"{AWKWARD_DIR}/{AWKWARD_ZIP}"
    for command in adb.shell_commands:
        argv = [a for a in shlex.split(command) if not a.startswith("2>")]
        if argv[0] == "ls":
            assert argv == ["ls", "-1", AWKWARD_DIR]
        else:
            assert argv[0] == "unzip" and argv[2] == remote and len(argv) in (3, 4)
    fetched = [shlex.split(c)[3] for c in adb.shell_commands if "unzip -p" in c and "*.json" not in c]
    assert len(fetched) == 4 and all("\\[" in f for f in fetched if "[" in f)


@pytest.mark.exiftool
@pytest.mark.parametrize(("operation", "expected"), [("list", "fixed=0"), ("stream", "fixed=0"), ("single", "no_result=4")])
def test_adb_timeouts_leave_the_zip_unfinished_instead_of_crashing(
    tmp_path, monkeypatch, capsys, jpeg_bytes, operation, expected
):
    # Regression: a timeout raised out of the program with a traceback
    adb, out, names = _phone(tmp_path, jpeg_bytes, monkeypatch)
    adb.timeout.add(operation)
    if operation == "single":
        adb.fail.add("stream")

    fixer.main()

    output = capsys.readouterr().out
    assert "1 zip(s) were not finished" in output
    if operation == "single":
        assert expected in output
    progress = json.loads((tmp_path / fixer.PROGRESS_FILE).read_text())
    assert progress["completed_zips"] == [] and progress["processed_entries"] == []


def test_missing_phone_directory_is_an_error_not_an_empty_list(tmp_path, monkeypatch, capsys):
    adb = FakeAdb({}).install(monkeypatch)
    adb.fail.add("ls")
    with pytest.raises(fixer.AdbError):
        fixer.adb_list_zips("/sdcard/Nope")
