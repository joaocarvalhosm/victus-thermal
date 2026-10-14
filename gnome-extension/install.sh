#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Install the Victus Thermal GNOME Shell extension for the current user.
# Run WITHOUT sudo:  ./gnome-extension/install.sh
# Remove:            ./gnome-extension/install.sh --uninstall

set -euo pipefail

readonly UUID="victus-thermal@joaocarvalhosm.github.io"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly HERE
readonly DEST="${XDG_DATA_HOME:-${HOME}/.local/share}/gnome-shell/extensions/${UUID}"

if [[ ${EUID} -eq 0 ]]; then
	echo "run as your normal user, not with sudo (extensions are per-user)" >&2
	exit 1
fi

if [[ ${1:-} == --uninstall ]]; then
	gnome-extensions disable "${UUID}" 2>/dev/null || true
	rm -rf "${DEST}"
	echo "removed ${UUID}"
	exit 0
fi

rm -rf "${DEST}"
install -d "${DEST}/icons" "${DEST}/schemas"
install -m 0644 "${HERE}/${UUID}"/{metadata.json,stylesheet.css} "${HERE}/${UUID}"/*.js "${DEST}/"
install -m 0644 "${HERE}/${UUID}/icons/victus-fan-symbolic.svg" "${DEST}/icons/"
install -m 0644 "${HERE}/${UUID}"/schemas/*.gschema.xml "${DEST}/schemas/"
glib-compile-schemas --strict "${DEST}/schemas"
echo "installed to ${DEST}"

if gnome-extensions info "${UUID}" >/dev/null 2>&1; then
	gnome-extensions enable "${UUID}"
	echo "enabled. If it was already running, log out and back in to load the new version."
else
	# GNOME on Wayland only discovers new extensions at login.
	gsettings set org.gnome.shell enabled-extensions \
		"$(python3 -c 'import ast,sys; l=ast.literal_eval(sys.argv[1] or "[]"); u=sys.argv[2]; print(l if u in l else l+[u])' \
			"$(gsettings get org.gnome.shell enabled-extensions | sed 's/^@as //')" "${UUID}")"
	echo "log out and back in; the indicator then appears in the top bar."
fi

if [[ " $(id -nG) " != *" victus-thermal "* ]]; then
	echo "note: changing the fan mode needs the 'victus-thermal' group (sudo ./install.sh daemon adds it; then log in again)."
fi
