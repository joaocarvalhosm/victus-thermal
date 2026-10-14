#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# victus-thermal installer for Ubuntu (Secure Boot aware).
#
#   sudo ./install.sh check          verify hardware, kernel, Secure Boot
#   sudo ./install.sh deps           install build dependencies (apt)
#   sudo ./install.sh mok            create/enroll the module signing key
#   sudo ./install.sh driver-test    build, sign and load the driver until reboot
#   sudo ./install.sh driver-install install the driver permanently (DKMS)
#   sudo ./install.sh daemon         install the fan control service
#   sudo ./install.sh all            everything, stopping where a reboot is needed
#
# Each step is idempotent and can be re-run.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly HERE
readonly BOARD="8C2F"
readonly DKMS_NAME="hp-wmi-victus-8c2f"
DKMS_VERSION="$(sed -n 's/^PACKAGE_VERSION="\(.*\)"/\1/p' "${HERE}/driver/dkms.conf")"
readonly DKMS_VERSION
readonly MOK_DIR="/var/lib/shim-signed/mok"
readonly MOK_DER="${MOK_DIR}/MOK.der"
readonly MOK_PRIV="${MOK_DIR}/MOK.priv"
readonly LIB_DIR="/usr/lib/victus-thermal"
readonly ETC_DIR="/etc/victus-thermal"
readonly GROUP="victus-thermal"
KVER="$(uname -r)"
readonly KVER

# exit status meaning "do what was printed (usually reboot), then re-run"
readonly EX_ACTION_NEEDED=3

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarn\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror\033[0m %s\n' "$*" >&2; exit 1; }

require_root() {
	[[ ${EUID} -eq 0 ]] || die "run with sudo: sudo $0 $*"
}

secure_boot_on() {
	mokutil --sb-state 2>/dev/null | grep -q "SecureBoot enabled"
}

hp_hwmon() {
	local d
	for d in /sys/devices/platform/hp-wmi/hwmon/hwmon*; do
		[[ -e ${d}/pwm1_enable ]] && { echo "${d}"; return 0; }
	done
	return 1
}

hp_wmi_platform_profile_registered() {
	grep -qx "hp-wmi" /sys/class/platform-profile/*/name 2>/dev/null
}

# ---------------------------------------------------------------------------

cmd_check() {
	local board
	board="$(cat /sys/class/dmi/id/board_name 2>/dev/null || true)"
	if [[ ${board} == "${BOARD}" ]]; then
		ok "board ${board} (HP Victus 15-fb2xxx)"
	elif [[ ${VICTUS_FORCE:-0} == 1 ]]; then
		warn "board '${board}' is not ${BOARD}; continuing because VICTUS_FORCE=1"
	else
		die "board '${board}' is not ${BOARD}. This driver patch is only for the Victus 15-fb2xxx."
	fi

	[[ ${KVER} == 7.0.* ]] || die "kernel ${KVER} is not 7.0.x; driver/hp-wmi.c must be rebased first"
	ok "kernel ${KVER}"

	[[ -d /lib/modules/${KVER}/build ]] || warn "kernel headers missing (run: sudo $0 deps)"

	python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
		|| die "python 3.11+ required"
	ok "python $(python3 -V | cut -d' ' -f2)"

	if secure_boot_on; then
		if [[ -r ${MOK_DER} ]] && mokutil --test-key "${MOK_DER}" 2>&1 | grep -q "already enrolled"; then
			ok "Secure Boot on, signing key enrolled"
		else
			warn "Secure Boot on, signing key not enrolled yet (run: sudo $0 mok)"
		fi
	else
		ok "Secure Boot off (modules need no signature)"
	fi

	if hp_wmi_platform_profile_registered; then
		ok "hp-wmi thermal profiles active"
	else
		info "hp-wmi thermal profiles not active (expected before the driver is installed)"
	fi
}

cmd_deps() {
	info "installing build dependencies"
	apt-get update -qq
	DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
		dkms build-essential "linux-headers-${KVER}" mokutil shim-signed python3
	ok "dependencies installed"
}

cmd_mok() {
	if ! secure_boot_on; then
		ok "Secure Boot is off, no signing key needed"
		return 0
	fi
	if [[ ! -r ${MOK_DER} || ! -r ${MOK_PRIV} ]]; then
		info "creating module signing key in ${MOK_DIR}"
		update-secureboot-policy --new-key
	fi
	if mokutil --test-key "${MOK_DER}" 2>&1 | grep -q "already enrolled"; then
		ok "signing key enrolled"
		return 0
	fi
	cat <<-EOF

	The signing key must be enrolled in the firmware. You will now choose a
	one-time password (any, 8+ characters). Then:

	  1. reboot
	  2. a blue "MOK management" screen appears: Enroll MOK -> Continue -> Yes
	  3. type the password, then Reboot
	  4. run: sudo $0 all

	EOF
	mokutil --import "${MOK_DER}"
	exit "${EX_ACTION_NEEDED}"
}

build_signed_module() {
	local out="$1" build status=0
	build="$(mktemp -d)"
	(
		set -e
		cp "${HERE}/driver/hp-wmi.c" "${HERE}/driver/Makefile" "${build}/"
		make -s -C "/lib/modules/${KVER}/build" M="${build}" modules >/dev/null
		if secure_boot_on; then
			"/usr/src/linux-headers-${KVER}/scripts/sign-file" sha512 \
				"${MOK_PRIV}" "${MOK_DER}" "${build}/hp-wmi.ko"
		fi
		install -m 0644 "${build}/hp-wmi.ko" "${out}"
	) || status=$?
	rm -rf "${build}"
	(( status == 0 )) || die "module build failed"
}

restore_stock_driver() {
	rmmod hp_wmi 2>/dev/null || true
	modprobe hp_wmi || warn "could not reload the stock hp-wmi; it will load on next boot"
}

cmd_driver_test() {
	cmd_check >/dev/null
	if secure_boot_on && ! mokutil --test-key "${MOK_DER}" 2>&1 | grep -q "already enrolled"; then
		die "signing key not enrolled; run: sudo $0 mok"
	fi

	local ko="/run/victus-thermal-test-hp-wmi.ko"
	info "building and signing patched hp-wmi for ${KVER}"
	build_signed_module "${ko}"
	ok "built $(modinfo -F srcversion "${ko}")"

	info "swapping in the patched driver (temporary: a reboot restores the stock one)"
	modprobe -r hp_wmi 2>/dev/null || rmmod hp_wmi 2>/dev/null || true
	local since
	since="$(date '+%Y-%m-%d %H:%M:%S')"
	if ! insmod "${ko}"; then
		restore_stock_driver
		die "the kernel refused the module (see: journalctl -k -n 20); stock driver restored"
	fi
	sleep 3

	local failed=0 hw
	if hw="$(hp_hwmon)"; then ok "fan interface at ${hw}"; else warn "no fan interface"; failed=1; fi
	if [[ -n ${hw:-} && -e ${hw}/pwm1 ]]; then ok "manual fan control (pwm1) available"
	else warn "pwm1 missing: firmware rejected the Victus S fan table"; failed=1; fi
	if hp_wmi_platform_profile_registered; then
		ok "thermal profiles: $(cat /sys/firmware/acpi/platform_profile_choices)"
	else warn "thermal profiles not registered"; failed=1; fi
	if [[ -n ${hw:-} ]]; then
		ok "fans: $(cat "${hw}/fan1_input" 2>/dev/null || echo ?) / $(cat "${hw}/fan2_input" 2>/dev/null || echo ?) RPM"
	fi
	local errors
	errors="$(journalctl -k --since "${since}" --no-pager -o cat 2>/dev/null \
		| grep -iE 'hp_wmi|hp-wmi' | grep -iE 'fail|error|unable' || true)"
	if [[ -n ${errors} ]]; then
		warn "driver reported:"; printf '     %s\n' "${errors}" >&2; failed=1
	fi
	rm -f "${ko}"

	if (( failed )); then
		restore_stock_driver
		die "patched driver did not work on this machine; stock driver restored, nothing installed"
	fi
	cat <<-EOF

	The patched driver is running until the next reboot. Try it now:
	  - switch power mode in GNOME (or: powerprofilesctl set performance)
	  - watch fans: watch -n1 cat ${hw}/fan1_input ${hw}/fan2_input
	  - manual max: echo 0 | sudo tee ${hw}/pwm1_enable   (back to auto: echo 2)
	If everything behaves, make it permanent: sudo $0 driver-install

	EOF
}

cmd_driver_install() {
	cmd_check >/dev/null
	local src="/usr/src/${DKMS_NAME}-${DKMS_VERSION}"
	info "installing ${DKMS_NAME} ${DKMS_VERSION} with DKMS"
	if dkms status -m "${DKMS_NAME}" -v "${DKMS_VERSION}" 2>/dev/null | grep -q .; then
		dkms remove -m "${DKMS_NAME}" -v "${DKMS_VERSION}" --all >/dev/null
	fi
	rm -rf "${src}"
	install -d -m 0755 "${src}"
	install -m 0644 "${HERE}/driver/hp-wmi.c" "${HERE}/driver/Makefile" \
		"${HERE}/driver/dkms.conf" "${src}/"
	dkms add -m "${DKMS_NAME}" -v "${DKMS_VERSION}"
	dkms install -m "${DKMS_NAME}" -v "${DKMS_VERSION}" -k "${KVER}"

	local path
	path="$(modinfo -n hp_wmi)"
	[[ ${path} == */updates/* ]] || die "modprobe still resolves the stock module (${path})"
	ok "modprobe now uses ${path}"
	if secure_boot_on; then
		[[ -n $(modinfo -F signer hp_wmi) ]] || die "DKMS did not sign the module"
		ok "signed by: $(modinfo -F signer hp_wmi)"
	fi

	if ! hp_wmi_platform_profile_registered; then
		info "loading the installed driver"
		modprobe -r hp_wmi 2>/dev/null || true
		modprobe hp_wmi
	fi
	if hp_wmi_platform_profile_registered; then
		ok "thermal profiles active"
	else
		warn "thermal profiles not active yet; reboot"
	fi
}

cmd_daemon() {
	info "installing victus-thermald"
	local user="${SUDO_USER:-}"

	for svc in victus-fan.service victus-backend.service; do
		if systemctl list-unit-files "${svc}" 2>/dev/null | grep -q "${svc}"; then
			warn "disabling ${svc}: it drives the same fan control"
			systemctl disable --now "${svc}" || true
		fi
	done

	getent group "${GROUP}" >/dev/null || groupadd --system "${GROUP}"
	if [[ -n ${user} && ${user} != root ]]; then
		usermod -aG "${GROUP}" "${user}"
		ok "added ${user} to ${GROUP} (log out and back in to use 'victusctl mode')"
	fi

	# Code run as root must not be writable by anyone else.
	rm -rf "${LIB_DIR}"
	install -d -o root -g root -m 0755 "${LIB_DIR}/victus_thermal" "${LIB_DIR}/bin"
	install -o root -g root -m 0644 "${HERE}"/victus_thermal/*.py "${LIB_DIR}/victus_thermal/"
	install -o root -g root -m 0755 "${HERE}"/bin/victus-thermald "${HERE}"/bin/victusctl "${LIB_DIR}/bin/"
	install -o root -g root -m 0755 "${HERE}/packaging/restore-firmware-fans" "${LIB_DIR}/"
	python3 -m compileall -q "${LIB_DIR}/victus_thermal"
	ln -sf "${LIB_DIR}/bin/victusctl" /usr/bin/victusctl

	install -d -m 0755 "${ETC_DIR}"
	if [[ -e ${ETC_DIR}/config.toml ]]; then
		ok "keeping existing ${ETC_DIR}/config.toml"
	else
		install -m 0644 "${HERE}/packaging/config.toml" "${ETC_DIR}/config.toml"
	fi

	install -d -m 0755 /usr/share/doc/victus-thermal
	install -m 0644 "${HERE}/README.md" /usr/share/doc/victus-thermal/
	install -m 0644 "${HERE}/packaging/victus-thermal.tmpfiles" /usr/lib/tmpfiles.d/victus-thermal.conf
	install -m 0644 "${HERE}/packaging/victus-thermal.service" /etc/systemd/system/
	systemd-tmpfiles --create /usr/lib/tmpfiles.d/victus-thermal.conf
	systemctl daemon-reload
	systemctl enable victus-thermal.service >/dev/null
	systemctl restart victus-thermal.service
	sleep 3
	systemctl is-active --quiet victus-thermal.service \
		|| die "service failed to start: journalctl -u victus-thermal -n 30"
	ok "victus-thermal is running"
	"${LIB_DIR}/bin/victusctl" status || true
}

cmd_all() {
	cmd_check
	cmd_deps
	cmd_mok
	if hp_wmi_platform_profile_registered && [[ $(modinfo -n hp_wmi) == */updates/* ]]; then
		ok "patched driver already installed"
	else
		cmd_driver_test
		cmd_driver_install
	fi
	cmd_daemon
}

main() {
	local cmd="${1:-}"
	case "${cmd}" in
	check | deps | mok | driver-test | driver-install | daemon | all)
		require_root "$@"
		"cmd_${cmd//-/_}"
		;;
	*)
		sed -n '4,14p' "$0" | sed 's/^# \{0,1\}//'
		exit 1
		;;
	esac
}

main "$@"
