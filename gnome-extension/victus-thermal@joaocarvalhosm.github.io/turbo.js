// SPDX-License-Identifier: GPL-3.0-or-later
//
// Turbo key: the OMEN key (KEY_PROG2, "Launch2" in GNOME) toggles
// `victusctl turbo`: performance profile + fans at max, or back to
// balanced + automatic fans. The shortcut is the `turbo-shortcut` setting.
//
// Feedback: a pill at the top centre of the screen, where GNOME shows its
// notification banners, and `onToggled(on)` for the panel indicator (which
// spins its fan). Both run SETTLE_MS after the switch, once GNOME has added
// or removed its own performance icon in the top bar.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

Gio._promisify(Gio.Subprocess.prototype, 'communicate_utf8_async');

const KEY = 'turbo-shortcut';
const SETTLE_MS = 300;

const TOAST_GAP_PX = 12;      // below the top bar
const TOAST_SLIDE_PX = 16;
const TOAST_IN_MS = 350;
const TOAST_HOLD_MS = 2500;
const TOAST_OUT_MS = 300;

/** Short message at the top centre of the primary monitor. */
class Toast {
    constructor() {
        this._actor = null;
        this._timer = 0;
    }

    show(text, iconName, styleClass = '') {
        if (!this._actor) {
            this._icon = new St.Icon({style_class: 'victus-toast-icon'});
            this._label = new St.Label({y_align: Clutter.ActorAlign.CENTER});
            this._actor = new St.BoxLayout({opacity: 0, reactive: false});
            this._actor.add_child(this._icon);
            this._actor.add_child(this._label);
            Main.layoutManager.uiGroup.add_child(this._actor);
            this._actor.translation_y = -TOAST_SLIDE_PX;
        }
        const actor = this._actor;
        this._icon.icon_name = iconName;
        this._label.text = text;
        actor.style_class = `victus-toast ${styleClass}`;
        Main.layoutManager.uiGroup.set_child_above_sibling(actor, null);

        const monitor = Main.layoutManager.primaryMonitor;
        const [, width] = actor.get_preferred_width(-1);
        actor.set_position(
            Math.round(monitor.x + (monitor.width - width) / 2),
            monitor.y + Main.panel.height + TOAST_GAP_PX);

        actor.remove_all_transitions();
        actor.ease({
            opacity: 255,
            translation_y: 0,
            duration: TOAST_IN_MS,
            mode: Clutter.AnimationMode.EASE_OUT_CUBIC,
        });

        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = GLib.timeout_add(GLib.PRIORITY_DEFAULT, TOAST_HOLD_MS, () => {
            this._timer = 0;
            actor.ease({
                opacity: 0,
                translation_y: -TOAST_SLIDE_PX / 2,
                duration: TOAST_OUT_MS,
                mode: Clutter.AnimationMode.EASE_IN_CUBIC,
                onStopped: finished => {
                    if (finished)
                        this._removeActor();
                },
            });
            return GLib.SOURCE_REMOVE;
        });
    }

    _removeActor() {
        this._actor?.destroy();
        this._actor = null;
    }

    destroy() {
        if (this._timer)
            GLib.source_remove(this._timer);
        this._timer = 0;
        this._removeActor();
    }
}

export class TurboKey {
    constructor(settings, onToggled = null) {
        this._onToggled = onToggled;
        this._toast = new Toast();
        this._settleId = 0;
        this._cancellable = new Gio.Cancellable();
        this._busy = false;
        Main.wm.addKeybinding(KEY, settings, Meta.KeyBindingFlags.IGNORE_AUTOREPEAT,
            Shell.ActionMode.NORMAL | Shell.ActionMode.OVERVIEW | Shell.ActionMode.POPUP,
            () => this._toggle());
    }

    async _toggle() {
        if (this._busy)   // one switch at a time
            return;
        this._busy = true;
        try {
            const proc = Gio.Subprocess.new(['victusctl', 'turbo', 'toggle'],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE);
            const [stdout, stderr] = await proc.communicate_utf8_async(null, this._cancellable);
            // victusctl can finish just before destroy() cancels the wait
            if (this._cancellable.is_cancelled())
                return;
            if (!proc.get_successful())
                throw new Error(stderr?.trim() || 'victusctl turbo falhou');
            const on = stdout.includes('turbo: on');
            this._settleId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, SETTLE_MS, () => {
                this._settleId = 0;
                this._show(on);
                return GLib.SOURCE_REMOVE;
            });
        } catch (e) {
            if (!e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                Main.notifyError('Victus Thermal', e.message);
        } finally {
            this._busy = false;
        }
    }

    _show(on) {
        this._toast.show(on ? 'Turbo ligado' : 'Turbo desligado',
            on ? 'power-profile-performance-symbolic' : 'power-profile-balanced-symbolic',
            on ? 'victus-turbo-on' : '');
        this._onToggled?.(on);
    }

    destroy() {
        if (this._settleId)
            GLib.source_remove(this._settleId);
        this._settleId = 0;
        this._cancellable.cancel();
        this._toast.destroy();
        Main.wm.removeKeybinding(KEY);
    }
}
