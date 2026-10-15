#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Catch the driver behind "WARNING: kernel/workqueue.c:2351" automatically,
# on every boot, and save the evidence to disk before the system freezes.
#
#   sudo tools/workqueue-watch.sh install     start now and on every boot
#   sudo tools/workqueue-watch.sh uninstall   remove the service
#   sudo tools/workqueue-watch.sh report      show the saved captures
#        tools/workqueue-watch.sh run         (the service itself)
#
# Why: on this laptop the warning has preceded every hard freeze by 25 s to
# 2 min, and its stack trace never names the driver. The workqueue tracepoint
# records the work function of every queued item; traceoff_on_warning freezes
# that ring buffer at the WARN, so its last event on the warning CPU is the
# culprit. The buffer lives in RAM and is lost on a forced reboot, so this
# service writes it to /var/log/victus-workqueue/ the moment the warning is
# logged, then re-arms for the next one.

set -euo pipefail

readonly T=/sys/kernel/tracing
readonly EVENT="${T}/events/workqueue/workqueue_queue_work/enable"
readonly LOG_DIR=/var/log/victus-workqueue
readonly KEEP=20
readonly LIB=/usr/local/lib/victus-thermal/workqueue-watch.sh
readonly UNIT=/etc/systemd/system/victus-workqueue-watch.service

[[ ${EUID} -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

arm() {
	echo 0 > "${T}/tracing_on"
	echo > "${T}/trace"
	echo 4096 > "${T}/buffer_size_kb"          # per CPU
	echo 1 > "${EVENT}"
	echo 1 > /proc/sys/kernel/traceoff_on_warning
	echo 1 > "${T}/tracing_on"
}

capture() {
	local warn="$1" cpu out fn
	cpu="$(sed -n 's/.*CPU#\([0-9]*\).*/\1/p' <<<"${warn}")"
	out="${LOG_DIR}/$(date +%Y%m%d-%H%M%S).txt"
	{
		echo "time:    $(date --iso-8601=seconds)"
		echo "kernel:  $(uname -r)"
		echo "warning: ${warn}"
		echo
		echo "last queued work items on CPU ${cpu} (the final one tripped the WARN):"
		grep -E "\[0*${cpu}\]" "${T}/trace" | grep workqueue_queue_work | tail -8 || true
		fn="$(grep -E "\[0*${cpu}\]" "${T}/trace" | grep workqueue_queue_work | tail -1 \
			| sed -n 's/.*function=\([^ ]*\).*/\1/p' || true)"
		echo
		echo "culprit function: ${fn:-unknown}"
		if [[ -n ${fn} ]]; then grep -w "${fn}" /proc/kallsyms | head -3 || true; fi
		echo
		echo "most frequent work functions in the buffer (all CPUs):"
		grep -o 'function=[^ ]*' "${T}/trace" | sort | uniq -c | sort -rn | head -10
	} > "${out}.tmp"
	mv "${out}.tmp" "${out}"
	sync "${out}"
	echo "captured ${out} (culprit: ${fn:-unknown})"
	# keep the newest KEEP captures (names are timestamps: sorted = oldest first)
	local files=("${LOG_DIR}"/*.txt)
	if (( ${#files[@]} > KEEP )); then
		rm -f -- "${files[@]:0:${#files[@]}-KEEP}"
	fi
}

case "${1:-}" in
run)
	[[ -e ${EVENT} ]] || { echo "tracefs not available at ${T}" >&2; exit 1; }
	install -d -m 0755 "${LOG_DIR}"
	arm
	echo "armed; watching the kernel log"
	journalctl -k -f -n 0 -o cat | while IFS= read -r line; do
		if [[ ${line} == *"WARNING: kernel/workqueue.c"* ]]; then
			sleep 0.2   # let the WARN finish; the buffer is already frozen
			capture "${line}"
			arm
		fi
	done
	;;
install)
	install -d -m 0755 "$(dirname "${LIB}")"
	install -m 0755 "$0" "${LIB}"
	cat > "${UNIT}" <<-EOF
	[Unit]
	Description=Capture the driver behind workqueue.c:2351 warnings (victus-thermal diagnostics)
	After=systemd-journald.service

	[Service]
	ExecStart=${LIB} run
	ExecStopPost=/bin/sh -c 'echo 0 > ${EVENT}; echo 0 > /proc/sys/kernel/traceoff_on_warning; echo 1 > ${T}/tracing_on'
	Restart=on-failure

	[Install]
	WantedBy=multi-user.target
	EOF
	systemctl daemon-reload
	systemctl enable --now victus-workqueue-watch.service
	echo "installed: armed now and on every boot; captures go to ${LOG_DIR}/"
	;;
uninstall)
	systemctl disable --now victus-workqueue-watch.service 2>/dev/null || true
	rm -f "${UNIT}" "${LIB}"
	systemctl daemon-reload
	echo "removed (captures kept in ${LOG_DIR}/)"
	;;
report)
	if ! compgen -G "${LOG_DIR}/*.txt" >/dev/null; then
		echo "no captures yet in ${LOG_DIR}/"
		exit 0
	fi
	files=("${LOG_DIR}"/*.txt)
	for (( i = ${#files[@]} - 1; i >= 0; i-- )); do   # newest first
		echo "=== ${files[i]}"
		cat "${files[i]}"
		echo
	done
	;;
*)
	sed -n '4,11p' "$0" | sed 's/^# \{0,1\}//'
	exit 1
	;;
esac
