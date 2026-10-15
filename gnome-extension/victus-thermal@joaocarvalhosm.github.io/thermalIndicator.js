// SPDX-License-Identifier: GPL-3.0-or-later
//
// Top-bar indicator for victus-thermald: shows the hottest sensor, and lets
// the user pick the power mode and fan mode. All state comes from
// /run/victus-thermal/status.json; all changes go through `victusctl`, so the
// extension never needs privileges of its own (starting a stopped service
// goes through the standard GNOME password dialog, via pkexec).

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

Gio._promisify(Gio.File.prototype, 'load_contents_async');
Gio._promisify(Gio.Subprocess.prototype, 'communicate_utf8_async');

const STATUS_DIR = '/run/victus-thermal';
const STATUS_NAME = 'status.json';
// Updates arrive through a file monitor the moment the daemon writes; the
// timer only catches a daemon that stopped writing (stale status).
const FALLBACK_POLL_SECONDS = 5;
const STALE_SECONDS = 15;
const WARM_C = 80;
// Secondary text, like GNOME's own dimmed labels; works in light and dark.
const DIM_OPACITY = 166;
const HOT_C = 90;

// [key, label, icon] — the icons are the ones GNOME's own power menu uses.
const PROFILES = [
    ['low-power', 'Economia', 'power-profile-power-saver-symbolic'],
    ['balanced', 'Equilibrado', 'power-profile-balanced-symbolic'],
    ['performance', 'Desempenho', 'power-profile-performance-symbolic'],
];

const FAN_MODES = [
    ['curve', 'Automático'],
    ['auto', 'Somente firmware HP'],
    ['max', 'Máximo'],
];

function formatTemp(value) {
    return value === null || value === undefined ? '–' : `${Math.round(value)} °C`;
}

function formatRpm(value) {
    return value === null || value === undefined ? '–' : `${value}`;
}

function describeDecision(s) {
    const d = s.decision;
    if (s.emergency)
        return 'Emergência: ventoinhas no máximo até esfriar';
    if (d.kind === 'manual' && d.reason.includes('AC quiet'))
        return `Silencioso na tomada (${Math.round(d.duty)}%)`;
    if (d.kind === 'manual' && d.hold_remaining) {
        return `Suavizando: ${Math.round(d.duty)}% por mais ` +
            `${Math.ceil(d.hold_remaining)} s`;
    }
    if (d.kind === 'manual') {
        const text = `Curva: ${Math.round(d.duty)}% a ${Math.round(d.curve_temp)} °C`;
        return d.applied_pwm > d.pwm ? `${text}, no ritmo do firmware` : text;
    }
    if (d.kind === 'max')
        return s.mode === 'max' ? 'Máximo (forçado)' : 'Curva no máximo';
    return s.mode === 'auto' ? 'Somente firmware HP' : 'Firmware HP no controle';
}

/*
 * Information rows are plain boxes, not menu items: GNOME greys out menu
 * items that cannot be activated, which would make readings look disabled.
 * The theme's padding for menu items (9px 12px) is mirrored in the CSS.
 */

/** Dim label on the left, value on the right. */
const InfoRow = GObject.registerClass(
class InfoRow extends St.BoxLayout {
    _init(title) {
        super._init({style_class: 'victus-info-row', x_expand: true});
        this.add_child(new St.Label({
            text: title,
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'victus-info-title',
            opacity: DIM_OPACITY,
        }));
        this.value = new St.Label({
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'victus-info-value',
        });
        this.add_child(this.value);
    }
});

/** Fan icon, big temperature and a one-line summary. */
const Header = GObject.registerClass(
class Header extends St.BoxLayout {
    _init(gicon) {
        super._init({style_class: 'victus-header', x_expand: true});
        this.icon = new St.Icon({gicon, style_class: 'victus-header-icon'});
        this.add_child(this.icon);
        const box = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            y_align: Clutter.ActorAlign.CENTER,
            x_expand: true,
        });
        this.title = new St.Label({style_class: 'victus-header-title'});
        this.subtitle = new St.Label({
            style_class: 'victus-header-subtitle',
            opacity: DIM_OPACITY,
        });
        box.add_child(this.title);
        box.add_child(this.subtitle);
        this.add_child(box);
    }
});

/**
 * Collapsible submenu showing the current choice on the right, like the
 * entries of GNOME's own system menu.
 */
function choiceSubMenu(title, icon) {
    const item = new PopupMenu.PopupSubMenuMenuItem(title, true);
    item.icon.icon_name = icon;
    item.current = new St.Label({
        y_align: Clutter.ActorAlign.CENTER,
        style_class: 'victus-submenu-current',
        opacity: DIM_OPACITY,
    });
    // Before the expander arrow, if this GNOME version exposes it.
    if (item._triangleBin)
        item.insert_child_below(item.current, item._triangleBin);
    else
        item.add_child(item.current);
    return item;
}

export const VictusIndicator = GObject.registerClass(
class VictusIndicator extends PanelMenu.Button {
    _init(extension) {
        super._init(0.5, 'Victus Thermal');
        this._extension = extension;
        this._cancellable = new Gio.Cancellable();
        this._statusFile = Gio.File.new_for_path(`${STATUS_DIR}/${STATUS_NAME}`);
        this._busy = false;
        this._pending = false;
        this._fanIcon = Gio.icon_new_for_string(
            `${extension.path}/icons/victus-fan-symbolic.svg`);

        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        this._icon = new St.Icon({
            gicon: this._fanIcon,
            style_class: 'system-status-icon victus-panel-icon',
        });
        this._label = new St.Label({
            text: '–',
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'victus-panel-label',
        });
        box.add_child(this._icon);
        box.add_child(this._label);
        this.add_child(box);

        this._buildMenu();
        this._watchStatus();
        this._refresh();
        this._timer = GLib.timeout_add_seconds(
            GLib.PRIORITY_DEFAULT, FALLBACK_POLL_SECONDS, () => {
                this._refresh();
                return GLib.SOURCE_CONTINUE;
            });
    }

    _watchStatus() {
        // The daemon replaces status.json atomically (write + rename), so
        // watch the directory and react to the rename landing on our name.
        try {
            this._monitor = Gio.File.new_for_path(STATUS_DIR).monitor_directory(
                Gio.FileMonitorFlags.WATCH_MOVES, this._cancellable);
            this._monitor.connect('changed', (_m, file, other) => {
                if (file?.get_basename() === STATUS_NAME ||
                    other?.get_basename() === STATUS_NAME)
                    this._refresh();
            });
        } catch {
            // Service not installed yet: the fallback timer still works.
            this._monitor = null;
        }
    }

    _buildMenu() {
        this._headerSection = new PopupMenu.PopupMenuSection();
        this._header = new Header(this._fanIcon);
        this._headerSection.actor.add_child(this._header);
        this.menu.addMenuItem(this._headerSection);

        // Shown only while the service is not running.
        this._startItem = new PopupMenu.PopupImageMenuItem(
            'Iniciar serviço', 'media-playback-start-symbolic');
        this._startItem.connect('activate', () =>
            this._run(['pkexec', 'systemctl', 'start', 'victus-thermal.service']));
        this.menu.addMenuItem(this._startItem);

        this._detailsSection = new PopupMenu.PopupMenuSection();
        this._rows = {
            cpu: new InfoRow('CPU'),
            dgpu: new InfoRow('GPU NVIDIA'),
            igpu: new InfoRow('GPU integrada'),
            fans: new InfoRow('Ventoinhas'),
        };
        for (const row of Object.values(this._rows))
            this._detailsSection.actor.add_child(row);
        this._reasonLabel = new St.Label({style_class: 'victus-reason', opacity: DIM_OPACITY});
        this._detailsSection.actor.add_child(this._reasonLabel);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this.menu.addMenuItem(this._detailsSection);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // Same setting as GNOME's Power Mode menu (power-profiles-daemon); with
        // the patched hp-wmi it also switches HP's own thermal mode.
        this._profileMenu = choiceSubMenu('Modo de energia', 'power-profile-balanced-symbolic');
        this._profileItems = new Map();
        for (const [key, label, icon] of PROFILES) {
            const item = new PopupMenu.PopupImageMenuItem(label, icon);
            item.connect('activate', () =>
                this._choose(this._profileItems, key, ['victusctl', 'profile', key]));
            this._profileMenu.menu.addMenuItem(item);
            this._profileItems.set(key, item);
        }
        this.menu.addMenuItem(this._profileMenu);

        this._modeMenu = choiceSubMenu('Ventoinhas', 'emblem-system-symbolic');
        this._modeMenu.icon.gicon = this._fanIcon;
        this._modeItems = new Map();
        for (const [key, label] of FAN_MODES) {
            const item = new PopupMenu.PopupMenuItem(label);
            item.connect('activate', () =>
                this._choose(this._modeItems, key, ['victusctl', 'mode', key]));
            this._modeMenu.menu.addMenuItem(item);
            this._modeItems.set(key, item);
        }
        this.menu.addMenuItem(this._modeMenu);

        this._acQuietItem = new PopupMenu.PopupSwitchMenuItem('Silencioso na tomada', false);
        this._acQuietItem.connect('toggled', (_item, state) =>
            this._run(['victusctl', 'ac-quiet', state ? 'on' : 'off']));
        this.menu.addMenuItem(this._acQuietItem);
    }

    _choose(items, key, argv) {
        // Show the choice at once; the next status update confirms it (or
        // puts the old one back if the command failed).
        this._setChoices(items, key, true);
        this._run(argv);
    }

    async _run(argv) {
        try {
            const proc = Gio.Subprocess.new(argv,
                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE);
            const [, stderr] = await proc.communicate_utf8_async(null, this._cancellable);
            if (!proc.get_successful()) {
                // pkexec exit 126: the user dismissed the password dialog
                if (argv[0] === 'pkexec' && proc.get_exit_status() === 126)
                    return;
                if (stderr?.includes('permission denied')) {
                    throw new Error('Sem permissão para mudar as ventoinhas. ' +
                        'Rode "sudo ./install.sh daemon" de novo ou saia e entre na sessão.');
                }
                throw new Error(stderr?.trim() || `${argv.join(' ')} falhou`);
            }
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                return;
            Main.notifyError('Victus Thermal', e.message);
        }
        this._refresh();
    }

    async _readStatus() {
        try {
            const [contents] = await this._statusFile.load_contents_async(this._cancellable);
            return JSON.parse(new TextDecoder().decode(contents));
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                throw e;
            return null;
        }
    }

    async _refresh() {
        if (this._busy) {
            this._pending = true;   // coalesce bursts of file events
            return;
        }
        this._busy = true;
        try {
            do {
                this._pending = false;
                this._update(await this._readStatus());
            } while (this._pending);
        } catch {
            // cancelled: the indicator is being destroyed
        } finally {
            this._busy = false;
        }
    }

    _setChoices(items, active, sensitive) {
        for (const [key, item] of items) {
            item.setOrnament(key === active
                ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
            item.setSensitive(sensitive);
        }
    }

    _setLevel(level) {
        for (const name of ['victus-warm', 'victus-hot', 'victus-offline']) {
            if (name === level) {
                this.add_style_class_name(name);
                this._header.add_style_class_name(name);
            } else {
                this.remove_style_class_name(name);
                this._header.remove_style_class_name(name);
            }
        }
    }

    _update(s) {
        const online = s !== null && Date.now() / 1000 - s.ts < STALE_SECONDS;
        this._icon.opacity = online ? 255 : 128;
        this._startItem.visible = !online;
        this._detailsSection.actor.visible = online;
        this._profileMenu.visible = online;
        this._modeMenu.visible = online;
        this._acQuietItem.visible = online;
        if (!online) {
            this._label.text = '–';
            this._setLevel('victus-offline');
            this._header.title.text = 'Serviço parado';
            this._header.subtitle.text = 'As ventoinhas estão com o firmware HP';
            return;
        }

        const t = s.temps;
        const hottest = Math.max(...[t.cpu, t.igpu, t.dgpu].filter(v => typeof v === 'number'));
        const hasTemp = Number.isFinite(hottest);
        this._label.text = hasTemp ? `${Math.round(hottest)}°` : '–';
        let level = null;
        if (s.emergency || (hasTemp && hottest >= HOT_C))
            level = 'victus-hot';
        else if (hasTemp && hottest >= WARM_C)
            level = 'victus-warm';
        this._setLevel(level);

        const profile = PROFILES.find(([key]) => key === s.profile);
        this._header.title.text = hasTemp ? `${Math.round(hottest)} °C` : '–';
        this._header.subtitle.text =
            `${profile?.[1] ?? s.profile ?? '–'} · ${s.on_ac ? 'Na tomada' : 'Na bateria'}`;

        this._rows.cpu.value.text = formatTemp(t.cpu);
        this._rows.dgpu.value.text = t.dgpu_state === 'active' ? formatTemp(t.dgpu) : 'Desligada';
        this._rows.igpu.value.text = formatTemp(t.igpu);
        this._rows.fans.value.text =
            `${formatRpm(s.fans_rpm[0])} · ${formatRpm(s.fans_rpm[1])} RPM`;
        this._reasonLabel.text = s.error ? `Erro: ${s.error}` : describeDecision(s);

        this._setChoices(this._profileItems, s.profile, true);
        this._profileMenu.current.text = profile?.[1] ?? '';
        this._profileMenu.icon.icon_name = profile?.[2] ?? 'power-profile-balanced-symbolic';

        this._setChoices(this._modeItems, s.mode, true);
        // Forced MAX is only allowed in some profiles (Desempenho by default);
        // the daemon refuses it elsewhere, so do not offer it.
        const maxItem = this._modeItems.get('max');
        const maxAllowed = s.max_allowed !== false;
        maxItem.setSensitive(maxAllowed);
        maxItem.label.text = maxAllowed ? 'Máximo' : 'Máximo (só no Desempenho)';
        this._modeMenu.current.text =
            FAN_MODES.find(([key]) => key === s.mode)?.[1] ?? '';

        // setToggleState does not emit 'toggled', so this cannot loop back
        this._acQuietItem.setToggleState(Boolean(s.ac_quiet?.enabled));
        this._acQuietItem.setSensitive(s.ac_quiet !== undefined);
    }

    destroy() {
        if (this._timer) {
            GLib.source_remove(this._timer);
            this._timer = null;
        }
        this._monitor?.cancel();
        this._monitor = null;
        this._cancellable.cancel();
        super.destroy();
    }
});
