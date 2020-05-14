# Copyright (c) 2010-2013, Regents of the University of California. 
# All rights reserved. 
#  
# Released under the BSD 3-Clause license as published at the link below.
# https://openwsn.atlassian.net/wiki/display/OW/License

import Queue
import json
import logging
import os
import socket
import sys
import threading
import time

import paho.mqtt.client as mqtt
import serial
from enum import IntEnum
from pydispatch import dispatcher

from openvisualizer.motehandler.moteprobe import openhdlc
from openvisualizer.motehandler.moteprobe.serialtester import SerialTester
from openvisualizer.utils import format_string_buf, format_crash_message

if os.name == 'nt':  # Windows
    import _winreg as winreg  # pylint: disable=import-error
elif os.name == 'posix':  # Linux
    import glob  # pylint: disable=import-error
    import platform  # pylint: disable=import-error

log = logging.getLogger('MoteProbe')
log.setLevel(logging.ERROR)
log.addHandler(logging.NullHandler())

# ============================ defines =========================================

BAUDRATE_IOTLAB = 500000

# ============================ functions =======================================

def find_serial_ports(baudrate, is_iot_motes=False, port_mask=None):
    """
    Returns the serial ports of the motes connected to the computer.

    :returns: A list of tuples (name,baudrate) where:
        - name is a strings representing a serial port, e.g. 'COM1'
        - baudrate is an int representing the baurate, e.g. 115200
    """
    serial_ports = []

    if port_mask is None:
        if os.name == 'nt':
            path = 'HARDWARE\\DEVICEMAP\\SERIALCOMM'
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
                for i in range(winreg.QueryInfoKey(key)[1]):
                    try:
                        val = winreg.EnumValue(key, i)
                    except:
                        pass
                    else:
                        serial_ports.append(str(val[1]))
            except Exception:
                pass

        elif os.name == 'posix':
            if platform.system() == 'Darwin':
                port_mask = ['/dev/tty.usbserial-*']
            else:
                port_mask = ['/dev/ttyUSB*']
            for mask in port_mask:
                serial_ports += [(s) for s in glob.glob(mask)]

    else:
        for mask in port_mask:
            serial_ports += [(s) for s in glob.glob(mask)]


    mote_ports = []

    if is_iot_motes:
        # this is IoTMotes, use the ports directly
        mote_ports = [(port, BAUDRATE_IOTLAB) for port in serial_ports]
    else:
        # Find all OpenWSN motes that answer to the TRIGGER_SERIALECHO commands
        for port in serial_ports:
            try:
                probe = MoteProbe(mqtt_broker_address=None, serial_port=(port, 115200))
                while not hasattr(probe, 'serial'):
                    pass
                for baud in baudrate:
                    log.debug("Probing port {} at baudrate {}".format(port, baud))
                    probe.serial.baudrate = baud
                    tester = SerialTester(probe)
                    tester.set_num_test_pkt(1)
                    tester.set_timeout(2)
                    tester.test(blocking=True)
                    if tester.get_stats()['numOk'] >= 1:
                        mote_ports.append((port, baud))
                        break
            except serial.SerialException as e:
                log.warning('{} {}'.format(e, port))
            except Exception as e:
                log.error(e)
            finally:
                probe.close()
                probe.join()

    # log
    log.success("discovered following serial-port(s): {0}".format(['{0}@{1}'.format(s[0], s[1]) for s in mote_ports]))

    return mote_ports


# ============================ class ===========================================

class OpentestbedMoteFinder(object):
    OPENTESTBED_RESP_STATUS_TIMEOUT = 10

    def __init__(self, mqtt_broker_address):
        self.opentestbed_motelist = set()
        self.mqtt_broker_address = mqtt_broker_address

    def get_opentestbed_motelist(self):

        # create mqtt client
        mqtt_client = mqtt.Client('FindMotes')
        mqtt_client.on_connect = self._on_mqtt_connect
        mqtt_client.on_message = self._on_mqtt_message
        mqtt_client.connect(self.mqtt_broker_address)
        mqtt_client.loop_start()

        # wait for a while to gather the response from otboxes
        log.info("discovering motes in testbed... (waiting for {}s)".format(self.OPENTESTBED_RESP_STATUS_TIMEOUT))
        time.sleep(self.OPENTESTBED_RESP_STATUS_TIMEOUT)

        # close the client and return the motes list
        mqtt_client.loop_stop()

        log.info("discovered {0} motes".format(len(self.opentestbed_motelist)))

        return self.opentestbed_motelist

    def _on_mqtt_connect(self, client, userdata, flags, rc):

        log.success("succesfully connected to: {0}".format(self.mqtt_broker_address))

        client.subscribe('opentestbed/deviceType/box/deviceId/+/resp/status')

        payload_status = {'token': 123}
        # publish the cmd message
        client.publish(
            topic='opentestbed/deviceType/box/deviceId/all/cmd/status',
            payload=json.dumps(payload_status),
        )

    def _on_mqtt_message(self, client, userdata, message):

        # get the motes list from payload
        payload_status = json.loads(message.payload)

        for mote in payload_status['returnVal']['motes']:
            if 'EUI64' in mote:
                self.opentestbed_motelist.add(mote['EUI64'])


class MoteProbe(threading.Thread):
    class MoteModes(IntEnum):
        MODE_SERIAL = 0
        MODE_EMULATED = 1
        MODE_IOTLAB = 2
        MODE_TESTBED = 3

    XOFF = 0x13
    XON = 0x11
    XONXOFF_ESCAPE = 0x12
    XONXOFF_MASK = 0x10

    # XOFF            is transmitted as [XONXOFF_ESCAPE,           XOFF^XONXOFF_MASK]==[0x12,0x13^0x10]==[0x12,0x03]
    # XON             is transmitted as [XONXOFF_ESCAPE,            XON^XONXOFF_MASK]==[0x12,0x11^0x10]==[0x12,0x01]
    # XONXOFF_ESCAPE  is transmitted as [XONXOFF_ESCAPE, XONXOFF_ESCAPE^XONXOFF_MASK]==[0x12,0x12^0x10]==[0x12,0x02]

    def __init__(self, mqtt_broker_address, serial_port=None, emulated_mote=None, iotlab_mote=None,
                 testbedmote_eui64=None):

        # initialize the parent class
        super(MoteProbe, self).__init__()

        log.debug("create instance")

        # verify params
        if serial_port:
            assert not emulated_mote
            assert not iotlab_mote
            assert not testbedmote_eui64
            self.mode = self.MoteModes.MODE_SERIAL
        elif emulated_mote:
            assert not serial_port
            assert not iotlab_mote
            assert not testbedmote_eui64
            self.mode = self.MoteModes.MODE_EMULATED
        elif iotlab_mote:
            assert not serial_port
            assert not emulated_mote
            assert not testbedmote_eui64
            self.mode = self.MoteModes.MODE_IOTLAB
        elif testbedmote_eui64:
            assert not serial_port
            assert not emulated_mote
            assert not iotlab_mote
            self.mode = self.MoteModes.MODE_TESTBED
        else:
            raise SystemError()

        # store params
        if self.mode == self.MoteModes.MODE_SERIAL:
            self.serialport = serial_port[0]
            self._baudrate = serial_port[1]
            self._portname = self.serialport
        elif self.mode == self.MoteModes.MODE_EMULATED:
            self.emulatedMote = emulated_mote
            self._portname = 'emulated{0}'.format(self.emulatedMote.get_id())
        elif self.mode == self.MoteModes.MODE_IOTLAB:
            self.iotlabmote = iotlab_mote
            self._portname = 'IoT-LAB{0}'.format(iotlab_mote)
        elif self.mode == self.MoteModes.MODE_TESTBED:
            self.testbedmote_eui64 = testbedmote_eui64
            self._portname = 'opentestbed_{0}'.format(testbedmote_eui64)
        else:
            raise SystemError()

        # at this moment, MQTT broker is used even if the mode is not
        # MODE_TESTBED; see moteconnector, OpenParser and ParserData.
        self.mqtt_broker_address = mqtt_broker_address

        # log
        log.debug("creating MoteProbe attaching to {0}".format(self._portname))

        # local variables
        self.hdlc = openhdlc.OpenHdlc()
        self.last_rx_byte = self.hdlc.HDLC_FLAG
        self.is_receiving = False
        self.input_buf = ''
        self.output_buf = []
        self.output_buf_lock = threading.RLock()
        self.data_lock = threading.Lock()
        # flag to permit exit from read loop
        self.quit = False

        self.send_to_parser = None  # to be assigned

        if self.mode == self.MoteModes.MODE_TESTBED:
            # initialize variable for testbedmote
            self.serialbytes_queue = Queue.Queue(maxsize=10)  # create queue for receiving serialbytes messages

            # mqtt client
            self.mqtt_client = mqtt.Client()
            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_message = self._on_mqtt_message
            self.mqtt_client.connect(self.mqtt_broker_address)
            self.mqtt_client.loop_start()

        # give this thread a name
        self.name = 'MoteProbe@' + self._portname

        if self.mode in [self.MoteModes.MODE_EMULATED, self.MoteModes.MODE_IOTLAB]:
            # Non-daemonized MoteProbe does not consistently die on close(),
            # so ensure MoteProbe does not persist.
            self.daemon = True

        # connect to dispatcher
        dispatcher.connect(self._send_data, signal='fromMoteConnector@' + self._portname)

        # start myself
        self.start()

    # ======================== thread ==========================================

    def run(self):
        try:
            # log
            log.debug("start running")
            log.debug("open port {0}".format(self._portname))

            if self.mode == self.MoteModes.MODE_SERIAL:
                self.serial = serial.Serial(self.serialport, self._baudrate, timeout=1, xonxoff=True, rtscts=False,
                                            dsrdtr=False)
            elif self.mode == self.MoteModes.MODE_EMULATED:
                self.serial = self.emulatedMote.bsp_uart
            elif self.mode == self.MoteModes.MODE_IOTLAB:
                self.serial = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.serial.connect((self.iotlabmote, 20000))
            elif self.mode == self.MoteModes.MODE_TESTBED:
                # subscribe to topic: opentestbed/deviceType/mote/deviceId/00-12-4b-00-14-b5-b6-49/notif/frommoteserialbytes
                self.mqtt_serial_queue = self.serialbytes_queue
            else:
                raise SystemError()

            while not self.quit:  # read bytes from serial port
                try:
                    if self.mode == self.MoteModes.MODE_SERIAL:
                        rx_bytes = self.serial.read(1)
                        if rx_bytes == 0:  # timeout
                            continue
                    elif self.mode == self.MoteModes.MODE_EMULATED:
                        rx_bytes = self.serial.read()
                    elif self.mode == self.MoteModes.MODE_IOTLAB:
                        rx_bytes = self.serial.recv(1024)
                    elif self.mode == self.MoteModes.MODE_TESTBED:
                        rx_bytes = self.mqtt_serial_queue.get()
                        rx_bytes = [chr(i) for i in rx_bytes]
                    else:
                        raise SystemError()
                except Exception as err:
                    log.warning(err)
                    time.sleep(1)
                    break
                else:
                    for rx_byte in rx_bytes:
                        if not self.is_receiving and self.last_rx_byte == self.hdlc.HDLC_FLAG and rx_byte != self.hdlc.HDLC_FLAG:
                            # start of frame
                            if log.isEnabledFor(logging.DEBUG):
                                log.debug("{0}: start of hdlc frame {1} {2}".format(
                                    self.name,
                                    format_string_buf(self.hdlc.HDLC_FLAG),
                                    format_string_buf(rx_byte))
                                )

                            self.is_receiving = True
                            self.xonxoff_escaping = False
                            self.input_buf = self.hdlc.HDLC_FLAG
                            self._add_to_input_buf(rx_byte)
                        elif self.is_receiving and rx_byte != self.hdlc.HDLC_FLAG:
                            # middle of frame
                            self._add_to_input_buf(rx_byte)
                        elif self.is_receiving and rx_byte == self.hdlc.HDLC_FLAG:
                            # end of frame
                            if log.isEnabledFor(logging.DEBUG):
                                log.debug("{0}: end of hdlc frame {1} ".format(self.name, format_string_buf(rx_byte)))

                            self.is_receiving = False
                            self._add_to_input_buf(rx_byte)
                            temp_buf = self.input_buf
                            try:
                                self.input_buf = self.hdlc.dehdlcify(self.input_buf)

                                if log.isEnabledFor(logging.DEBUG):
                                    log.debug("{0}: {2} dehdlcized input: {1}".format(
                                        self.name, format_string_buf(self.input_buf), format_string_buf(temp_buf)))

                            except openhdlc.HdlcException as err:
                                log.warning('{0}: invalid serial frame: {2} {1}'.format(
                                    self.name,
                                    err,
                                    format_string_buf(temp_buf))
                                )
                            else:
                                if self.send_to_parser:
                                    self.send_to_parser([ord(c) for c in self.input_buf])
                        self.last_rx_byte = rx_byte

                if self.mode == self.MoteModes.MODE_EMULATED:
                    self.serial.done_reading()
        except Exception as err:
            err_msg = format_crash_message(self.name, err)
            log.critical(err_msg)
            sys.exit(-1)
        finally:
            if self.mode == self.MoteModes.MODE_SERIAL and self.serial is not None:
                self.serial.close()

    # ======================== public ==========================================

    @property
    def portname(self):
        with self.data_lock:
            return self._portname

    @property
    def baudrate(self):
        with self.data_lock:
            return self._baudrate

    def close(self):
        self.quit = True

    # ======================== private =========================================

    def _add_to_input_buf(self, byte):
        if byte == chr(self.XONXOFF_ESCAPE):
            self.xonxoff_escaping = True
        else:
            if self.xonxoff_escaping == True:
                self.input_buf += chr(ord(byte) ^ self.XONXOFF_MASK)
                self.xonxoff_escaping = False
            elif byte != chr(self.XON) and byte != chr(self.XOFF):
                self.input_buf += byte

    def _send_data(self, data):

        # abort for IoT-LAB
        if self.mode == self.MoteModes.MODE_IOTLAB:
            return

        # frame with HDLC
        hdlc_data = self.hdlc.hdlcify(data)

        if self.mode == self.MoteModes.MODE_TESTBED:
            payload_buffer = {'token': 123, 'serialbytes': [ord(i) for i in hdlc_data]}

            # publish the cmd message
            self.mqtt_client.publish(
                topic='opentestbed/deviceType/mote/deviceId/{0}/cmd/tomoteserialbytes'.format(self.testbedmote_eui64),
                payload=json.dumps(payload_buffer)
            )
        else:
            # write to serial
            bytes_written = 0

            if self.mode == self.MoteModes.MODE_SERIAL:
                self.serial.flush()

            while bytes_written != len(bytearray(hdlc_data)):
                bytes_written += self.serial.write(hdlc_data)

    # ==== mqtt callback functions
    def _on_mqtt_connect(self, client, userdata, flags, rc):

        client.subscribe(
            'opentestbed/deviceType/mote/deviceId/{0}/notif/frommoteserialbytes'.format(self.testbedmote_eui64))

    def _on_mqtt_message(self, client, userdata, message):

        try:
            serial_bytes = json.loads(message.payload)['serialbytes']
        except:
            log.error("failed to parse message payload {0}".format(message.payload))
        else:
            try:
                self.serialbytes_queue.put(serial_bytes, block=False)
            except:
                log.warning("queue overflow")
