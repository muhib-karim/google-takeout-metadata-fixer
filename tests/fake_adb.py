"""A stand-in for the ``adb`` executable, so the phone modes can be tested without a device.

It answers the handful of commands the scripts send (``devices``, ``shell ls``,
``shell unzip -l``, ``shell unzip -p``, ``shell stat``, ``pull``, ``push``) from local zip
files, and can be told to fail, time out, or translate line endings the way old adb
versions do. Every command string is recorded so tests can check how it was quoted.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class FakeAdb:
    def __init__(
        self, zips: Mapping[str, Path | str], *, devices: str = "SYNTH0001\tdevice", crlf: bool = False
    ) -> None:
        self.zips = {remote: Path(local) for remote, local in zips.items()}
        self.devices = devices
        self.crlf = crlf
        self.fail: set[str] = set()      # operations that return a non-zero status and no output
        self.timeout: set[str] = set()   # operations that raise TimeoutExpired
        self.calls: list[list[str]] = []
        self.pushed: dict[str, bytes] = {}
        self._real_run = subprocess.run

    def install(self, monkeypatch: Any) -> FakeAdb:
        monkeypatch.setattr(subprocess, "run", self)
        return self

    # -- helpers ---------------------------------------------------------------------

    @property
    def shell_commands(self) -> list[str]:
        """The command strings that were handed to the device shell."""
        return [" ".join(call[2:]) for call in self.calls if call[1] in ("shell", "exec-out")]

    def _done(self, cmd: list[str], kwargs: dict[str, Any], out: bytes = b"", code: int = 0) -> Any:
        text = bool(kwargs.get("text") or kwargs.get("encoding"))
        if self.crlf:
            out = out.replace(b"\n", b"\r\n")
        return subprocess.CompletedProcess(
            cmd, code, out.decode("utf-8", "replace") if text else out, "" if text else b""
        )

    def _check(self, operation: str, cmd: list[str], kwargs: dict[str, Any]) -> Any | None:
        if operation in self.timeout:
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 0)
        if operation in self.fail:
            return self._done(cmd, kwargs, b"", 1)
        return None

    # -- the subprocess.run replacement ---------------------------------------------------

    def __call__(self, cmd: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(cmd, (list, tuple)) or not cmd or cmd[0] != "adb":
            return self._real_run(cmd, *args, **kwargs)
        cmd = [str(c) for c in cmd]
        self.calls.append(cmd)

        if cmd[1] == "devices":
            return self._check("devices", cmd, kwargs) or self._done(
                cmd, kwargs, f"List of devices attached\n{self.devices}\n\n".encode()
            )
        if cmd[1] == "pull":
            failed = self._check("pull", cmd, kwargs)
            if failed is None:
                shutil.copyfile(self.zips[cmd[2]], cmd[3])
            return failed or self._done(cmd, kwargs)
        if cmd[1] == "push":
            failed = self._check("push", cmd, kwargs)
            if failed is None:
                self.pushed[cmd[3]] = Path(cmd[2]).read_bytes()
            return failed or self._done(cmd, kwargs)

        # "adb shell" joins its arguments with spaces and lets the device shell parse them
        argv = [a for a in shlex.split(" ".join(cmd[2:])) if not a.startswith("2>")]
        if argv[0] == "ls":
            directory = argv[-1].rstrip("/")
            names = sorted(r.rsplit("/", 1)[1] for r in self.zips if r.rsplit("/", 1)[0] == directory)
            return self._check("ls", cmd, kwargs) or self._done(cmd, kwargs, ("\n".join(names) + "\n").encode())
        if argv[0] == "stat":
            local = self.zips.get(argv[-1])
            if local is None:
                return self._done(cmd, kwargs, b"", 1)
            return self._check("stat", cmd, kwargs) or self._done(cmd, kwargs, f"{local.stat().st_size}\n".encode())
        if argv[0] == "unzip" and argv[1] == "-l":
            failed = self._check("list", cmd, kwargs)
            if failed is not None or argv[2] not in self.zips:
                return failed or self._done(cmd, kwargs, b"", 9)
            with zipfile.ZipFile(self.zips[argv[2]]) as zf:
                rows = [f"{i.file_size:9d}  2024-01-01 12:00   {i.filename}" for i in zf.infolist()]
            head = [f"Archive:  {argv[2]}", "  Length      Date    Time    Name", "---------  ---------- -----   ----"]
            tail = ["---------                     -------", f"        0                     {len(rows)} files"]
            return self._done(cmd, kwargs, ("\n".join(head + rows + tail) + "\n").encode())
        if argv[0] == "unzip" and argv[1] == "-p":
            everything = argv[3] == "*.json"
            failed = self._check("stream" if everything else "single", cmd, kwargs)
            if failed is not None or argv[2] not in self.zips:
                return failed or self._done(cmd, kwargs, b"", 9)
            with zipfile.ZipFile(self.zips[argv[2]]) as zf:
                if everything:
                    data = b"".join(zf.read(i) for i in zf.infolist() if i.filename.endswith(".json"))
                else:
                    wanted = _unescape_unzip_pattern(argv[3])
                    data = zf.read(wanted) if wanted in zf.namelist() else b""
            return self._done(cmd, kwargs, data)
        if argv[0] in ("mkdir", "rm", "find"):
            return self._check(argv[0], cmd, kwargs) or self._done(cmd, kwargs)
        raise AssertionError(f"FakeAdb does not understand: {cmd}")


def _unescape_unzip_pattern(pattern: str) -> str:
    """Undo the backslash escaping of unzip wildcard characters."""
    out, i = [], 0
    while i < len(pattern):
        if pattern[i] == "\\" and i + 1 < len(pattern):
            i += 1
        out.append(pattern[i])
        i += 1
    return "".join(out)
