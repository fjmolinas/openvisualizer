#!/usr/bin/python
# Copyright (c) 2010-2013, Regents of the University of California. 
# All rights reserved. 
#  
# Released under the BSD 3-Clause license as published at the link below.
# https://openwsn.atlassian.net/wiki/display/OW/License

import logging
import logging.config
import os
import signal
import sys
import threading
import time
from argparse import ArgumentParser
from cmd import Cmd

import bottle
import coloredlogs

# do not remove the line below, prevents PyCharm from optimizing out the Python path modification
# noinspection PyUnresolvedReferences
import helpers.build_python_path
import openvisualizer_app
import utils as u
from openvisualizer.jrc import jrc
from openvisualizer.motehandler.moteconnector.openparser import parseriec
from openvisualizer.motehandler.motestate.motestate import MoteState
from openvisualizer.openlbr import sixlowpan_frag
from openvisualizer.opentun import opentun, opentunwindows, opentunmacos, opentunlinux
from openvisualizer.rpl import rpl
from webserver import WebServer

# do not remove line below, prevents PyCharm from optimizing out the next import

log = logging.getLogger('OpenVisualizerCli')

coloredlogs.install()

# ============================ functions =======================================

def list_from_string(list_str=None):
    """Get list of items from `list_str`

    >>> list_from_string(None)
    []
    >>> list_from_string("")
    []
    >>> list_from_string("  ")
    []
    >>> list_from_string("a")
    ['a']
    >>> list_from_string("a  ")
    ['a']
    >>> list_from_string("a b  c")
    ['a', 'b', 'c']
    """
    value = (list_str or '').split(' ')
    return [v for v in value if v]

# ============================ class ===========================================

class OpenVisualizerCli(Cmd):

    def __init__(self, app):
        log.debug('create instance')

        # store params
        self.app = app
        self.debug = False

        Cmd.__init__(self)
        self.doc_header = 'Commands (type "help all" or "help <topic>"):'
        self.prompt = '> '
        self.intro = '\nOpenVisualizer (type "help" for commands)'

    # ======================== public ==========================================

    def start_webserver(self, args):
        log.info(
            'Initializing webserver with options: \n\t\t{0}'.format(
                '\n\t\t'.join(
                    ['host: {0}'.format(args.host), 'port: {0}'.format(args.port)]
                )
            )
        )

        # ===== add a web interface
        web_server = bottle.Bottle()
        WebServer(self.app, web_server)

        # start web interface in a separate thread
        webthread = threading.Thread(
            target=web_server.run,
            kwargs={
                'host': args.host,
                'port': args.port,
                'quiet': not self.debug,
                'debug': self.debug,
            }
        )
        webthread.start()

    # ======================== private =========================================

    # ===== callbacks

    def do_state(self, arg):
        """
        Prints provided state, or lists states.
        Usage: state [state-name]
        """
        if not arg:
            for ms in self.app.mote_states:
                output = []
                output += ['Available states:']
                output += [' - {0}'.format(s) for s in ms.get_state_elem_names()]
                self.stdout.write('\n'.join(output))
            self.stdout.write('\n')
        else:
            for ms in self.app.mote_states:
                try:
                    self.stdout.write(str(ms.get_state_elem(arg)))
                    self.stdout.write('\n')
                except ValueError as err:
                    self.stdout.write(err)

    def do_root(self, arg):
        """
        Sets dagroot to the provided mote, or lists motes
        Usage: root [serial-port]
        """

        if not arg:
            self.stdout.write('\nAvailable ports:')
            if self.app.mote_states:
                for ms in self.app.mote_states:
                    self.stdout.write('  {0}'.format(ms.mote_connector.serialport))
            else:
                self.stdout.write('  <none>')
            self.stdout.write('\n')
        else:
            for ms in self.app.mote_states:
                try:
                    if ms.mote_connector.serialport == arg:
                        ms.trigger_action(MoteState.TRIGGER_DAGROOT)
                except ValueError as err:
                    self.stdout.write(err)

    def do_set(self, arg):
        """
        Sets mote with parameters
        Usage: set [serial-port] [command] [parameter]
        """

        if not arg:
            self.stdout.write('Available ports:')
            if self.app.mote_states:
                for ms in self.app.mote_states:
                    self.stdout.write('  {0}'.format(ms.mote_connector.serialport))
            else:
                self.stdout.write('  <none>')
            self.stdout.write('\n')
        else:
            try:
                [port, command, parameter] = arg.split(' ')
                for ms in self.app.mote_states:
                    try:
                        if ms.mote_connector.serialport == port:
                            ms.trigger_action([MoteState.SET_COMMAND, command, parameter])
                    except ValueError as err:
                        self.stdout.write(err)
            except ValueError as err:
                print "{0}:{1}".format(type(err), err)

    def help_all(self):
        """ Lists first line of help for all documented commands """
        names = self.get_names()
        names.sort()
        max_len = 65
        self.stdout.write(
            'type "help <topic>" for topic details\n')
        for name in names:
            if name[:3] == 'do_':
                try:
                    doc = getattr(self, name).__doc__
                    if doc:
                        # Handle multi-line doc comments and format for length.
                        doclines = doc.splitlines()
                        doc = doclines[0]
                        if len(doc) == 0 and len(doclines) > 0:
                            doc = doclines[1].strip()
                        if len(doc) > max_len:
                            doc = doc[:max_len] + '...'
                        self.stdout.write('{0} - {1}\n'.format(
                            name[3:80 - max_len], doc))
                except AttributeError:
                    pass

    def do_quit(self, arg):
        self.app.close()
        os.kill(os.getpid(), signal.SIGTERM)

    def emptyline(self):
        return

    def cmdloop(self, intro=None):
        """ Override cmdloop method to catch and handle Ctrl-C. """
        try:
            Cmd.cmdloop(self, intro=intro)
        except KeyboardInterrupt:
            print("\nYou pressed Ctrl-C. Killing OpenVisualizer..\n")
            self.app.close()
            os.kill(os.getpid(), signal.SIGTERM)


DEFAULT_MOTE_COUNT = 3


def _add_parser_args(parser):
    """ Adds arguments specific to the OpenVisualizer application """
    parser.add_argument(
        '-s', '--sim',
        dest='simulator_mode',
        default=False,
        action='store_true',
        help='simulation mode, with default of {0} motes'.format(DEFAULT_MOTE_COUNT)
    )

    parser.add_argument(
        '-n', '--simCount',
        dest='num_motes',
        type=int,
        default=0,
        help='simulation mode, with provided mote count'
    )

    parser.add_argument(
        '-t', '--trace',
        dest='trace',
        default=False,
        action='store_true',
        help='enables memory debugging'
    )

    parser.add_argument(
        '--no-color',
        dest='no_color',
        default=False,
        action='store_true',
        help='disables colored logging output'
    )

    parser.add_argument(
        '-o', '--simTopology',
        dest='sim_topology',
        default='',
        action='store',
        help='force a certain topology (simulation mode only)'
    )

    parser.add_argument(
        '-d', '--debug',
        dest='debug',
        default=False,
        action='store_true',
        help='enables application debugging'
    )

    parser.add_argument(
        '-z', '--usePageZero',
        dest='use_page_zero',
        default=False,
        action='store_true',
        help='use page number 0 in page dispatch (only works with one-hop)'
    )

    parser.add_argument(
        '-i', '--iotlabMotes',
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
        default='',
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
        '-p', '--pathTopo',
        dest='path_topo',
        default='',
        action='store',
        help='a topology can be loaded from a json file'
    )

    parser.add_argument(
        '-r', '--root',
        dest='root',
        default='',
        action='store',
        help='set mote associated to serial port as root'
    )
    parser.add_argument(
        '-H',
        '--host',
        dest='host',
        default='0.0.0.0',
        action='store',
        help='host address'
    )

    parser.add_argument(
        '-P',
        '--port',
        dest='port',
        default=8080,
        action='store',
        help='port number'
    )

    parser.add_argument(
        '-a', '--appDir',
        dest='appdir',
        default='.',
        action='store',
        help='working directory'
    )

    parser.add_argument(
        '--port-mask',
        dest='port_mask',
        type=list_from_string,
        action='store',
        help='port mask for serial port detection, e.g \'/dev/tty/USB*\''
    )


# ============================ main ============================================


def main():
    parser = ArgumentParser()
    _add_parser_args(parser)

    args = parser.parse_args()

    conf_dir, data_dir, log_dir = u.init_external_dirs(args.appdir, args.debug)

    # Must use a '/'-separated path for log dir, even on Windows.
    logging.config.fileConfig(os.path.join(conf_dir, 'logging.conf'),
                              {'logDir': u.force_slash_sep(log_dir, args.debug)})

    if not args.no_color:
        loggers = [parseriec.log, rpl.log, jrc.log, opentun.log, log, openvisualizer_app.log, sixlowpan_frag.log]
        style = '%(asctime)s %(levelname)s %(message)s'
        datefmt = '%H:%M:%S'

        if sys.platform.startswith('win32'):
            fs = {'asctime': {'color': 'cyan'}, 'levelname': {'bold': True, 'color': 'cyan'}}
            ls = {'critical': {'bold': True, 'color': 'red'}, 'error': {'color': 'red', 'bold': True},
                  'warning': {'color': 'yellow'}, 'success': {'bold': True, 'color': 'green'}}
            loggers.append(opentunwindows.log)
        else:
            fs = {'asctime': {'color': 35}, 'levelname': {'bold': True, 'color': 31}}
            ls = {'critical': {'bold': True, 'color': 'red'}, 'error': {'color': 124},
                  'verbose': {'color': 87}, 'warning': {'color': 166},
                  'success': {'color': 83, 'bold': True}}
            if sys.platform.startswith('darwin'):
                loggers.append(opentunmacos.log)
            else:
                loggers.append(opentunlinux.log)

        for lg in loggers:
            coloredlogs.install(level='VERBOSE', logger=lg, fmt=style, datefmt=datefmt, field_styles=fs,
                                level_styles=ls)

    # initialize OpenVisualizer application
    app = openvisualizer_app.main(parser, conf_dir, data_dir, log_dir, DEFAULT_MOTE_COUNT)
    cli = OpenVisualizerCli(app)

    log.debug('Using external dirs:\n\t\t{}'.format(
        '\n\t\t'.join(['conf     = {0}'.format(conf_dir),
                       'data     = {0}'.format(data_dir),
                       'log      = {0}'.format(log_dir)],
                      )))

    # log

    time.sleep(0.1)

    cli.start_webserver(args)
    cli.do_root(None)

    cli.cmdloop()


if __name__ == "__main__":
    main()
