
#! /usr/bin/env python
# -*- coding:utf-8 -*-

import os

from setuptools import setup, find_packages

'''
This implementation of the traditional setup.py uses the root package's package_data parameter to store data files, 
rather than the application-level data_files parameter. This arrangement organizes OpenVisualizer within a single tree 
of directories, and so is more portable.

In contrast to the native setup, the installer is free to relocate the tree of directories with install options for 
setup.py.

This implementation is based on setuptools, and builds the list of module dependencies by reading 'requirements.txt'.
'''

PACKAGE = 'openVisualizer'
LICENSE = 'BSD 3-Clause'

def get_version(package):
    """ Extract package version without importing file.
    Inspired from iotlab-cli setup.py
    """
    with open(os.path.join(package, '__init__.py')) as init_fd:
        for line in init_fd:
            if line.startswith('__version__'):
                return eval(line.split('=')[-1])  # pylint:disable=eval-used

WEB_STATIC = 'bin/web_files/static'
WEB_TEMPLATES = 'bin/web_files/templates'
SIM_DATA = 'bin/sim_files'

LONG_DESCRIPTION = ['README.rst']

REQUIREMENTS = [i.strip() for i in open("requirements.txt").readlines()]

SCRIPTS = ['openv-cli']

DEPRECATED_SCRIPTS = [
    'opencli.py',
    'openvisualizer_cli.py',
    'openvisualizer_app.py',
    'webserver.py'
    ]

SCRIPTS += DEPRECATED_SCRIPTS

setup(
    name=PACKAGE,
    version=get_version(PACKAGE.lower()),
    packages=find_packages(),
    scripts=SCRIPTS,
    package_dir={'': '.', 'openvisualizer': 'openvisualizer'},
    # Copy sim_data files by extension so don't copy .gitignore in that directory.
    package_data={'openvisualizer': [
        'bin/*.conf',
        'requirements.txt',
        '/'.join([WEB_STATIC, 'css', '*']),
        '/'.join([WEB_STATIC, 'font-awesome', 'css', '*']),
        '/'.join([WEB_STATIC, 'font-awesome', 'fonts', '*']),
        '/'.join([WEB_STATIC, 'images', '*']),
        '/'.join([WEB_STATIC, 'js', '*.js']),
        '/'.join([WEB_STATIC, 'js', 'plugins', 'metisMenu', '*']),
        '/'.join([WEB_TEMPLATES, '*']),
        '/'.join([SIM_DATA, 'windows', '*.pyd']),
        '/'.join([SIM_DATA, 'linux', '*.so']),
        '/'.join([SIM_DATA, '*.h'])
    ]},
    install_requires=REQUIREMENTS,
    # Must extract zip to edit conf files.
    zip_safe=False,
    author='Thomas Watteyne',
    author_email='watteyne@eecs.berkeley.edu',
    description='Wireless sensor network monitoring, visualization, and debugging tool',
    long_description=LONG_DESCRIPTION,
    url='https://openwsn.atlassian.net/wiki/display/OW/OpenVisualizer',
    keywords=['6TiSCH', 'Internet of Things', '6LoWPAN', '802.15.4e', 'sensor', 'mote'],
    platforms=['platform-independent'],
    license=LICENSE,
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2.7',
        'Topic :: Communications',
        'Topic :: Home Automation',
        'Topic :: Internet',
        'Topic :: Software Development',
    ],
)
