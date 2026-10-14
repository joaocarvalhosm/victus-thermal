// SPDX-License-Identifier: GPL-3.0-or-later
//
// Top-bar indicator for victus-thermald: shows the hottest sensor, and lets
// the user pick the platform profile and fan mode. All state comes from
// /run/victus-thermal/status.json; all changes go through `victusctl`, so the
// extension never needs privileges of its own.

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
const HOT_C = 90;

const PROFILES = [
    ['low-power', 'Economia'],
    ['balanced', 'Equilibrado'],
    ['performance', 'Desempenho'],
];

const FAN_MODES = [
    ['curve', 'Automático (recomendado)'],
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
        return 'EMERGÊNCIA: ventoinhas no máximo até esfriar';
    if (d.kind === 'manual' && d.reason.includes('AC quiet'))
        return `Silencioso na tomada: ${Math.round(d.duty)}% (curva assume se esquentar)`;
    if (d.kind === 'manual' && d.hold_remaining) {
        return `Suavizando: ${Math.round(d.duty)}% por mais ` +
            `${Math.ceil(d.hold_remaining)} s antes de voltar ao firmware`;
    }
    if (d.kind === 'manual') {
        const text = `Curva: ${Math.round(d.duty)}% a ${Math.round(d.curve_temp)} °C`;
        return d.applied_pwm > d.pwm ? `${text} (mantendo a velocidade do firmware)` : text;
    }
    if (d.kind === 'max')
        return s.mode === 'max' ? 'Máximo (forçado)' : 'Curva: máximo';
    return s.mode === 'auto' ? 'Firmware HP no controle (forçado)' : 'Firmware HP no controle';
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

        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        this._icon = new St.Icon({
            gicon: Gio.icon_new_for_string(
                `${extension.path}/icons/victus-fan-symbolic.svg`),
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
        } catch (e) {
            // Service not installed yet: the fallback timer still works.
            this._monitor = null;
        }
    }

    _infoItem(styleClass = null) {
        const item = new PopupMenu.PopupMenuItem('', {reactive: false, can_focus: false});
        if (styleClass)
            item.label.add_style_class_name(styleClass);
        this.menu.addMenuItem(item);
        return item;
    }

    _choiceSection(title, choices, onActivate) {
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._infoItem('victus-section-title').label.text = title;
        const items = new Map();
        for (const [key, text] of choices) {
            const item = new PopupMenu.PopupMenuItem(text);
            item.connect('activate', () => onActivate(key));
            this.menu.addMenuItem(item);
            items.set(key, item);
        }
        return items;
    }

    _buildMenu() {
        this._tempsItem = this._infoItem();
        this._fansItem = this._infoItem();
        this._reasonItem = this._infoItem('victus-detail');

        // Same setting as GNOME's Power Mode menu (power-profiles-daemon); with
        // the patched hp-wmi it also switches HP's own thermal mode.
        this._profileItems = this._choiceSection('Modo de energia (o mesmo do GNOME)', PROFILES,
            key => this._choose(this._profileItems, key, ['victusctl', 'profile', key]));
        this._modeItems = this._choiceSection('Ventoinhas', FAN_MODES,
            key => this._choose(this._modeItems, key, ['victusctl', 'mode', key]));

        this._acQuietItem = new PopupMenu.PopupSwitchMenuItem(
            'Silencioso contínuo na tomada', false);
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
            if (name === level)
                this.add_style_class_name(name);
            else
                this.remove_style_class_name(name);
        }
    }

    _update(s) {
        const online = s !== null && Date.now() / 1000 - s.ts < STALE_SECONDS;
        if (!online) {
            this._label.text = '–';
            this._setLevel('victus-offline');
            this._tempsItem.label.text = 'Serviço victus-thermal parado';
            this._fansItem.label.text = 'sudo systemctl start victus-thermal';
            this._reasonItem.label.text = '';
            this._setChoices(this._profileItems, null, false);
            this._setChoices(this._modeItems, null, false);
            this._acQuietItem.setSensitive(false);
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

        const gpu = t.dgpu_state === 'active' ? formatTemp(t.dgpu) : 'dormindo';
        this._tempsItem.label.text =
            `CPU ${formatTemp(t.cpu)}  ·  GPU ${gpu}  ·  iGPU ${formatTemp(t.igpu)}`;
        this._fansItem.label.text =
            `Ventoinhas ${formatRpm(s.fans_rpm[0])} / ${formatRpm(s.fans_rpm[1])} RPM` +
            (s.on_ac ? '  ·  na tomada' : '  ·  na bateria');
        this._reasonItem.label.text = s.error ? `Erro: ${s.error}` : describeDecision(s);

        this._setChoices(this._profileItems, s.profile, true);
        this._setChoices(this._modeItems, s.mode, true);
        // Forced MAX is only allowed in some profiles (Desempenho by default);
        // the daemon refuses it elsewhere, so do not offer it.
        const maxItem = this._modeItems.get('max');
        const maxAllowed = s.max_allowed !== false;
        maxItem.setSensitive(maxAllowed);
        maxItem.label.text = maxAllowed ? 'Máximo' : 'Máximo (só no Desempenho)';
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
