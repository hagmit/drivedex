"""Register `drivedex sync --notify` with Windows Task Scheduler (schtasks, no extra packages).

The task is created from an XML definition rather than schtasks /SC flags so that
it can run on battery, catch up after sleep (StartWhenAvailable), skip a run while
the previous one is still going, and has no 261-character /TR limit.
It runs as the signed-in user only while they are logged on (InteractiveToken):
that is what lets DPAPI decrypt the tokens and the toast appear on their desktop.
"""
from __future__ import annotations

import datetime as dt
import os
import shlex
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

from .config import is_store_python

TASK = r"drivedex\sync"
MIN_EVERY, MAX_EVERY = 5, 1440


def check_every(every: int) -> int:
    if not MIN_EVERY <= every <= MAX_EVERY:
        raise ValueError(f"--every は {MIN_EVERY}〜{MAX_EVERY} 分で指定してください")
    return every


def pythonw(executable: str | None = None, env=None) -> str:
    """The windowless interpreter for the task (no console window flashing every hour)."""
    exe = Path(executable or sys.executable)
    env = os.environ if env is None else env
    if is_store_python(str(exe)) and env.get("LOCALAPPDATA"):
        # The Store package folder cannot be started directly; its app execution alias can.
        alias = Path(env["LOCALAPPDATA"], "Microsoft", "WindowsApps",
                     f"pythonw{sys.version_info.major}.{sys.version_info.minor}.exe")
        return str(alias)
    w = exe.with_name("pythonw.exe")
    return str(w) if w.exists() else str(exe)


def arguments(home: Path) -> list[str]:
    return ["-m", "drivedex", "--home", str(home), "sync", "--notify"]


def task_xml(command: str, args: list[str], every: int, start: dt.datetime) -> str:
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>drivedex: ドライブの変更を {every} 分ごとに取得して通知</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <StartBoundary>{start.strftime("%Y-%m-%dT%H:%M:%S")}</StartBoundary>
      <Repetition><Interval>PT{every}M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>{escape(command)}</Command><Arguments>{escape(subprocess.list2cmdline(args))}</Arguments></Exec>
  </Actions>
</Task>
"""


def cron_line(every: int, command: str, args: list[str]) -> str:
    if every < 60:
        when = f"*/{every} * * * *"
    elif every % 60 == 0 and every < 1440:
        when = "0 * * * *" if every == 60 else f"0 */{every // 60} * * *"
    else:
        when = "0 0 * * *"
    return f"{when} {shlex.join([command, *args])}"


def _schtasks(*args) -> tuple[int, str]:
    r = subprocess.run(["schtasks", *args], capture_output=True)
    enc = "oem" if os.name == "nt" else "utf-8"
    return r.returncode, (r.stdout + r.stderr).decode(enc, "replace").strip()


def install(home: Path, every: int, *, now: dt.datetime | None = None) -> str:
    start = (now or dt.datetime.now()).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
    xml = task_xml(pythonw(), arguments(home), check_every(every), start)
    path = Path(home) / "schedule-task.xml"
    path.write_text(xml, "utf-16")  # schtasks /XML wants UTF-16 with a BOM
    try:
        code, out = _schtasks("/Create", "/TN", TASK, "/XML", str(path), "/F")
    finally:
        path.unlink(missing_ok=True)
    if code:
        raise ValueError(f"タスクを登録できませんでした: {out}")
    return out


def remove() -> bool:
    code, _ = _schtasks("/Delete", "/TN", TASK, "/F")
    return code == 0


def status() -> str | None:
    code, out = _schtasks("/Query", "/TN", TASK, "/V", "/FO", "LIST")
    return out if code == 0 else None
