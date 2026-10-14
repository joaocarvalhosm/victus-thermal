#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# NVIDIA strictly on demand (hybrid AMD iGPU + NVIDIA dGPU, GNOME).
# Run WITHOUT sudo:  extras/gpu-on-demand/install.sh
# Undo:              extras/gpu-on-demand/install.sh --uninstall
# Both take effect at the next login.
#
# Why: the Vulkan loader loads every installed driver when any Vulkan app
# starts (GTK4 apps such as the terminal render with Vulkan), and the NVIDIA
# driver opens the GPU while loading. That keeps the dGPU powered (~2 W) even
# though nothing renders on it.
#
# What it does:
#   1. Session default: Vulkan sees only the AMD iGPU
#      (VK_LOADER_DRIVERS_SELECT=*radeon*).
#   2. Game launchers get PrefersNonDefaultGPU=true, so GNOME starts them
#      with switcheroo-control's NVIDIA environment, which overrides (1) with
#      VK_LOADER_DRIVERS_SELECT=*nvidia* and turns on PRIME offload for
#      OpenGL. Their games inherit it and run on the RTX.
# Any other app can still be started on the NVIDIA GPU from the GNOME
# dock/app grid: right click -> "Launch using Discrete Graphics Card".

set -euo pipefail

readonly ENV_FILE="${XDG_CONFIG_HOME:-${HOME}/.config}/environment.d/90-victus-gpu-on-demand.conf"
readonly APPS_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
readonly MARKER="X-Victus-GPU-Override=true"
readonly LAUNCHERS=(
	steam.desktop
	net.lutris.Lutris.desktop
	com.heroicgameslauncher.hgl.desktop
	com.usebottles.bottles.desktop
	tlauncher.desktop
)
readonly SYSTEM_APP_DIRS=(
	/usr/share/applications
	/usr/local/share/applications
	/var/lib/flatpak/exports/share/applications
	/var/lib/snapd/desktop/applications
)

if [[ ${EUID} -eq 0 ]]; then
	echo "run as your normal user, not with sudo" >&2
	exit 1
fi

find_system_entry() {
	local dir
	for dir in "${SYSTEM_APP_DIRS[@]}"; do
		[[ -f ${dir}/$1 ]] && { echo "${dir}/$1"; return 0; }
	done
	return 1
}

# Copy a launcher into the user's applications dir with the discrete-GPU
# keys added to its [Desktop Entry] group (not to [Desktop Action] groups).
mark_launcher() {
	local name="$1" src dest="${APPS_DIR}/$1"
	if [[ -f ${dest} ]] && ! grep -qx "${MARKER}" "${dest}"; then
		# the user's own override: only add the keys if they are missing
		if grep -q '^PrefersNonDefaultGPU=true' "${dest}"; then
			echo "  ${name}: already starts on the NVIDIA GPU"
			return 0
		fi
		src="${dest}"
	else
		src="$(find_system_entry "${name}")" || return 0
	fi
	local tmp
	tmp="$(mktemp "${APPS_DIR}/.${name}.XXXXXX")"
	awk -v marker="${MARKER}" '
		/^PrefersNonDefaultGPU=|^X-Victus-GPU-Override=/ { next }
		{ print }
		$0 == "[Desktop Entry]" {
			print "PrefersNonDefaultGPU=true"
			print marker
		}' "${src}" > "${tmp}"
	chmod 0644 "${tmp}"
	mv "${tmp}" "${dest}"
	echo "  ${name}: will start on the NVIDIA GPU"
}

if [[ ${1:-} == --uninstall ]]; then
	rm -f "${ENV_FILE}"
	for name in "${LAUNCHERS[@]}"; do
		if [[ -f ${APPS_DIR}/${name} ]] && grep -qx "${MARKER}" "${APPS_DIR}/${name}"; then
			rm -f "${APPS_DIR}/${name}"
			echo "  ${name}: restored"
		fi
	done
	echo "undone; log out and back in."
	exit 0
fi

if [[ $(gdbus call --system --dest net.hadess.SwitcherooControl \
	--object-path /net/hadess/SwitcherooControl \
	--method org.freedesktop.DBus.Properties.Get net.hadess.SwitcherooControl GPUs 2>/dev/null) \
	!= *"VK_LOADER_DRIVERS_SELECT"*"*nvidia*"* ]]; then
	echo "switcheroo-control does not provide an NVIDIA Vulkan selection;" >&2
	echo "games could lose the NVIDIA GPU, so nothing was changed." >&2
	exit 1
fi

mkdir -p "$(dirname "${ENV_FILE}")" "${APPS_DIR}"
cat > "${ENV_FILE}" <<'EOF'
# Written by victus-thermal extras/gpu-on-demand. Vulkan apps use the AMD
# iGPU unless started on the discrete GPU (which overrides this variable),
# so the NVIDIA GPU can power off when unused.
VK_LOADER_DRIVERS_SELECT=*radeon*
EOF
echo "session default: Vulkan on the AMD iGPU"
for name in "${LAUNCHERS[@]}"; do
	mark_launcher "${name}"
done
update-desktop-database "${APPS_DIR}" 2>/dev/null || true
echo "done; log out and back in."
