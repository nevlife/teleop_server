"""Pytest bootstrap for teleop_server.

This server is a flat-layout (modules sit at the top level — ``state.py``,
``robot_bridge.py``, etc.). The vendored ``teleop_contracts`` submodule
isn't installed into the venv, so we prepend its directory to ``sys.path``
here. The same shim runs in production via ``main.py``.
"""
import os
import sys

_CONTRACTS_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "teleop_contracts")
)
if _CONTRACTS_ROOT not in sys.path:
    sys.path.insert(0, _CONTRACTS_ROOT)
