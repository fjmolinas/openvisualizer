# Copyright (c) 2010-2013, Regents of the University of California. 
# All rights reserved. 
#  
# Released under the BSD 3-Clause license as published at the link below.
# https://openwsn.atlassian.net/wiki/display/OW/License

"""
Contains application model for OpenVisualizer. Expects to be called by top-level UI module.  See main() for startup use.
"""

import logging.config
import platform
import shutil
import signal
import sys
import tempfile
import threading
import time
from ConfigParser import SafeConfigParser
from SimpleXMLRPCServer import SimpleXMLRPCServer
from argparse import ArgumentParser
from xmlrpclib import Fault

import bottle
import coloredlogs
import pkg_resources
import verboselogs

from openvisualizer import *
from openvisualizer.eventbus import eventbusmonitor
from openvisualizer.jrc import jrc
from openvisualizer.motehandler.moteconnector import moteconnector
from openvisualizer.motehandler.moteprobe import moteprobe
from openvisualizer.motehandler.motestate import motestate
from openvisualizer.motehandler.motestate.motestate import MoteState
from openvisualizer.openlbr import openlbr
from openvisualizer.opentun.opentun import OpenTun
from openvisualizer.rpl import topology, rpl
from openvisualizer.simengine import simengine, motehandler
from openvisualizer.utils import extract_component_codes, extract_log_descriptions, extract_6top_rcs, \
    extract_6top_states
from openvisualizer.webserver import WebServer

verboselogs.install()

log = logging.getLogger('OpenVisualizerServer')
coloredlogs.install(level='WARNING', logger=log, fmt='%(asctime)s [%(name)s:%(levelname)s] %(message)s',
                    datefmt='%H:%M:%S')


class ColoredFormatter(coloredlogs.ColoredFormatter):
    """ Class that matches coloredlogs.ColoredFormatter arguments with logging.Formatter """

    def __init__(self, fmt=None, datefmt=None):
        self.parser = SafeConfigParser()

        if sys.platform.startswith('win32'):
            log_colors_conf = pkg_resources.resource_filename(PACKAGE_NAME, WINDOWS_COLORS)
        else:
            log_colors_conf = pkg_resources.resource_filename(PACKAGE_NAME, UNIX_COLORS)

        self.parser.read(log_colors_conf)

        ls = self.parse_section('levels', 'keys')
        fs = self.parse_section('fields', 'keys')

        coloredlogs.ColoredFormatter.__init__(self, fmt=fmt, datefmt=datefmt, level_styles=ls, field_styles=fs)

    def parse_section(self, section, option):
        dictionary = {}

        if not self.parser.has_section(section) or not self.parser.has_option(section, option):
            log.warning('Unknown section {} or option {}'.format(section, option))
            return dictionary

        subsections = map(str.strip, self.parser.get(section, option).split(','))

        for subsection in subsections:
            if not self.parser.has_section(str(subsection)):
                log.warning('Unknown section name: {}'.format(subsection))
                continue

            dictionary[subsection] = {}
            options = self.parser.options(subsection)

            for opt in options:
                res = self.parse_options(subsection, opt.strip().lower())
                if res is not None:
                    dictionary[subsection][opt] = res

        return dictionary

    def parse_options(self, section, option):
        res = None
        if option == 'bold' or option == 'faint':
            try:
                return self.parser.getboolean(section, option)
            except ValueError:
                log.error('Illegal value: {} for option: {}'.format(self.parser.get(section, option), option))
        elif option == 'color':
            try:
                res = self.parser.getint(section, option)
            except ValueError:
                res = self.parser.get(section, option)
        else:
            log.warning('Unknown option name: {}'.format(option))

        return res


class OpenVisualizerServer(SimpleXMLRPCServer):
    """
    Class implements and RPC server that allows monitoring and (remote) management of a mesh network.
    """

    def __init__(self, host, port, webserver, simulator_mode, debug, vcdlog, use_page_zero, sim_topology, iotlab_motes,
                 testbed_motes, mqtt_broker, opentun, fw_path, auto_boot, root, port_mask):

        # store params
        self.host = host
        self.port = port
        self.webserver = webserver
        self.simulator_mode = simulator_mode
        self.debug = debug
        self.use_page_zero = use_page_zero
        self.iotlab_motes = iotlab_motes
        self.testbed_motes = testbed_motes
        self.vcdlog = vcdlog
        self.port_mask = port_mask
        self.baudrate = baudrate
        self.fw_path = fw_path
        self.dagroot = None
        self.auto_boot = auto_boot

        if self.fw_path is None:
            try:
                self.fw_path = os.environ['OPENWSN_FW_BASE']
            except KeyError:
                log.critical("Neither OPENWSN_FW_BASE or '--fw-path' was specified.")
                os.kill(os.getpid(), signal.SIGTERM)

        # local variables
        self.ebm = eventbusmonitor.EventBusMonitor()
        self.openlbr = openlbr.OpenLbr(use_page_zero)
        self.rpl = rpl.RPL()
        self.jrc = jrc.JRC()
        self.topology = topology.Topology()
        self.dagroot_list = []
        self.mote_probes = []

        # create opentun call last since indicates prefix
        self.opentun = OpenTun.create(opentun)

        if self.simulator_mode:
            self.simengine = simengine.SimEngine(sim_topology)
            self.simengine.start()

            # in "simulator" mode, motes are emulated
            self.temp_dir = self.copy_sim_fw()

            if self.temp_dir is None:
                log.critical("Failed to import simulation files! Exiting now!")
                os.kill(os.getpid(), signal.SIGTERM)

            sys.path.append(os.path.join(self.temp_dir))
            motehandler.read_notif_ids(os.path.join(self.temp_dir, 'openwsnmodule_obj.h'))

            import oos_openwsn  # pylint: disable=import-error

            self.mote_probes = []
            for _ in range(self.simulator_mode):
                mote_handler = motehandler.MoteHandler(oos_openwsn.OpenMote(), self.vcdlog)
                self.simengine.indicate_new_mote(mote_handler)
                self.mote_probes += [moteprobe.MoteProbe(mqtt_broker, emulated_mote=mote_handler)]
        elif self.iotlab_motes:
            # in "IoT-LAB" mode, motes are connected to TCP ports

            self.mote_probes = [moteprobe.MoteProbe(mqtt_broker, iotlab_mote=p) for p in self.iotlab_motes.split(',')]
        elif self.testbed_motes:
            motes_finder = moteprobe.OpentestbedMoteFinder(mqtt_broker)
            self.mote_probes = [
                moteprobe.MoteProbe(mqtt_broker, testbedmote_eui64=p) for p in motes_finder.get_opentestbed_motelist()
            ]

        else:
            # in "hardware" mode, motes are connected to the serial port

            self.mote_probes = [
                moteprobe.MoteProbe(mqtt_broker, serial_port=p)
                for p in moteprobe.find_serial_ports(port_mask=self.port_mask)
           ]

        # create a MoteConnector for each MoteProbe
        try:
            fw_defines = self.extract_stack_defines()
        except IOError as err:
            log.critical("Could not updated firmware definitions: {}".format(err))
            os.kill(os.getpid(), signal.SIGTERM)
            return

        self.mote_connectors = [moteconnector.MoteConnector(mp, fw_defines) for mp in self.mote_probes]

        # create a MoteState for each MoteConnector
        self.mote_states = [motestate.MoteState(mc) for mc in self.mote_connectors]

        # set up RPC server
        SimpleXMLRPCServer.__init__(self, (self.host, self.port), allow_none=True, logRequests=False)

        self.register_introspection_functions()

        # register RPCs
        self.register_function(self.shutdown)
        self.register_function(self.get_mote_dict)
        self.register_function(self.boot_motes)
        self.register_function(self.set_root)
        self.register_function(self.get_mote_state)
        self.register_function(self.get_dagroot)
        self.register_function(self.get_dag)
        self.register_function(self.get_motes_connectivity)
        self.register_function(self.get_ebm_wireshark_enabled)
        self.register_function(self.get_ebm_stats)

        if self.simulator_mode and self.auto_boot:
            self.boot_motes(['all'])

        if self.webserver:
            web_server = bottle.Bottle()
            WebServer(web_server, (self.host, self.port))

            # start web interface in a separate thread
            web_thread = threading.Thread(
                target=web_server.run,
                kwargs={
                    'host': self.host,
                    'port': self.webserver,
                    'quiet': True,
                }
            )
            web_thread.start()

        time.sleep(1)
        if root is not None:
            if self.simulator_mode and self.auto_boot is False:
                log.warning("Cannot set root when motes are not booted!")
            else:
                self.set_root(root)

    @staticmethod
    def cleanup_temporary_files(files):
        for f in files:
            log.verbose("Cleaning up files: {}".format(f))
            shutil.rmtree(f, ignore_errors=True)

    def copy_sim_fw(self):
        hosts = ['amd64-linux', 'x86-linux', 'amd64-windows', 'x86-windows']
        if os.name == 'nt':
            index = 2 if platform.architecture()[0] == '64bit' else 3
        else:
            index = 0 if platform.architecture()[0] == '64bit' else 1

        host = hosts[index]

        # in openwsn-fw, directory containing 'openwsnmodule_obj.h'
        inc_dir = os.path.join(self.fw_path, 'bsp', 'boards', 'python')
        if not os.path.exists(inc_dir):
            log.error("Path '{}' does not exist".format(inc_dir))
            return

        # in openwsn-fw, directory containing extension library
        lib_dir = os.path.join(self.fw_path, 'build', 'python_gcc', 'projects', 'common')
        if not os.path.exists(lib_dir):
            log.error("Path '{}' does not exist".format(lib_dir))
            return

        temp_dir = tempfile.mkdtemp()

        # Build source and destination pathnames.
        arch_and_os = host.split('-')
        lib_ext = 'pyd' if arch_and_os[1] == 'windows' else 'so'
        source_name = 'oos_openwsn.{0}'.format(lib_ext)
        dest_name = 'oos_openwsn-{0}.{1}'.format(arch_and_os[0], lib_ext)
        dest_dir = os.path.join(temp_dir, arch_and_os[1])

        shutil.copy(os.path.join(inc_dir, 'openwsnmodule_obj.h'), temp_dir)
        log.verbose(
            "Copying '{}' to temporary dir '{}'".format(os.path.join(inc_dir, 'openwsnmodule_obj.h'), temp_dir))

        try:
            os.makedirs(os.path.join(dest_dir))
        except OSError:
            pass

        shutil.copy(os.path.join(lib_dir, source_name), os.path.join(dest_dir, dest_name))
        log.verbose(
            "Copying '{}' to '{}'".format(os.path.join(lib_dir, source_name), os.path.join(dest_dir, dest_name)))

        # Copy the module directly to sim_files directory if it matches this host.
        if arch_and_os[0] == 'amd64':
            arch_match = platform.architecture()[0] == '64bit'
        else:
            arch_match = platform.architecture()[0] == '32bit'
        if arch_and_os[1] == 'windows':
            os_match = os.name == 'nt'
        else:
            os_match = os.name == 'posix'

        if arch_match and os_match:
            shutil.copy(os.path.join(lib_dir, source_name), temp_dir)

        return temp_dir

    def extract_stack_defines(self):
        log.info('Extracting firmware definitions.')
        definitions = {
            "components": extract_component_codes(os.path.join(self.fw_path, 'inc', 'opendefs.h')),
            "log_descriptions": extract_log_descriptions(os.path.join(self.fw_path, 'inc', 'opendefs.h')),
            "sixtop_returncodes": extract_6top_rcs(os.path.join(self.fw_path, 'openstack', '02b-MAChigh', 'sixtop.h')),
            "sixtop_states": extract_6top_states(os.path.join(self.fw_path, 'openstack', '02b-MAChigh', 'sixtop.h'))
        }

        return definitions

    # ======================== RPC functions ================================

    def shutdown(self):
        """ Closes all thread-based components. """
        log.debug('RPC: {}'.format(self.shutdown.__name__))

        self.opentun.close()
        self.rpl.close()
        self.jrc.close()
        for probe in self.mote_probes:
            probe.close()

        if self.simulator_mode:
            OpenVisualizerServer.cleanup_temporary_files([self.temp_dir])

        os.kill(os.getpid(), signal.SIGTERM)

    def get_dag(self):
        return self.topology.get_dag()

    def boot_motes(self, addresses):
        # boot all emulated motes, if applicable
        log.debug('RPC: {}'.format(self.boot_motes.__name__))

        if self.simulator_mode:
            self.simengine.pause()
            now = self.simengine.timeline.get_current_time()
            if len(addresses) == 1 and addresses[0] == "all":
                for rank in range(self.simengine.get_num_motes()):
                    mh = self.simengine.get_mote_handler(rank)
                    if not mh.hw_supply.mote_on:
                        self.simengine.timeline.schedule_event(now, mh.get_id(), mh.hw_supply.switch_on,
                                                               mh.hw_supply.INTR_SWITCHON)
                    else:
                        raise Fault(faultCode=-1, faultString="Mote already booted.")
            else:
                for address in addresses:
                    try:
                        address = int(address)
                    except ValueError:
                        raise Fault(faultCode=-1, faultString="Invalid mote address: {}".format(address))

                    for rank in range(self.simengine.get_num_motes()):
                        mh = self.simengine.get_mote_handler(rank)
                        if address == mh.get_id():
                            if not mh.hw_supply.mote_on:
                                self.simengine.timeline.schedule_event(now, mh.get_id(), mh.hw_supply.switch_on,
                                                                       mh.hw_supply.INTR_SWITCHON)
                            else:
                                raise Fault(faultCode=-1, faultString="Mote already booted.")

            self.simengine.resume()
            return True
        else:
            raise Fault(faultCode=-1, faultString="Method not supported on real hardware")

    def set_root(self, port_or_address):
        log.debug('RPC: {}'.format(self.set_root.__name__))

        mote_dict = self.get_mote_dict()
        if port_or_address in mote_dict:
            port = mote_dict[port_or_address]
        elif port_or_address in mote_dict.values():
            port = port_or_address
        else:
            raise Fault(faultCode=-1, faultString="Unknown port or address: {}".format(port_or_address))

        for ms in self.mote_states:
            try:
                if ms.mote_connector.serialport == port:
                    ms.trigger_action(MoteState.TRIGGER_DAGROOT)
                    self.dagroot = ms.get_state_elem(ms.ST_IDMANAGER).get_16b_addr()
                    log.info('Setting mote {} as root'.format(''.join(['%02d' % b for b in self.dagroot])))
                    return True
            except ValueError as err:
                log.error(err)
                break
        raise Fault(faultCode=-1, faultString="Could not set {} as root".format(port))

    def get_dagroot(self):
        log.debug('RPC: {}'.format(self.get_dagroot.__name__))
        return self.dagroot

    def get_mote_state(self, mote_id):
        """
        Returns the MoteState object for the provided connected mote.
        :param mote_id: 16-bit ID of mote
        :rtype: MoteState or None if not found
        """
        log.debug('RPC: {}'.format(self.get_mote_state.__name__))

        for ms in self.mote_states:
            id_manager = ms.get_state_elem(ms.ST_IDMANAGER)
            if id_manager and id_manager.get_16b_addr():
                addr = ''.join(['%02x' % b for b in id_manager.get_16b_addr()])
                if addr == mote_id:
                    return OpenVisualizerServer._extract_mote_states(ms)
        else:
            error_msg = "Unknown mote ID: {}".format(mote_id)
            log.warning("returning fault: {}".format(error_msg))
            raise Fault(faultCode=-1, faultString=error_msg)

    def get_ebm_wireshark_enabled(self):
        return self.ebm.wireshark_debug_enabled

    def get_ebm_stats(self):
        return self.ebm.get_stats()

    def get_motes_connectivity(self):
        motes = []
        states = []
        edges = []
        src_s = None

        for ms in self.mote_states:
            id_manager = ms.get_state_elem(ms.ST_IDMANAGER)
            if id_manager and id_manager.get_16b_addr():
                src_s = ''.join(['%02X' % b for b in id_manager.get_16b_addr()])
                motes.append(src_s)
            neighbor_table = ms.get_state_elem(ms.ST_NEIGHBORS)
            for neighbor in neighbor_table.data:
                if len(neighbor.data) == 0:
                    break
                if neighbor.data[0]['used'] == 1 and neighbor.data[0]['parentPreference'] == 1:
                    dst_s = ''.join(['%02X' % b for b in neighbor.data[0]['addr'].addr[-2:]])
                    edges.append({'u': src_s, 'v': dst_s})
                    break

        motes = list(set(motes))
        for mote in motes:
            d = {'id': mote, 'value': {'label': mote}}
            states.append(d)
        return states, edges

    def get_mote_dict(self):
        """ Returns a dictionary with key-value entry: (mote_id: serialport) """
        log.debug('RPC: {}'.format(self.get_mote_dict.__name__))

        mote_dict = {}

        for ms in self.mote_states:
            addr = ms.get_state_elem(motestate.MoteState.ST_IDMANAGER).get_16b_addr()
            if addr:
                mote_dict[''.join(['%02x' % b for b in addr])] = ms.mote_connector.serialport
            else:
                mote_dict[ms.mote_connector.serialport] = None

        return mote_dict

    @staticmethod
    def _extract_mote_states(ms):
        states = {
            ms.ST_IDMANAGER: ms.get_state_elem(ms.ST_IDMANAGER).to_json('data'),
            ms.ST_ASN: ms.get_state_elem(ms.ST_ASN).to_json('data'),
            ms.ST_ISSYNC: ms.get_state_elem(ms.ST_ISSYNC).to_json('data'),
            ms.ST_MYDAGRANK: ms.get_state_elem(ms.ST_MYDAGRANK).to_json('data'),
            ms.ST_KAPERIOD: ms.get_state_elem(ms.ST_KAPERIOD).to_json('data'),
            ms.ST_OUPUTBUFFER: ms.get_state_elem(ms.ST_OUPUTBUFFER).to_json('data'),
            ms.ST_BACKOFF: ms.get_state_elem(ms.ST_BACKOFF).to_json('data'),
            ms.ST_MACSTATS: ms.get_state_elem(ms.ST_MACSTATS).to_json('data'),
            ms.ST_SCHEDULE: ms.get_state_elem(ms.ST_SCHEDULE).to_json('data'),
            ms.ST_QUEUE: ms.get_state_elem(ms.ST_QUEUE).to_json('data'),
            ms.ST_NEIGHBORS: ms.get_state_elem(ms.ST_NEIGHBORS).to_json('data'),
            ms.ST_JOINED: ms.get_state_elem(ms.ST_JOINED).to_json('data'),
        }
        return states


def _add_parser_args(parser):
    """ Adds arguments specific to the OpenVisualizer application """
    parser.add_argument(
        '-s', '--sim',
        dest='simulator_mode',
        default=0,
        type=int,
        help='Run a simulation with the given amount of emulated motes'
    )

    parser.add_argument(
        '--fw-path',
        dest='fw_path',
        type=str,
        help='Provide the path to the OpenWSN firmware. This option overrides the OPENWSN_FW_BASE environment variable.'
    )

    parser.add_argument(
        '-o', '--simtopo',
        dest='sim_topology',
        default='',
        action='store',
        help='force a certain topology (simulation mode only)'
    )

    parser.add_argument(
        '--root',
        dest='set_root',
        action='store',
        type=str,
        help='Set a simulated or hardware mote as root, specify the mote\'s port or address'
    )

    parser.add_argument(
        '-d', '--debug',
        dest='debug',
        action='store',
        help='Set the debugging level, default is INFO.'
    )

    parser.add_argument(
        '-l', '--lconf',
        dest='lconf',
        action='store',
        help='Provide a logging configuration'
    )

    parser.add_argument(
        '--vcdlog',
        dest='vcdlog',
        default=False,
        action='store_true',
        help='use VCD logger'
    )

    parser.add_argument(
        '-z', '--pagezero',
        dest='use_page_zero',
        default=False,
        action='store_true',
        help='use page number 0 in page dispatch (only works with one-hop)'
    )

    parser.add_argument(
        '-i', '--iotlab',
        dest='iotlab_motes',
        default='',
        action='store',
        help='comma-separated list of IoT-LAB motes (e.g. "wsn430-9,wsn430-34,wsn430-3")'
    )

    parser.add_argument(
        '-b', '--opentestbed',
        dest='testbed_motes',
        default=False,
        action='store_true',
        help='connect motes from opentestbed'
    )

    parser.add_argument(
        '--mqtt-broker',
        dest='mqtt_broker',
        default='argus.paris.inria.fr',
        action='store',
        help='MQTT broker address to use'
    )

    parser.add_argument(
        '--opentun',
        dest='opentun',
        default=False,
        action='store_true',
        help='use TUN device to route packets to the Internet'
    )

    parser.add_argument(
        '-H',
        '--host',
        dest='host',
        default='localhost',
        action='store',
        help='host address'
    )

    parser.add_argument(
        '-P',
        '--port',
        dest='port',
        default=9000,
        action='store',
        help='port number'
    )

    parser.add_argument(
        '-w',
        '--webserver',
        dest='webserver',
        action='store',
        help='port number for the webserver'
    )
    parser.add_argument(
        '--port-mask',
        dest='port_mask',
        type=str,
        action='store',
        nargs='+',
        help='port mask for serial port detection, e.g: /dev/tty/USB*'
    )

    parser.add_argument(
        '--no-boot',
        dest='auto_boot',
        default=True,
        action='store_false',
        help='disables automatic boot of emulated motes'
    )


# ============================ main ============================================

def main():
    """ Entry point for the openvisualizer server. """

    banner = [""]
    banner += [" ___                 _ _ _  ___  _ _ "]
    banner += ["| . | ___  ___ ._ _ | | | |/ __>| \ |"]
    banner += ["| | || . \/ ._>| ' || | | |\__ \|   |"]
    banner += ["`___'|  _/\___.|_|_||__/_/ <___/|_\_|"]
    banner += ["     |_|                  openwsn.org"]
    banner += [""]

    print '\n'.join(banner)

    parser = ArgumentParser()
    _add_parser_args(parser)
    args = parser.parse_args()

    # loading the logging configuration
    if not args.lconf and pkg_resources.resource_exists(PACKAGE_NAME, DEFAULT_LOGGING_CONF):
        logging.config.fileConfig(pkg_resources.resource_stream(PACKAGE_NAME, DEFAULT_LOGGING_CONF))
        log.verbose("Loading logging configuration: {}".format(DEFAULT_LOGGING_CONF))
    elif args.lconf:
        logging.config.fileConfig(args.lconf)
        log.verbose("Loading logging configuration: {}".format(args.logconf))
    else:
        log.error("Could not load logging configuration.")

    options = ['host address server     = {0}'.format(args.host), 'port number server      = {0}'.format(args.port)]

    if args.webserver:
        options.append('webserver port          = {0}'.format(args.webserver))

    if args.fw_path:
        options.append('firmware path           = {0}'.format(args.fw_path))
    else:
        try:
            options.append('firmware path           = {0}'.format(os.environ['OPENWSN_FW_BASE']))
        except KeyError:
            log.warning(
                "Unknown openwsn-fw location, specify with option '--fw-path' or by exporting the OPENWSN_FW_BASE "
                "environment variable.")

    if args.simulator_mode:
        options.append('simulation              = {0}'.format(args.simulator_mode)),
        if args.sim_topology:
            options.append('simulation topology     = {0}'.format(args.sim_topology))
        else:
            options.append('simulation topology     = {0}'.format('Pister-hack'))

        options.append('auto-boot sim motes     = {0}'.format(args.auto_boot))

    if args.set_root:
        options.append('set root                = {0}'.format(args.set_root))

    options.append('use page zero           = {0}'.format(args.use_page_zero))
    options.append('use VCD logger          = {0}'.format(args.vcdlog))

    if args.port_mask:
        options.append('serial port mask        = {0}'.format(args.port_mask))

    if args.testbed_motes:
        options.append('opentestbed             = {0}'.format(args.testbed_motes))
        options.append('mqtt broker             = {0}'.format(args.mqtt_broker))

    log.info('Initializing OV Server with options:\n\t- {0}'.format('\n\t- '.join(options)))

    log.debug('sys.path:\n\t{0}'.format('\n\t'.join(str(p) for p in sys.path)))

    server = OpenVisualizerServer(
        host=args.host,
        port=args.port,
        webserver=args.webserver,
        simulator_mode=args.simulator_mode,
        debug=args.debug,
        use_page_zero=args.use_page_zero,
        vcdlog=args.vcdlog,
        sim_topology=args.sim_topology,
        port_mask=args.port_mask,
        iotlab_motes=args.iotlab_motes,
        testbed_motes=args.testbed_motes,
        mqtt_broker=args.mqtt_broker,
        opentun=args.opentun,
        fw_path=args.fw_path,
        auto_boot=args.auto_boot,
        root=args.set_root
    )

    try:
        log.info("Starting RPC server")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.shutdown()
