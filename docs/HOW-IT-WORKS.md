# How it works

**English** | [Português](HOW-IT-WORKS.pt-BR.md)

This document explains the project's technical decisions. To use it, the
[README](../README.md) is all you need.

## 1. The driver

The kernel's `hp-wmi` picks the thermal protocol by board (`DMI_BOARD_NAME`),
from a table. 8C2F uses the **Victus S** protocol, the same as boards 8BBE,
8BD4, 8BD5, 8C99, 8C9C and 8D41. This was independently confirmed by
omen-fan-control v2.1.0, tested on the 15-fb2xxx.

`driver/hp-wmi.c` is an **exact copy** of the file in the
`linux-source-7.0.0` package (Ubuntu 7.0.0-38.38), plus one table entry:

```c
{
	.matches = { DMI_MATCH(DMI_BOARD_NAME, "8C2F") },
	.driver_data = (void *)&victus_s_thermal_params,
},
```

`victus_s_thermal_params` uses `HP_EC_OFFSET_UNKNOWN`: the driver **never
reads or writes embedded controller (EC) registers**. Everything goes
through WMI methods that the BIOS implements and validates. With that, the
driver gains:

| Feature | How it shows up |
|---|---|
| HP thermal modes | `platform_profile`: `low-power`, `balanced`, `performance` |
| GPU boost (cTGP / PPAB) | together with the profile |
| CPU power limits | reset to the default when the power source changes |
| Fan RPM | `fan1_input`, `fan2_input` |
| Manual control | `pwm1_enable` (0 = maximum, 1 = manual, 2 = firmware) and `pwm1` |
| Keyboard light¹ | LED `hp::kbd_backlight` (0/1), through UPower for GNOME |

¹ A local addition, not sent upstream yet. It uses OMEN's WMI command
`0x20009`: subcommand 4 reads the keyboard brightness register in the EC,
and 5 writes it and tells the EC to apply it (bit 7 = on; `0x64` turns it
off, `0xE4` turns it on), following the `LM04`/`LM05` methods in the 8C2F
DSDT. Event `0x0D` (the Fn shortcut) becomes `brightness_hw_changed` for
UPower. It is only registered on `victus_s` boards whose BIOS sets bit 0 of
the capabilities query (subcommand 1, method `LM01`); the reply goes to the
log (`keyboard backlight: caps … state …`). **On 8C2F that bit is 0**
(`caps [06 3c]`): the commands are accepted and the value reads back, but
the controller does not turn the white light on or off, which only responds
to the Fn shortcut. That is why the LED is not registered there.

The firmware reverts manual mode after 120 s; the driver itself resends the
command every 90 s (*keep-alive*).

**DKMS:** `dkms.conf` limits the build to kernel `7.0.x`, because the
internal APIs change between series. On another kernel nothing is built, and
Ubuntu's original driver keeps working. Ubuntu's DKMS signs the module with
the MOK key in `/var/lib/shim-signed/mok/`.

**Test before installing:** `driver-test` builds and signs the module,
unloads the original one, loads the declared dependencies (`sparse-keymap`,
`wmi`, `platform_profile`), inserts the new one and checks the fan
interface, `pwm1`, the profile registration and kernel errors. Any failure
restores the original driver.

## 2. The service (`victus-thermald`)

A loop every 2 s: read the sensors, decide and apply. Between steps it
checks, every 0.2 s, whether the requested mode, the AC power preference or
the profile changed. If they did, it decides right away.

### Order of decisions (`policy.py`)

1. **Emergency:** any sensor ≥ `emergency_temp` (raw reading, no smoothing)
   → **maximum**, until all are ≤ `emergency_release`.
2. **Mode forced by the user:** `auto` → firmware; `max` → maximum, if the
   profile is in `max_profiles`. Outside them, the request is deleted and the
   mode goes back to `curve`.
3. **No CPU sensor** → firmware.
4. **Profile configured as `"firmware"`** → firmware.
5. **Curve:** the input is the hottest sensor (CPU, iGPU, NVIDIA), smoothed
   (exponential average) and with hysteresis on the way down.
   - **On AC power with `ac_quiet`:** never below `ac_quiet_duty`.
   - **Curve at 0 %:** firmware, or a low speed for as long as `hold_time`
     lasts after the curve has turned the fans on.
   - **Taking over from the firmware:** only after `engage_delay` s of
     continuous demand, measured on the smoothed temperature **without**
     hysteresis (with hysteresis, every spike would look sustained). A curve
     at 100 % takes over immediately.
   - **Curve at 100 %** → maximum; otherwise, manual with the curve's `pwm`.

### Applying (`daemon.py`)

- When switching from **firmware to manual**, it reads the firmware's
  current speed (`pwm1`) and uses it as a **floor**: the curve never takes
  over slower than the firmware was spinning.
- `pwm` changes smaller than 3/255 are not written (each write is a WMI
  call).
- On exit, for any reason, it writes `pwm1_enable = 2` (firmware).
  `ExecStopPost` repeats this for the cases where Python cannot.

### Costs measured on the 8C2F

| Reading | Cost | How it is handled |
|---|---|---|
| `fanN_input` (RPM) | **~310 ms** of BIOS time each, holding the ACPI interpreter | 10 s cache, plus one reading 3 s after each change |
| `pwm1_enable` | ~0.1 ms | every decision |
| temperatures (`k10temp`, `amdgpu`) | < 2 ms | every decision |
| `nvidia-smi` | ~22 ms of CPU, and counts as GPU activity | see below |

Result: ~1% CPU and ~13% BIOS time (before the optimisation: ~6% and
~50%).

### NVIDIA without waking it

Each `nvidia-smi` query resets the time until the GPU goes to sleep. That is
why (`DgpuSampler` in `hw.py`):

- GPU suspended → never queries;
- woke up less than 10 s ago → does not query (it will probably go back to
  sleep);
- awake and idle → at most every 30 s;
- in use → every 5 s.

## 3. Permissions

| Path | Owner | Content |
|---|---|---|
| `/run/victus-thermal/status.json` | root, 0644 | current state (readable by everyone) |
| `/run/victus-thermal/control/mode` | `victus-thermal` group + user ACL | mode request; deleted on every restart |
| `/var/lib/victus-thermal/ac-quiet` | `victus-thermal` group + user ACL | remembered preference (`on`/`off`) |
| `/etc/victus-thermal/config.toml` | root | configuration |

The request files are **untrusted data**: the service opens them with
`O_NOFOLLOW`, reads at most 16 bytes and only accepts known words. The ACL
for the user who installed it applies immediately; the group only applies
from the next login.

The systemd unit runs with an empty `CapabilityBoundingSet=`,
`ProtectSystem=strict`, `PrivateNetwork`, `NoNewPrivileges`,
`MemoryDenyWriteExecute` and a syscall filter, and with `WatchdogSec=30`.

## 4. GNOME extension

It reads `status.json` with a file monitor (the service replaces the file
with an atomic rename), so it updates ~1–3 ms after each decision. A 5 s
timer is only there to detect a stopped service. The actions call
`victusctl`; the extension has no privileges.

## 5. Keyboard light in the extension

`keyboardLight.js` talks to UPower (`org.freedesktop.UPower.KbdBacklight`)
and uses Mutter's idle monitor: after `kbd-auto-off-seconds` without input,
it turns the light off if it is on and saves the level; on the first input,
it turns it back on 0.4 s later. The delay makes sure this happens after the
restore that GNOME itself does when the screen wakes up. A light turned off
by the user is never turned on. The preferences live in GSettings
(`org.gnome.shell.extensions.victus-thermal`).

## 6. NVIDIA on demand

The Vulkan loader loads **every** installed driver when any Vulkan app
opens, and the NVIDIA driver opens the GPU when it loads (measured: 15 opens
of `/dev/nvidia*` per process). `extras/gpu-on-demand` sets
`VK_LOADER_DRIVERS_SELECT=*radeon*` in the session. Game launchers get
`PrefersNonDefaultGPU=true`: GNOME starts them with switcheroo-control's
environment for the NVIDIA GPU, which overrides the same variable with
`*nvidia*` and turns on PRIME offload for OpenGL.

The laptop's HDMI port is wired to the NVIDIA GPU, so GNOME keeps the device
open; that does not stop the GPU from sleeping.
