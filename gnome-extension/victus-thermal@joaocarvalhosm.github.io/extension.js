// SPDX-License-Identifier: GPL-3.0-or-later
//
// Victus Thermal GNOME extension:
//   - thermalIndicator.js: top-bar indicator for victus-thermald (fans,
//     temperatures, power mode);
//   - keyboardLight.js: Quick Settings toggle that turns the keyboard light
//     off after inactivity (only shown when the light is controllable);
//   - turbo.js: the OMEN key toggles turbo (performance + fans at max).

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {KeyboardLightController, KeyboardLightIndicator} from './keyboardLight.js';
import {VictusIndicator} from './thermalIndicator.js';
import {TurboKey} from './turbo.js';

export default class VictusThermalExtension extends Extension {
    enable() {
        this._indicator = new VictusIndicator(this);
        Main.panel.addToStatusArea(this.uuid, this._indicator);

        this._settings = this.getSettings();
        this._turbo = new TurboKey(this._settings, on => {
            if (on)
                this._indicator?.spinFan();
            else
                this._indicator?.stopFan();
        });
        const controller = new KeyboardLightController(this._settings);
        this._kbdController = controller;
        controller.start().then(available => {
            // disable() may have run while UPower was answering
            if (!available || this._kbdController !== controller)
                return;
            this._kbdIndicator = new KeyboardLightIndicator(this._settings);
            Main.panel.statusArea.quickSettings.addExternalIndicator(this._kbdIndicator);
        });
    }

    disable() {
        this._turbo?.destroy();
        this._turbo = null;
        this._kbdIndicator?.destroy();
        this._kbdIndicator = null;
        this._kbdController?.destroy();
        this._kbdController = null;
        this._settings = null;
        this._indicator?.destroy();
        this._indicator = null;
    }
}
