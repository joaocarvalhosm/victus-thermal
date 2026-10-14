"""victusctl: inspect and steer victus-thermald. Runs unprivileged.

  victusctl status [--json]          live temperatures, fans and decision
  victusctl mode curve|auto|max      fan mode (resets to curve on reboot)
  victusctl profile [NAME]           show or set the platform profile
  victusctl ac-quiet [on|off]        fans never stop on AC (remembered)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

from .daemon import AC_QUIET_PATH, MODE_PATH, STATUS_PATH
from .policy import MODES

# power-profiles-daemon names for each kernel platform_profile
PPD_NAMES = {"low-power": "power-saver", "balanced": "balanced", "performance": "performance"}


def _status() -> dict | None:
    try:
        with open(STATUS_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _fmt(value, unit: str = "") -> str:
    return "-" if value is None else f"{value}{unit}"


def cmd_status(args) -> int:
    s = _status()
    if s is None:
        print("victus-thermald is not running (no status file).", file=sys.stderr)
        print("Check: systemctl status victus-thermal", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(s, indent=2))
        return 0
    age = time.time() - s["ts"]
    t, d = s["temps"], s["decision"]
    rpm = s["fans_rpm"]
    print(f"driver    {s['driver']}")
    print(f"profile   {_fmt(s['profile'])}   power: {'AC' if s['on_ac'] else 'battery'}")
    print(f"mode      {s['mode']}" + ("   EMERGENCY" if s["emergency"] else ""))
    if "ac_quiet" in s:
        q = s["ac_quiet"]
        print(f"ac quiet  {'on' if q['enabled'] else 'off'}"
              + ("  (active)" if q["active"] else ""))
    print(f"cpu       {_fmt(t['cpu'], ' C')}")
    print(f"igpu      {_fmt(t['igpu'], ' C')}")
    print(f"dgpu      {_fmt(t['dgpu'], ' C')} ({t['dgpu_state']})")
    print(f"fans      {_fmt(rpm[0])} / {_fmt(rpm[1])} RPM")
    print(f"decision  {d['kind']}  {d['reason']}")
    if s.get("error"):
        print(f"error     {s['error']}")
    if age > 10:
        print(f"warning   status is {age:.0f}s old; is the daemon stuck?")
    return 0


def _write_request(path: str, value: str) -> bool:
    """Atomically replace a request file the daemon reads."""
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".req-")
        with os.fdopen(fd, "w") as f:
            f.write(value + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except PermissionError:
        print("permission denied: add yourself to the 'victus-thermal' group "
              "(the installer does this) and log in again", file=sys.stderr)
        return False
    except OSError as exc:
        print(f"cannot write {path}: {exc}", file=sys.stderr)
        return False
    return True


def cmd_mode(args) -> int:
    if not _write_request(MODE_PATH, args.mode):
        return 1
    print(f"fan mode: {args.mode}")
    return 0


def cmd_ac_quiet(args) -> int:
    if args.state is None:
        s = _status()
        if s is None or "ac_quiet" not in s:
            print("victus-thermald is not running", file=sys.stderr)
            return 1
        q = s["ac_quiet"]
        print(("on" if q["enabled"] else "off") + (" (active now)" if q["active"] else ""))
        return 0
    if not _write_request(AC_QUIET_PATH, args.state):
        return 1
    print(f"AC quiet: {args.state}")
    return 0


def cmd_profile(args) -> int:
    if args.name is None:
        try:
            with open("/sys/firmware/acpi/platform_profile") as f:
                current = f.read().strip()
            with open("/sys/firmware/acpi/platform_profile_choices") as f:
                choices = f.read().split()
        except OSError as exc:
            print(f"cannot read platform profile: {exc}", file=sys.stderr)
            return 1
        print(" ".join(f"[{c}]" if c == current else c for c in choices))
        return 0
    # Go through power-profiles-daemon so GNOME's menu stays in sync.
    try:
        subprocess.run(["powerprofilesctl", "set", PPD_NAMES[args.name]], check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"powerprofilesctl failed: {exc}", file=sys.stderr)
        return 1
    print(f"profile: {args.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="victusctl", description="Control victus-thermald.")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="show live state")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)
    m = sub.add_parser("mode", help="set fan mode")
    m.add_argument("mode", choices=MODES)
    m.set_defaults(func=cmd_mode)
    pr = sub.add_parser("profile", help="show or set platform profile")
    pr.add_argument("name", nargs="?", choices=tuple(PPD_NAMES))
    pr.set_defaults(func=cmd_profile)
    q = sub.add_parser("ac-quiet", help="fans never stop on AC (on/off, remembered)")
    q.add_argument("state", nargs="?", choices=("on", "off"))
    q.set_defaults(func=cmd_ac_quiet)
    args = p.parse_args(argv)
    return args.func(args)
