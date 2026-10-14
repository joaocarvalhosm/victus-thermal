#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Remove victus-thermal and return to the stock hp-wmi driver.
#
#   sudo ./uninstall.sh          remove service + driver, keep config
#   sudo ./uninstall.sh --purge  also remove /etc/victus-thermal and the group
#
# The MOK signing key is left enrolled: it is shared with other DKMS modules
# (e.g. NVIDIA). Remove it only if nothing else needs it:
#   sudo mokutil --delete /var/lib/shim-signed/mok/MOK.der

set -euo pipefail

readonly DKMS_NAME="hp-wmi-victus-8c2f"

[[ ${EUID} -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
purge=0
[[ ${1:-} == --purge ]] && purge=1

echo "==> stopping service"
if [[ $(systemctl list-unit-files victus-thermal.service 2>/dev/null) == *victus-thermal* ]]; then
	systemctl disable --now victus-thermal.service || true
fi
for f in /sys/devices/platform/hp-wmi/hwmon/hwmon*/pwm1_enable; do
	[[ -w ${f} ]] && echo 2 > "${f}"
done

echo "==> removing files"
rm -f /etc/systemd/system/victus-thermal.service /usr/lib/tmpfiles.d/victus-thermal.conf
rm -f /usr/bin/victusctl
rm -rf /usr/lib/victus-thermal /usr/share/doc/victus-thermal /run/victus-thermal
systemctl daemon-reload

echo "==> removing driver"
if command -v dkms >/dev/null && [[ -n $(dkms status -m "${DKMS_NAME}" 2>/dev/null) ]]; then
	for v in $(dkms status -m "${DKMS_NAME}" | sed -n 's|^[^/]*/\([^,:]*\).*|\1|p' | sort -u); do
		dkms remove -m "${DKMS_NAME}" -v "${v}" --all
		rm -rf "/usr/src/${DKMS_NAME}-${v}"
	done
	depmod -a
	modprobe -r hp_wmi 2>/dev/null || true
	modprobe hp_wmi || echo "stock hp-wmi will load on next boot"
fi

if (( purge )); then
	echo "==> purging configuration"
	rm -rf /etc/victus-thermal
	getent group victus-thermal >/dev/null && groupdel victus-thermal
fi

echo "done: modprobe resolves $(modinfo -n hp_wmi)"
