#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Identify the driver behind "WARNING: kernel/workqueue.c:2351 __queue_work".
#
# That warning means a delayed work item was queued while already on a list
# (a driver bug), but the stack trace only shows the timer, not whose work it
# was. The workqueue_queue_work tracepoint fires just before the check and
# records the work function; traceoff_on_warning freezes the trace buffer at
# the moment of the WARN, so the last event on the warning CPU names it.
#
#   sudo tools/catch-workqueue-warning.sh start     arm (until reboot)
#   sudo tools/catch-workqueue-warning.sh status    armed / triggered?
#   sudo tools/catch-workqueue-warning.sh report    show the culprit
#   sudo tools/catch-workqueue-warning.sh stop      disarm
#
# Overhead is small (one ring-buffer write per queued work item) and nothing
# survives a reboot.

set -euo pipefail

readonly T=/sys/kernel/tracing
readonly EVENT="${T}/events/workqueue/workqueue_queue_work/enable"
readonly SINCE_FILE=/run/catch-workqueue-warning.since

[[ ${EUID} -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ -e ${EVENT} ]] || { echo "tracefs not available at ${T}" >&2; exit 1; }

case "${1:-}" in
start)
	echo 0 > "${T}/tracing_on"
	echo > "${T}/trace"
	echo 4096 > "${T}/buffer_size_kb"          # per CPU
	echo 1 > "${EVENT}"
	echo 1 > /proc/sys/kernel/traceoff_on_warning
	date '+%Y-%m-%d %H:%M:%S' > "${SINCE_FILE}"
	echo 1 > "${T}/tracing_on"
	echo "armed. Use the laptop normally; after the next freeze/warning run: $0 report"
	;;
status)
	if [[ $(cat "${EVENT}") == 1 && $(cat "${T}/tracing_on") == 1 ]]; then
		echo "armed, no warning yet"
	elif [[ $(cat "${EVENT}") == 1 ]]; then
		echo "TRIGGERED: run $0 report"
	else
		echo "not armed"
	fi
	;;
report)
	# The trace froze at the first warning after `start`, so use that one.
	since="$(cat "${SINCE_FILE}" 2>/dev/null || echo today)"
	warn="$(journalctl -k -b --since "${since}" --no-pager -o cat \
		| grep -m1 'WARNING: kernel/workqueue.c' || true)"
	if [[ -z ${warn} ]]; then
		echo "no workqueue warning since the tracer was armed (${since})"
		exit 0
	fi
	echo "warning: ${warn}"
	cpu="$(sed -n 's/.*CPU#\([0-9]*\).*/\1/p' <<<"${warn}")"
	echo "last queued work items on CPU ${cpu}:"
	grep -E "\[0*${cpu}\]" "${T}/trace" | grep workqueue_queue_work | tail -5
	echo
	echo "most frequent work functions in the buffer (all CPUs):"
	grep -o 'function=[^ ]*' "${T}/trace" | sort | uniq -c | sort -rn | head -8
	echo
	echo "the 'function=' of the final line is the work item that tripped the WARN;"
	echo "find its driver with: grep -w <function> /proc/kallsyms"
	;;
stop)
	echo 0 > "${EVENT}"
	echo 0 > /proc/sys/kernel/traceoff_on_warning
	rm -f "${SINCE_FILE}"
	echo 1 > "${T}/tracing_on"
	echo > "${T}/trace"
	echo "disarmed"
	;;
*)
	sed -n '4,17p' "$0" | sed 's/^# \{0,1\}//'
	exit 1
	;;
esac
