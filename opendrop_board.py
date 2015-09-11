import time
import logging

from open_drop import Proxy
from serial import Serial

from serial_device import get_serial_ports


INPUT = 0
OUTPUT = 1
LOW = 0
HIGH = 1


class OpenDropBoard(object):
    '''
    This is a dummy class defining the API required to work with the OpenDrop
    plugin.
    '''
    def __init__(self):
        self.serial_device = None

        # the following must be defined as properties or members
        # todo: query these from the device
        self.serial_number = 0
        self.max_waveform_voltage = 200.0
        self.min_waveform_frequency = 0
        self.max_waveform_frequency = 10e3

    @property
    def port(self):
        try:
            return self.serial_device.port
        except:
            return None
    
    @property
    def baud_rate(self):
        try:
            return self.serial_device.baudrate
        except:
            return None
    
    @baud_rate.setter
    def baud_rate(self, value):
        # todo
        pass
    
    def connect(self, serial_port=None, baud_rate=115200):
        if serial_port is None or serial_port == 'None':
            # check if we're reconnecting (i.e., already have a port number)
            serial_port = self.port

            # if not, try connecting to the first available port
            if serial_port is None:
                serial_port = [port for port in get_serial_ports()][0]
        
        # if there's no port to try, return
        if serial_port is None:
            return

        if not (self.port == serial_port and self.baud_rate == baud_rate):
            self.serial_device = Serial(serial_port, baudrate=baud_rate)
            self.proxy = Proxy(self.serial_device)
            
            # wait for board to initialize
            time.sleep(2)
    
            # initialize the digital pins 2-18 as an outputs
            for pin in range(2, 19):
                self.proxy.pin_mode(pin, OUTPUT)
            self.clear_all_channels()

    def disconnect(self):
        try:
            self.serial_device.close()
        except:
            pass

    def connected(self):
        if self.serial_device and self.serial_device.isOpen():
            return True
        else:
            return False

    def flash_firmware(self, hardware_version):
        pass

    # these are currently mutators (but could be converted to properties)
    def set_state_of_all_channels(self, state_array):
        self.clear_all_channels()
        for channel, state in enumerate(state_array):
            if state:
                logging.info('[OpenDropBoard] set channel %d to %s' %
                              (channel, ['HIGH', 'LOW'][state < 1]))
                self.set_channel_state(channel, state)

    def set_waveform_voltage(self, voltage):
        pass
    
    def set_waveform_frequency(self, frequency):
        pass

    # these are currently accessors (but could be converted to properties)
    def number_of_channels(self):
        # todo: query this from the device
        return 68
    
    def name(self):
        # todo: query this from the device
        return "OpenDrop"
    
    def host_software_version(self):
        # todo: auto-generate based on git version 
        return "0.0.0"
    
    def software_version(self):
        # todo: query this from the device
        return "0.0.0"

    def hardware_version(self):
        # todo: query this from the device
        return "0.0.0"

    # these methods implement OpenDrop functionality through the base_node_rpc
    # API.
    # todo: move implementation to firmware 
    def set_gate(self, i, state):
        logging.debug('[OpenDropBoard] set G%d %s'
                      % (i, ['HIGH', 'LOW'][state < 1]))
        self.proxy.digital_write(2 + i, state)

    def set_source(self, i, state):
        logging.debug('[OpenDropBoard] set S%d %s'
                      % (i, ['HIGH', 'LOW'][state < 1]))
        self.proxy.digital_write(10 + i, state)    

    def clear_all_channels(self):
        # set all gate pins high
        for i in range(0, 9):
            self.set_gate(i, HIGH)

        # set all source pins low
        for i in range(1, 9):
            self.set_source(i, LOW)

    def set_channel_state(self, channel, state):
        if channel < 2:
            self.set_gate(0, state)
            self.set_source(2 * channel + 1, int(not bool(state)))
        elif channel < 4:
            self.set_gate(0, state)
            self.set_source(2 * channel + 2, int(not bool(state)))
        else:
            self.set_gate((channel - 4) / 8 + 1, state)
            self.set_source((channel - 4) % 8 + 1, int(not bool(state)))
