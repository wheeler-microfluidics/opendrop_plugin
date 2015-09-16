"""
Copyright 2015 Ryan Fobel

This file is part of opendrop_plugin.

opendrop_plugin is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

opendrop_plugin is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with opendrop_plugin.  If not, see <http://www.gnu.org/licenses/>.
"""
import os
import math
import re
from copy import deepcopy
import warnings

import tables
from datetime import datetime
from pygtkhelpers.ui.dialogs import info as info_dialog
import yaml
import gtk
import gobject
import numpy as np
from path_helpers import path
from flatland import Integer, Boolean, Float, Form, Enum, String
from flatland.validation import ValueAtLeast, ValueAtMost, Validator
import microdrop_utility as utility
from microdrop_utility.gui import yesno, FormViewDialog
from microdrop.logger import logger
from microdrop.gui.protocol_grid_controller import ProtocolGridController
from microdrop.plugin_helpers import (StepOptionsController, AppDataController,
                                      get_plugin_info)
from microdrop.plugin_manager import (IPlugin, IWaveformGenerator, Plugin,
                                      implements, PluginGlobals,
                                      ScheduleRequest, emit_signal,
                                      get_service_instance,
                                      get_service_instance_by_name)
from microdrop.app_context import get_app
from microdrop.dmf_device import DeviceScaleNotSet
from serial_device import SerialDevice, get_serial_ports

from opendrop_board import OpenDropBoard


# Ignore natural name warnings from PyTables [1].
#
# [1]: https://www.mail-archive.com/pytables-users@lists.sourceforge.net/msg01130.html
warnings.simplefilter('ignore', tables.NaturalNameWarning)

PluginGlobals.push_env('microdrop.managed')


def max_voltage(element, state):
    """Verify that the voltage is below a set maximum"""
    service = get_service_instance_by_name(
        get_plugin_info(path(__file__).parent).plugin_name)

    if service.control_board.connected() and \
        element.value > service.control_board.max_waveform_voltage:
        return element.errors.append('Voltage exceeds the maximum value '
                                     '(%d V).' %
                                     service.control_board.max_waveform_voltage)
    else:
        return True


def check_frequency(element, state):
    """Verify that the frequency is within the valid range"""
    service = get_service_instance_by_name(
        get_plugin_info(path(__file__).parent).plugin_name)

    if service.control_board.connected() and \
        (element.value < service.control_board.min_waveform_frequency or \
        element.value > service.control_board.max_waveform_frequency):
        return element.errors.append('Frequency is outside of the valid range '
            '(%.1f - %.1f Hz).' %
            (service.control_board.min_waveform_frequency,
             service.control_board.max_waveform_frequency)
        )
    else:
        return True


class OpenDropPlugin(Plugin, StepOptionsController, AppDataController):
    """
    This class is automatically registered with the PluginManager.
    """
    implements(IPlugin)
    implements(IWaveformGenerator)

    serial_ports_ = [port for port in get_serial_ports()]
    if len(serial_ports_):
        default_port_ = serial_ports_[0]
    else:
        default_port_ = None

    AppFields = Form.of(
        Enum.named('serial_port').using(default=default_port_,
                                        optional=True).valued(*serial_ports_),
        Integer.named('baud_rate')
        .using(default=115200, optional=True, validators=[ValueAtLeast(minimum=0),
                                                     ],),
    )

    StepFields = Form.of(
        Integer.named('duration').using(default=100, optional=True,
                                        validators=
                                        [ValueAtLeast(minimum=0), ]),
        Float.named('voltage').using(default=100, optional=True,
                                     validators=[ValueAtLeast(minimum=0),
                                                 max_voltage]),
        Float.named('frequency').using(default=10e3, optional=True,
                                       validators=[ValueAtLeast(minimum=0),
                                                   check_frequency]),
    )

    version = get_plugin_info(path(__file__).parent).version

    def __init__(self):
        self.control_board = OpenDropBoard()
        self.name = get_plugin_info(path(__file__).parent).plugin_name
        self.connection_status = "Not connected"
        self.current_frequency = None
        self.timeout_id = None

    def on_plugin_enable(self):
        super(OpenDropPlugin, self).on_plugin_enable()
        self.check_device_name_and_version()
        if get_app().protocol:
            self.on_step_run()
            self._update_protocol_grid()

    def on_plugin_disable(self):
        if get_app().protocol:
            self.on_step_run()
            self._update_protocol_grid()

    def on_protocol_swapped(self, old_protocol, protocol):
        self._update_protocol_grid()
        
    def _update_protocol_grid(self):
        app = get_app()
        app_values = self.get_app_values()
        pgc = get_service_instance(ProtocolGridController, env='microdrop')
        if pgc.enabled_fields:
            pgc.update_grid()

    def on_app_options_changed(self, plugin_name):
        app = get_app()
        if plugin_name == self.name:
            app_values = self.get_app_values()
            reconnect = False

            if self.control_board.connected():
                for k, v in app_values.items():
                    if k == 'baud_rate' and self.control_board.baud_rate != v:
                        self.control_board.baud_rate = v
                        reconnect = True
                    if k == 'serial_port' and self.control_board.port != v:
                        reconnect = True

            if reconnect:
                self.connect()
                
            self._update_protocol_grid()
        elif plugin_name == app.name:
            # Turn off all electrodes if we're not in realtime mode and not
            # running a protocol.
            if (self.control_board.connected() and not app.realtime_mode and
                not app.running):
                logger.info('Turning off all electrodes.')
                self.control_board.set_state_of_all_channels(
                    np.zeros(self.control_board.number_of_channels())
                )

    def connect(self):
        '''
        Try to connect to the control board at the default serial port selected
        in the Microdrop application options.

        If unsuccessful, try to connect to the control board on any available
        serial port, one-by-one.
        '''
        self.current_frequency = None
        if len(OpenDropPlugin.serial_ports_):
            app_values = self.get_app_values()
            # try to connect to the last successful port
            try:
                self.control_board.connect(str(app_values['serial_port']),
                    app_values['baud_rate'])
            except RuntimeError, why:
                logger.warning('Could not connect to control board on port %s.'
                               ' Checking other ports... [%s]' %
                               (app_values['serial_port'], why))
                
                self.control_board.connect(baud_rate=app_values['baud_rate'])
            app_values['serial_port'] = self.control_board.port
            self.set_app_values(app_values)
        else:
            raise Exception("No serial ports available.")

    def check_device_name_and_version(self):
        '''
        Check to see if:

         a) The connected device is a OpenDrop
         b) The device firmware matches the host driver API version

        In the case where the device firmware version does not match, display a
        dialog offering to flash the device with the firmware version that
        matches the host driver API version.
        '''
        try:
            self.connect()
            name = self.control_board.name()
            if name != "open_drop":
                raise Exception("Device is not an OpenDrop")

            host_software_version = self.control_board.host_software_version()
            remote_software_version = self.control_board.software_version()

            # Reflash the firmware if it is not the right version.
            if host_software_version != remote_software_version:
                response = yesno("The control board firmware version (%s) "
                                 "does not match the driver version (%s). "
                                 "Update firmware?" % (remote_software_version,
                                                       host_software_version))
                if response == gtk.RESPONSE_YES:
                    self.on_flash_firmware()
        except Exception, why:
            logger.warning("%s" % why)

        self.update_connection_status()

    def on_flash_firmware(self, widget=None, data=None):
        app = get_app()
        try:
            connected = self.control_board.connected()
            if not connected:
                self.connect()
            response = yesno("Save current control board configuration before "
                             "flashing?")
            if response == gtk.RESPONSE_YES:
                self.save_config()
            hardware_version = utility.Version.fromstring(
                self.control_board.hardware_version()
            )
            if not connected:
                self.control_board.disconnect()
            self.control_board.flash_firmware(hardware_version)
            app.main_window_controller.info("Firmware updated successfully.",
                                            "Firmware update")
        except Exception, why:
            logger.error("Problem flashing firmware. ""%s" % why)
        self.check_device_name_and_version()

    def update_connection_status(self):
        self.connection_status = "Not connected"
        app = get_app()
        connected = self.control_board.connected()
        if connected:
            name = self.control_board.name()
            version = self.control_board.hardware_version()
            firmware = self.control_board.software_version()
            n_channels = self.control_board.number_of_channels()
            serial_number = self.control_board.serial_number
            self.connection_status = ('%s v%s (Firmware: %s, S/N %03d)\n'
            '%d channels' % (name, version, firmware, serial_number,
                             n_channels))

        app.main_window_controller.label_control_board_status\
           .set_text(self.connection_status)

    def on_step_run(self):
        """
        Handler called whenever a step is executed.

        Plugins that handle this signal must emit the on_step_complete
        signal once they have completed the step. The protocol controller
        will wait until all plugins have completed the current step before
        proceeding.
        """
        logger.debug('[OpenDropPlugin] on_step_run()')
        self._kill_running_step()
        app = get_app()
        options = self.get_step_options()
        dmf_options = app.dmf_device_controller.get_step_options()
        logger.debug('[OpenDropPlugin] options=%s dmf_options=%s' %
                     (options, dmf_options))
        app_values = self.get_app_values()

        if (self.control_board.connected() and (app.realtime_mode or
                                                app.running)):

            state = dmf_options.state_of_channels
            max_channels = self.control_board.number_of_channels()
            if len(state) > max_channels:
                state = state[0:max_channels]
            elif len(state) < max_channels:
                state = np.concatenate([state, np.zeros(max_channels -
                                                        len(state), int)])
                assert(len(state) == max_channels)

            emit_signal("set_frequency",
                        options['frequency'],
                        interface=IWaveformGenerator)
            emit_signal("set_voltage", options['voltage'],
                        interface=IWaveformGenerator)

            self.control_board.set_state_of_all_channels(state)

        # if a protocol is running, wait for the specified minimum duration
        if app.running:
            logger.debug('[OpenDropPlugin] on_step_run: '
                         'timeout_add(%d, _callback_step_completed)' %
                         options['duration'])
            self.timeout_id = gobject.timeout_add(
                options['duration'], self._callback_step_completed)
            return
        else:
            self.step_complete()

    def step_complete(self, return_value=None):
        app = get_app()
        if app.running or app.realtime_mode:
            emit_signal('on_step_complete', [self.name, return_value])

    def on_step_complete(self, plugin_name, return_value=None):
        if plugin_name == self.name:
            self.timeout_id = None

    def _kill_running_step(self):
        if self.timeout_id:
            logger.debug('[OpenDropPlugin] _kill_running_step: removing'
                         'timeout_id=%d' % self.timeout_id)
            gobject.source_remove(self.timeout_id)

    def _callback_step_completed(self):
        logger.debug('[OpenDropPlugin] _callback_step_completed')
        self.step_complete()
        return False  # stop the timeout from refiring

    def on_protocol_run(self):
        """
        Handler called when a protocol starts running.
        """
        app = get_app()
        if not self.control_board.connected():
            logger.warning("Warning: no control board connected.")
        elif (self.control_board.number_of_channels() <=
              app.dmf_device.max_channel()):
            logger.warning("Warning: currently connected board does not have "
                           "enough channels for this protocol.")

    def on_protocol_pause(self):
        """
        Handler called when a protocol is paused.
        """
        app = get_app()
        self._kill_running_step()
        if self.control_board.connected() and not app.realtime_mode:
            # Turn off all electrodes
            logger.debug('Turning off all electrodes.')
            self.control_board.set_state_of_all_channels(
                np.zeros(self.control_board.number_of_channels()))

    def on_experiment_log_selection_changed(self, data):
        """
        Handler called whenever the experiment log selection changes.

        Parameters:
            data : dictionary of experiment log data for the selected steps
        """
        pass

    def set_voltage(self, voltage):
        """
        Set the waveform voltage.

        Parameters:
            voltage : RMS voltage
        """
        logger.info("[OpenDropPlugin].set_voltage(%.1f)" % voltage)
        self.control_board.set_waveform_voltage(voltage)

    def set_frequency(self, frequency):
        """
        Set the waveform frequency.

        Parameters:
            frequency : frequency in Hz
        """
        logger.info("[OpenDropPlugin].set_frequency(%.1f)" % frequency)
        self.control_board.set_waveform_frequency(frequency)
        self.current_frequency = frequency

    def on_step_options_changed(self, plugin, step_number):
        logger.debug('[OpenDropPlugin] on_step_options_changed(): %s '
                     'step #%d' % (plugin, step_number))
        app = get_app()
        app_values = self.get_app_values()
        options = self.get_step_options(step_number)
        if (app.protocol and not app.running and not app.realtime_mode and
            (plugin == 'microdrop.gui.dmf_device_controller' or plugin ==
             self.name) and app.protocol.current_step_number == step_number):
            self.on_step_run()

    def on_step_swapped(self, original_step_number, new_step_number):
        logger.debug('[OpenDropPlugin] on_step_swapped():'
                     'original_step_number=%d, new_step_number=%d' %
                     (original_step_number, new_step_number))
        self.on_step_options_changed(self.name,
                                     get_app().protocol.current_step_number)

    def on_experiment_log_changed(self, log):
        # Check if the experiment log already has control board meta data, and
        # if so, return.
        data = log.get("control board name")
        for val in data:
            if val:
                return

        # otherwise, add the name, hardware version, serial number,
        # and firmware version
        data = {}
        if self.control_board.connected():
            data["control board name"] = self.control_board.name()
            data["control board serial number"] = \
                self.control_board.serial_number
            data["control board hardware version"] = (self.control_board
                                                      .hardware_version())
            data["control board software version"] = (self.control_board
                                                      .software_version())
            # add info about the devices on the i2c bus
            try:
                data["i2c devices"] = (self.control_board._i2c_devices)
            except:
                pass
        log.add_data(data)

    def get_schedule_requests(self, function_name):
        """
        Returns a list of scheduling requests (i.e., ScheduleRequest
        instances) for the function specified by function_name.
        """
        if function_name in ['on_step_options_changed']:
            return [ScheduleRequest(self.name,
                                    'microdrop.gui.protocol_grid_controller'),
                    ScheduleRequest(self.name,
                                    'microdrop.gui.protocol_controller'),
                    ]
        elif function_name == 'on_app_options_changed':
            return [ScheduleRequest('microdrop.app', self.name)]
        elif function_name == 'on_protocol_swapped':
            return [ScheduleRequest('microdrop.gui.protocol_grid_controller',
                                    self.name)]
        return []

PluginGlobals.pop_env()
