"""LabAIAgent -- Connected Orchestration of Networked Devices, Unified
Instrument Toolkit.

A vendor-neutral layer that makes laboratory instruments discoverable,
safely operable, and auditable by software agents -- one instrument at a time.

    from labaiagent import LabSession
    from labaiagent.drivers.simulated import World, SimulatedPlateReader

    world = World()
    session = LabSession([SimulatedPlateReader("reader", world=world)])
    session.connect_all()
    session.call("reader", "read_absorbance", wavelength=562.0)
"""

__version__ = "1.4.1"

from .core import errors
from .core.audit import AuditLog
from .core.capability import Capability, procedure, read, write
from .core.device import Device
from .core.registry import get_driver, list_drivers, register_driver
from .core.safety import EmergencyStop, Interlock, SafetyEngine
from .core.types import (
    DeviceState,
    Kind,
    Length,
    OneOf,
    Param,
    Pattern,
    Predicate,
    Quantity,
    Range,
    Risk,
)
from .orchestration import LabSession, OnError, Protocol, Step
from .orchestration.jobs import JobManager, JobState

__all__ = [
    "__version__",
    "Device", "read", "write", "procedure", "Capability",
    "Risk", "Kind", "DeviceState", "Quantity", "Param",
    "Range", "OneOf", "Pattern", "Length", "Predicate",
    "register_driver", "get_driver", "list_drivers",
    "LabSession", "Protocol", "Step", "OnError",
    "JobManager", "JobState",
    "EmergencyStop", "Interlock", "SafetyEngine", "AuditLog", "errors",
]
