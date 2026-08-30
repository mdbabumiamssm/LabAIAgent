"""Exception hierarchy.

The split matters operationally: a ``TransientError`` is worth retrying,
a ``SafetyViolation`` never is, and a ``PhysicalError`` means a human has to
walk over to the bench before anything else happens.
"""

from __future__ import annotations

from typing import Any


class LabAIAgentError(Exception):
    """Base for every error raised by the framework."""


class ConfigurationError(LabAIAgentError):
    """Malformed lab config, unknown driver, bad wiring."""


class DriverError(LabAIAgentError):
    """A driver violated its contract (bad manifest, wrong return type)."""


class TransportError(LabAIAgentError):
    """Communication with the instrument failed."""


class TransientError(TransportError):
    """Communication failed in a way that is plausibly worth retrying.

    Timeouts, busy responses, transient socket resets. The scheduler will
    apply the retry policy to these and only these.
    """


class DeviceNotFound(LabAIAgentError):
    pass


class CapabilityNotFound(LabAIAgentError):
    pass


class InvalidState(LabAIAgentError):
    """Capability invoked while the device was in an incompatible state."""


class SafetyViolation(LabAIAgentError):
    """A declared limit or interlock rejected the call. Never retried.

    Carries structured detail so the audit record and the agent-facing error
    message can both be precise about which constraint failed.
    """

    def __init__(self, message: str, *, device: str = "", capability: str = "",
                 constraint: str = "", detail: Any = None) -> None:
        super().__init__(message)
        self.device = device
        self.capability = capability
        self.constraint = constraint
        self.detail = detail

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,   # the typed subclass name is part
                                            # of the caller's repair signal
            "message": str(self),
            "device": self.device,
            "capability": self.capability,
            "constraint": self.constraint,
            "detail": self.detail,
        }


class LimitViolation(SafetyViolation):
    """A declared parameter limit rejected an argument.

    Carries the parameter, the offending value and the permitted range so the
    caller -- very often a model -- gets a repair instruction rather than a
    bare rejection.
    """

    def __init__(self, message: str, *, parameter: str = "", value: Any = None,
                 permitted: str = "", unit: str = "", **kw: Any) -> None:
        super().__init__(message, **kw)
        self.parameter = parameter
        self.value = value
        self.permitted = permitted
        self.unit = unit

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d.update({"error": "LimitViolation", "parameter": self.parameter,
                  "value": self.value, "permitted": self.permitted,
                  "unit": self.unit})
        return d


class InterlockFailure(SafetyViolation):
    """A named precondition interlock returned False."""


class EmergencyStopActive(SafetyViolation):
    """Actuation attempted while the e-stop latch was set."""


class ConfirmationRequired(SafetyViolation):
    """A high-risk capability was invoked without an approval token."""


class PhysicalError(LabAIAgentError):
    """The instrument reports a fault requiring human intervention.

    Distinct from TransportError: the link is fine, the hardware is not.
    Never retried automatically.
    """


class WorkflowError(LabAIAgentError):
    """A protocol step failed and recovery could not continue."""

    def __init__(self, message: str, *, step: str = "", cause: Exception | None = None):
        super().__init__(message)
        self.step = step
        self.cause = cause


class ConformanceError(LabAIAgentError):
    """A driver failed the conformance suite."""


__all__ = [
    "LabAIAgentError", "ConfigurationError", "DriverError", "TransportError",
    "TransientError", "DeviceNotFound", "CapabilityNotFound", "InvalidState",
    "SafetyViolation", "LimitViolation", "InterlockFailure", "EmergencyStopActive",
    "ConfirmationRequired", "PhysicalError", "WorkflowError", "ConformanceError",
]
