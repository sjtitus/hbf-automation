#!/usr/bin/env python3
"""Thin shim — preserves `python3 process_badger.py <dir>` muscle memory.

Equivalent to `python3 -m hbf_shipping --vendor badger <dir>`.
"""
import sys

from hbf_shipping.cli import main

sys.argv[1:1] = ['--vendor', 'badger']
main()
