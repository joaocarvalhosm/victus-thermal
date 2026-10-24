#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Kernel failover: boot the newest generic kernel, and fall back to the
# newest OEM kernel automatically when a boot fails.
#   sudo extras/kernel-failover/install.sh               install
#   sudo extras/kernel-failover/install.sh --uninstall   remove
#   extras/kernel-failover/install.sh --status           what booted, and why
#
# Needs both flavours installed and kept up to date:
#   sudo apt install linux-generic-hwe-26.04 linux-oem-26.04

set -euo pipefail

readonly HOOK=/etc/grub.d/06_victus_kernel_failover
readonly DEFAULTS=/etc/default/grub
readonly TIMEOUT=10   # seconds the menu waits after a failed boot
readonly GRUBENV=/boot/grub/grubenv
# update-initramfs only rebuilds the "newest" kernel by version, and
# 7.0.0-1015-oem sorts above 7.0.0-38-generic, so the generic initrd would go
# stale on every systemd/udev/firmware update. "all" rebuilds every kernel.
readonly INITRAMFS_CONF=/etc/initramfs-tools/update-initramfs.conf
readonly INITRAMFS_LINE='update_initramfs=all # victus-thermal'

status() {
	local planned
	planned=$(grep -a "^victus_planned=" "${GRUBENV}" 2>/dev/null | cut -d= -f2- || true)
	echo "running kernel: $(uname -r)"
	echo "GRUB planned:   ${planned:-unknown (installed before this check existed)}"
	if [[ -z ${planned} ]]; then
		return
	elif [[ ${planned} == *failover ]]; then
		echo "result: failover, the previous boot did not finish"
	elif [[ ${planned} == "$(uname -r)" ]]; then
		echo "result: ok, the default kernel booted"
	else
		echo "result: GRUB planned another kernel; it was chosen in the menu," \
			"or GRUB could not load it and used the fallback"
	fi
}

if [[ ${1:-} == --status ]]; then
	status
	exit 0
fi

[[ ${EUID} -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

if [[ ${1:-} == --uninstall ]]; then
	rm -f "${HOOK}"
	sed -i '/^GRUB_RECORDFAIL_TIMEOUT=.*# victus-thermal$/d' "${DEFAULTS}"
	if [[ -f ${INITRAMFS_CONF} ]]; then
		sed -i "/^${INITRAMFS_LINE}$/d" "${INITRAMFS_CONF}"
		[[ -s ${INITRAMFS_CONF} ]] || rm -f "${INITRAMFS_CONF}"
	fi
	grub-editenv "${GRUBENV}" unset victus_planned 2>/dev/null || true
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
if grep -q '^update_initramfs=' "${INITRAMFS_CONF}" 2>/dev/null; then
	grep -qx "${INITRAMFS_LINE}" "${INITRAMFS_CONF}" ||
		echo "warning: ${INITRAMFS_CONF} already sets update_initramfs; left as is" >&2
else
	mkdir -p "$(dirname "${INITRAMFS_CONF}")"
	echo "${INITRAMFS_LINE}" >> "${INITRAMFS_CONF}"
fi
# bring every initrd up to date now, not only at the next update
update-initramfs -u -k all
update-grub
echo "installed: generic kernel by default, OEM kernel after a failed boot"
