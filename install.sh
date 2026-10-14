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
#   sudo ./install.sh extension      install the GNOME top-bar extension for you
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
readonly USER_TMPFILES="/etc/tmpfiles.d/victus-thermal-user.conf"
# sha256 of every config.toml shipped before the current one. An installed
# config matching one of these was never edited and is safe to upgrade.
readonly PRISTINE_CONFIGS=(
	44a76bc7ac9e7e11e03d961a30a27f75242643c311219830b9863c097ea82649  # 1.0.0
	369f85c558a918e99f0a52184a8594d365ece129d7a43a9b4cef498c8e8d31f3  # 1.1.0 - 1.3.0
	f42311369d24bf29da9a2810e4c53254feac4b9d70969f31fe0107bb1b29a83c  # 1.4.0
	a2f57f820f5b92d46c637d33b7b0ebd8f7661a56b415bd92c2fbd8449b1b495f  # 1.5.0
)
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

# Note: commands are captured and matched instead of piped into grep -q.
# Under pipefail a pipeline fails if the left side exits non-zero, and
# `mokutil --test-key` exits 1 even when it reports "already enrolled".

secure_boot_on() {
	[[ $(mokutil --sb-state 2>/dev/null) == *"SecureBoot enabled"* ]]
}

mok_enrolled() {
	[[ -r ${MOK_DER} && $(mokutil --test-key "${MOK_DER}" 2>&1) == *"already enrolled"* ]]
}

hp_hwmon() {
	local d
	for d in /sys/devices/platform/hp-wmi/hwmon/hwmon*; do
		[[ -e ${d}/pwm1_enable ]] && { echo "${d}"; return 0; }
	done
	return 1
}

# Versions of our DKMS package installed for the running kernel.
installed_driver_versions() {
	dkms status -m "${DKMS_NAME}" -k "${KVER}" 2>/dev/null \
		| sed -n 's|^[^/]*/\([^,:]*\).*: installed.*|\1|p'
}

# UPower only looks for keyboard lights when it starts; make GNOME see ours.
refresh_upower() {
	if [[ -e /sys/class/leds/hp::kbd_backlight ]] && systemctl is-active --quiet upower; then
		systemctl restart upower || warn "could not restart upower; reboot to enable keyboard light control"
	fi
}

# True when the hp_wmi in memory is the one modprobe would load from disk.
loaded_driver_is_current() {
	[[ -r /sys/module/hp_wmi/srcversion ]] \
		&& [[ $(cat /sys/module/hp_wmi/srcversion) == "$(modinfo -F srcversion hp_wmi 2>/dev/null)" ]]
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
		if mok_enrolled; then
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
	if mok_enrolled; then
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
	if secure_boot_on && ! mok_enrolled; then
		die "signing key not enrolled; run: sudo $0 mok"
	fi

	local ko="/run/victus-thermal-test-hp-wmi.ko"
	info "building and signing patched hp-wmi for ${KVER}"
	build_signed_module "${ko}"
	ok "built $(modinfo -F srcversion "${ko}")"

	info "swapping in the patched driver (temporary: a reboot restores the stock one)"
	modprobe -r hp_wmi 2>/dev/null || rmmod hp_wmi 2>/dev/null || true
	# modprobe -r also unloads dependencies left unused (sparse-keymap), and
	# insmod does not resolve dependencies, so load the declared ones first.
	local dep
	for dep in $(modinfo -F depends "${ko}" | tr ',' ' '); do
		modprobe "${dep}" || die "cannot load dependency ${dep}"
	done
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
	# Keyboard light is optional: report what the BIOS answered, never fail.
	local kbd
	kbd="$(journalctl -k --since "${since}" --no-pager -o cat 2>/dev/null \
		| grep -m1 'keyboard backlight:' || true)"
	if [[ -e /sys/class/leds/hp::kbd_backlight ]]; then
		ok "keyboard light control available (${kbd#*: })"
		refresh_upower
	else
		warn "no keyboard light control (${kbd:-no answer from the BIOS}); fans are unaffected"
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
	# Remove every version of ours first, so only one is ever installed.
	local v
	for v in $(dkms status -m "${DKMS_NAME}" 2>/dev/null \
		| sed -n 's|^[^/]*/\([^,:]*\).*|\1|p' | sort -u); do
		dkms remove -m "${DKMS_NAME}" -v "${v}" --all >/dev/null
		rm -rf "/usr/src/${DKMS_NAME}-${v}"
	done
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

	if ! loaded_driver_is_current; then
		info "loading the installed driver"
		modprobe -r hp_wmi 2>/dev/null || true
		modprobe hp_wmi
		refresh_upower
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
		if [[ $(systemctl list-unit-files "${svc}" 2>/dev/null) == *"${svc}"* ]]; then
			warn "disabling ${svc}: it drives the same fan control"
			systemctl disable --now "${svc}" || true
		fi
	done

	getent group "${GROUP}" >/dev/null || groupadd --system "${GROUP}"
	if [[ -n ${user} && ${user} != root ]]; then
		usermod -aG "${GROUP}" "${user}"
		# Group membership only reaches new logins; an ACL for the installing
		# user lets the running desktop session change the fan mode right away.
		{
			echo "# written by victus-thermal install.sh"
			printf 'a+ %s - - - - u:%s:rwx\n' \
				/run/victus-thermal/control "${user}" \
				/var/lib/victus-thermal "${user}"
		} > "${USER_TMPFILES}"
		chmod 0644 "${USER_TMPFILES}"
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
	local cfg="${ETC_DIR}/config.toml" current
	if [[ -e ${cfg} ]]; then
		current="$(sha256sum "${cfg}" | cut -d' ' -f1)"
		if [[ " ${PRISTINE_CONFIGS[*]} " == *" ${current} "* ]]; then
			install -m 0644 "${HERE}/packaging/config.toml" "${cfg}"
			ok "upgraded unmodified ${cfg} to the new defaults"
		elif ! cmp -s "${cfg}" "${HERE}/packaging/config.toml"; then
			ok "keeping your edited ${cfg} (new defaults: ${HERE}/packaging/config.toml)"
		fi
	else
		install -m 0644 "${HERE}/packaging/config.toml" "${cfg}"
	fi

	install -d -m 0755 /usr/share/doc/victus-thermal
	install -m 0644 "${HERE}/README.md" /usr/share/doc/victus-thermal/
	install -m 0644 "${HERE}/packaging/victus-thermal.tmpfiles" /usr/lib/tmpfiles.d/victus-thermal.conf
	install -m 0644 "${HERE}/packaging/victus-thermal.service" /etc/systemd/system/
	local tmpfiles=(/usr/lib/tmpfiles.d/victus-thermal.conf)
	[[ -e ${USER_TMPFILES} ]] && tmpfiles+=("${USER_TMPFILES}")
	systemd-tmpfiles --create "${tmpfiles[@]}"
	if [[ -n ${user} && ${user} != root ]]; then
		# bash's [[ -w ]] asks the kernel (access(2)), which honours ACLs; the
		# uutils /usr/bin/test shipped by Ubuntu 26.04 does not.
		# shellcheck disable=SC2016  # $1 is expanded by the inner bash
		if runuser -u "${user}" -- bash -c '[[ -w $1 ]]' _ /run/victus-thermal/control; then
			ok "${user} can change the fan mode now (no re-login needed)"
		else
			warn "${user} cannot write the fan mode yet; log out and back in"
		fi
	fi
	systemctl daemon-reload
	systemctl enable victus-thermal.service >/dev/null
	systemctl restart victus-thermal.service
	sleep 3
	systemctl is-active --quiet victus-thermal.service \
		|| die "service failed to start: journalctl -u victus-thermal -n 30"
	ok "victus-thermal is running"
	"${LIB_DIR}/bin/victusctl" status || true
}

cmd_extension() {
	local user="${SUDO_USER:-}"
	if [[ -z ${user} || ${user} == root ]]; then
		warn "run through sudo from your desktop user to install the GNOME extension"
		return 0
	fi
	local uid
	uid="$(id -u "${user}")"
	info "installing the GNOME extension for ${user}"
	# The extension is per-user and talks to the user's session bus.
	if runuser -u "${user}" -- env \
		HOME="$(getent passwd "${user}" | cut -d: -f6)" \
		XDG_RUNTIME_DIR="/run/user/${uid}" \
		DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${uid}/bus" \
		"${HERE}/gnome-extension/install.sh"; then
		ok "GNOME extension installed"
	else
		warn "GNOME extension not installed; run ./gnome-extension/install.sh without sudo"
	fi
}

cmd_all() {
	cmd_check
	cmd_deps
	cmd_mok
	if [[ $(installed_driver_versions) == "${DKMS_VERSION}" ]] && loaded_driver_is_current; then
		ok "patched driver ${DKMS_VERSION} already installed and loaded"
	else
		cmd_driver_test
		cmd_driver_install
	fi
	cmd_daemon
	cmd_extension
}

main() {
	local cmd="${1:-}"
	case "${cmd}" in
	check | deps | mok | driver-test | driver-install | daemon | extension | all)
		require_root "$@"
		"cmd_${cmd//-/_}"
		;;
	*)
		sed -n '4,15p' "$0" | sed 's/^# \{0,1\}//'
		exit 1
		;;
	esac
}

main "$@"
