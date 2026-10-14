// SPDX-License-Identifier: GPL-3.0-or-later
//
// Keyboard light: turn it off after a period without input, with a Quick
// Settings toggle to enable/disable that and pick the delay.
//
// The light is reached through UPower's KbdBacklight interface, which appears
// once the patched hp-wmi exposes hp::kbd_backlight. GNOME itself already
// turns the light off when the screen blanks, the lid closes or the system
// suspends; this only adds the inactivity timeout.

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';

import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';

const UPOWER_BUS_NAME = 'org.freedesktop.UPower';
const KBD_OBJECT_PATH = '/org/freedesktop/UPower/KbdBacklight';
const KbdBacklightProxy = Gio.DBusProxy.makeProxyWrapper(`
<node>
  <interface name="org.freedesktop.UPower.KbdBacklight">
    <method name="GetBrightness"><arg type="i" direction="out"/></method>
    <method name="GetMaxBrightness"><arg type="i" direction="out"/></method>
    <method name="SetBrightness"><arg type="i" direction="in"/></method>
    <signal name="BrightnessChangedWithSource">
      <arg type="i"/><arg type="s"/>
    </signal>
  </interface>
</node>`);

export const TIMEOUTS = [
    [30, '30 s'],
    [60, '1 min'],
    [120, '2 min'],
    [300, '5 min'],
];

// When input resumes after the screen blanked, GNOME restores the light
// itself to what it was when it blanked (off, if we had already turned it
// off). Restore a moment later so our state wins.
const RESTORE_DELAY_MS = 400;

function timeoutLabel(seconds) {
    return TIMEOUTS.find(([s]) => s === seconds)?.[1] ?? `${seconds} s`;
}

/**
 * Watches for inactivity and switches the keyboard light off and back on.
 * Only ever turns back on a light that it turned off itself.
 */
export class KeyboardLightController {
    constructor(settings) {
        this._settings = settings;
        this._cancellable = new Gio.Cancellable();
        this._monitor = global.backend.get_core_idle_monitor();
        this._idleWatch = 0;
        this._activeWatch = 0;
        this._restoreSource = 0;
        this._savedLevel = null;   // set while we hold the light off
        this._proxy = null;
    }

    /** Resolves to true when a controllable keyboard light exists. */
    async start() {
        try {
            this._proxy = await new Promise((resolve, reject) => {
                new KbdBacklightProxy(Gio.DBus.system, UPOWER_BUS_NAME, KBD_OBJECT_PATH,
                    (proxy, error) => (error ? reject(error) : resolve(proxy)),
                    this._cancellable);
            });
            const [max] = await this._proxy.GetMaxBrightnessAsync();
            if (max <= 0)
                return false;
        } catch {
            this._proxy = null;
            return false;
        }

        // A manual change (Fn key, GNOME) while we hold the light off means
        // the user decided: forget our saved level.
        this._proxy.connectSignal('BrightnessChangedWithSource', (_p, _s, [level]) => {
            if (this._savedLevel !== null && level > 0)
                this._savedLevel = null;
        });
        this._settingsIds = [
            this._settings.connect('changed::kbd-auto-off', () => this._rearm()),
            this._settings.connect('changed::kbd-auto-off-seconds', () => this._rearm()),
        ];
        this._rearm();
        return true;
    }

    _rearm() {
        if (this._idleWatch) {
            this._monitor.remove_watch(this._idleWatch);
            this._idleWatch = 0;
        }
        if (!this._settings.get_boolean('kbd-auto-off')) {
            this._restore();
            return;
        }
        const ms = this._settings.get_uint('kbd-auto-off-seconds') * 1000;
        this._idleWatch = this._monitor.add_idle_watch(ms, () => this._onIdle());
    }

    async _onIdle() {
        try {
            const [level] = await this._proxy.GetBrightnessAsync();
            if (level <= 0 || this._savedLevel !== null)
                return;   // already off (maybe by the user): nothing to do
            await this._proxy.SetBrightnessAsync(0);
            this._savedLevel = level;
            if (!this._activeWatch) {
                this._activeWatch = this._monitor.add_user_active_watch(() => {
                    this._activeWatch = 0;   // one-shot watch
                    this._scheduleRestore();
                });
            }
        } catch (e) {
            console.warn(`Victus Thermal: keyboard light: ${e.message}`);
        }
    }

    _scheduleRestore() {
        if (this._restoreSource)
            GLib.source_remove(this._restoreSource);
        this._restoreSource = GLib.timeout_add(GLib.PRIORITY_DEFAULT, RESTORE_DELAY_MS, () => {
            this._restoreSource = 0;
            this._restore();
            return GLib.SOURCE_REMOVE;
        });
    }

    _restore() {
        if (this._savedLevel === null || !this._proxy)
            return;
        const level = this._savedLevel;
        this._savedLevel = null;
        this._proxy.SetBrightnessAsync(level).catch(e =>
            console.warn(`Victus Thermal: keyboard light: ${e.message}`));
    }

    destroy() {
        for (const id of this._settingsIds ?? [])
            this._settings.disconnect(id);
        this._settingsIds = null;
        if (this._idleWatch)
            this._monitor.remove_watch(this._idleWatch);
        if (this._activeWatch)
            this._monitor.remove_watch(this._activeWatch);
        if (this._restoreSource)
            GLib.source_remove(this._restoreSource);
        this._idleWatch = this._activeWatch = this._restoreSource = 0;
        this._restore();   // never leave the light off behind us
        this._cancellable.cancel();
        this._proxy = null;
    }
}

const KeyboardLightToggle = GObject.registerClass(
class KeyboardLightToggle extends QuickMenuToggle {
    _init(settings) {
        super._init({
            title: 'Luz do teclado',
            iconName: 'keyboard-brightness-symbolic',
            toggleMode: true,
        });
        this._settings = settings;
        this._settings.bind('kbd-auto-off', this, 'checked', Gio.SettingsBindFlags.DEFAULT);

        this.menu.setHeader('keyboard-brightness-symbolic', 'Luz do teclado',
            'Apagar sozinha quando o teclado e o mouse não são usados');
        this._items = new Map();
        for (const [seconds, label] of TIMEOUTS) {
            const item = new PopupMenu.PopupMenuItem(`Apagar após ${label}`);
            item.connect('activate', () => {
                this._settings.set_uint('kbd-auto-off-seconds', seconds);
                this._settings.set_boolean('kbd-auto-off', true);
            });
            this.menu.addMenuItem(item);
            this._items.set(seconds, item);
        }

        this._changedIds = [
            this._settings.connect('changed::kbd-auto-off', () => this._sync()),
            this._settings.connect('changed::kbd-auto-off-seconds', () => this._sync()),
        ];
        this._sync();
    }

    _sync() {
        const enabled = this._settings.get_boolean('kbd-auto-off');
        const seconds = this._settings.get_uint('kbd-auto-off-seconds');
        this.subtitle = enabled ? `Apaga após ${timeoutLabel(seconds)}` : 'Sempre acesa';
        for (const [s, item] of this._items) {
            item.setOrnament(enabled && s === seconds
                ? PopupMenu.Ornament.CHECK : PopupMenu.Ornament.NONE);
        }
    }

    destroy() {
        for (const id of this._changedIds)
            this._settings.disconnect(id);
        super.destroy();
    }
});

export const KeyboardLightIndicator = GObject.registerClass(
class KeyboardLightIndicator extends SystemIndicator {
    _init(settings) {
        super._init();
        this.quickSettingsItems.push(new KeyboardLightToggle(settings));
    }

    destroy() {
        this.quickSettingsItems.forEach(item => item.destroy());
        super.destroy();
    }
});
