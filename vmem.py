"""
vmem.py — shim for OpenDroneMap's missing vmem C extension.
Delegates to psutil which is cross-platform.
"""
from psutil import virtual_memory  # noqa: F401 – re-exported as-is
