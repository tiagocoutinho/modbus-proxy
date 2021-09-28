# -*- coding: utf-8 -*-
#
# This file is part of the modbus-proxy project
#
# Copyright (c) 2020-2021 Tiago Coutinho
# Distributed under the GPLv3 license. See LICENSE for more info.

"""Tests for `modbus_proxy` package."""

from urllib.parse import urlparse

import pytest

from modbus_proxy import parse_url


@pytest.mark.parametrize(
    "url, expected",
    [("tcp://host:502", urlparse("tcp://host:502")),
     ("host:502", urlparse("tcp://host:502")),
     ("tcp://:502", urlparse("tcp://0:502")),
     (":502", urlparse("tcp://0:502")),
     ])
def test_parse_url(url, expected):
    assert parse_url(url) == expected

