#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# This file is part of the ModBus TCP proxy project
#
# Copyright (c) 2020 Tiago Coutinho
# Distributed under the GNU General Public License v3. See LICENSE for more info.

"""The setup script."""

from setuptools import setup

with open('README.md') as readme_file:
    readme = readme_file.read()

with open('HISTORY.md') as history_file:
    history = history_file.read()

setup_requirements = ['pytest-runner', ]

test_requirements = ['pytest>=3', ]

setup(
    author="Tiago Coutinho",
    author_email='coutinhotiago@gmail.com',
    python_requires='>=3.7',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Intended Audience :: Manufacturing',
        'Intended Audience :: Science/Research',
        'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',
        'Natural Language :: English',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
    ],
    description="ModBus TCP proxy",
    entry_points={
        "console_scripts": [
            'modbus-proxy=modbus_proxy:main',
        ],
    },
    license="GNU General Public License v3",
    long_description=readme + '\n\n' + history,
    long_description_content_type="text/markdown",
    include_package_data=True,
    keywords='modbus, tcp, proxy',
    name='modbus-proxy',
    py_modules=['modbus_proxy'],
    setup_requires=setup_requirements,
    test_suite='tests',
    tests_require=test_requirements,
    url='https://github.com/tiagocoutinho/modbus-proxy',
    version='0.3.0',
    zip_safe=False,
)
