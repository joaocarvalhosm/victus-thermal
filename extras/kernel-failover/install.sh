#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Kernel failover: boot the newest generic kernel, and fall back to the
# newest OEM kernel automatically when a boot fails.
#   sudo extras/kernel-failover/install.sh               install
#   sudo extras/kernel-failover/install.sh --uninstall   remove
#
# Needs both flavours installed and kept up to date:
#   sudo apt install linux-generic-hwe-26.04 linux-oem-26.04

set -euo pipefail

readonly HOOK=/etc/grub.d/06_victus_kernel_failover
readonly DEFAULTS=/etc/default/grub
readonly TIMEOUT=10   # seconds the menu waits after a failed boot

[[ ${EUID} -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

if [[ ${1:-} == --uninstall ]]; then
	rm -f "${HOOK}"
	sed -i '/^GRUB_RECORDFAIL_TIMEOUT=.*# victus-thermal$/d' "${DEFAULTS}"
	update-grub
	echo "removed: GRUB_DEFAULT from ${DEFAULTS} applies again"
	exit 0
fi

ls /boot/vmlinuz-*-generic >/dev/null 2>&1 || { echo "no generic kernel installed" >&2; exit 1; }
ls /boot/vmlinuz-*-oem >/dev/null 2>&1 || echo "warning: no OEM kernel installed; there will be no fallback" >&2

install -m 0755 "$(dirname "$0")/06_victus_kernel_failover" "${HOOK}"
if ! grep -q '^GRUB_RECORDFAIL_TIMEOUT=' "${DEFAULTS}"; then
	echo "GRUB_RECORDFAIL_TIMEOUT=${TIMEOUT} # victus-thermal" >> "${DEFAULTS}"
fi
update-grub
echo "installed: generic kernel by default, OEM kernel after a failed boot"
