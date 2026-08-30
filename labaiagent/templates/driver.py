"""Driver scaffolding.

``labaiagent scaffold`` renders these. The generated file is deliberately opinionated
and heavily commented: the point is that someone integrating their eleventh
instrument should not have to remember what a good driver looks like, and the
inline notes should tell them the specific things that go wrong with the
transport they picked.
"""

from __future__ import annotations

TRANSPORT_SETUP: dict[str, dict[str, str]] = {
    "serial": {
        "import": "from labaiagent.transports import SerialTransport",
        "init": '''        self._link = SerialTransport(
            cfg.get("port", "/dev/ttyUSB0"),
            baudrate=int(cfg.get("baudrate", 9600)),
            timeout=float(cfg.get("timeout", 2.0)),
            terminator=cfg.get("terminator", "\\r\\n"),
        )''',
        "connect": "        self._link.open()",
        "disconnect": "        self._link.close()",
        "selftest": '''        reply = self._link.request("*IDN?")
        return bool(reply)''',
        "notes": """    Serial link. Things that bite, in order of frequency:
      - wrong line terminator (CR vs LF vs CRLF) looks identical to a dead device
      - the instrument echoes the command before replying, so every read is
        off by one
      - USB-serial adapters need ~100 ms after DTR assertion before the first
        command lands""",
    },
    "tcp": {
        "import": "from labaiagent.transports import TCPLineTransport",
        "init": '''        self._link = TCPLineTransport(
            cfg.get("host", "127.0.0.1"),
            int(cfg.get("port", 5025)),
            timeout=float(cfg.get("timeout", 5.0)),
            terminator=cfg.get("terminator", "\\n"),
        )''',
        "connect": "        self._link.open()",
        "disconnect": "        self._link.close()",
        "selftest": '''        return bool(self._link.request("*IDN?"))''',
        "notes": """    TCP line protocol. Most controllers accept exactly ONE client at a time --
    if the vendor GUI is open, this will fail to connect or, worse, connect and
    silently interleave commands with it.""",
    },
    "http": {
        "import": "from labaiagent.transports import HTTPTransport",
        "init": '''        self._http = HTTPTransport(
            f"http://{cfg.get('host', '127.0.0.1')}:{cfg.get('port', 80)}",
            timeout=float(cfg.get("timeout", 30.0)),
            headers=cfg.get("headers", {}),
        )''',
        "connect": "        self._http.open()",
        "disconnect": "        self._http.close()",
        "selftest": '''        health = self._http.request({"method": "GET", "path": "/health"})
        return bool(health)''',
        "notes": """    HTTP/REST. Check whether the vendor versions its API through a header
    (several do) -- a mismatch produces 422s that read like malformed input
    rather than a version problem.""",
    },
    "filewatch": {
        "import": "from labaiagent.transports import FileWatchTransport",
        "init": '''        self._fw = FileWatchTransport(
            watch_dir=cfg.get("watch_dir", "."),
            command_dir=cfg.get("command_dir"),
            pattern=cfg.get("pattern", "*.csv"),
            timeout=float(cfg.get("timeout", 600.0)),
            archive_dir=cfg.get("archive_dir"),
        )''',
        "connect": "        self._fw.open()",
        "disconnect": "        self._fw.close()",
        "selftest": "        return self._fw.watch_dir.exists()",
        "notes": """    Watched export folder -- the instrument has no API.
    Start the wait BEFORE triggering the run, or the file may appear and be
    missed. Always set archive_dir: vendor software cheerfully overwrites its
    own exports.
    Most exporters emit a metadata preamble before the plate grid; parse by
    locating the header row rather than by a fixed skip count.""",
    },
    "com": {
        "import": "from labaiagent.transports import COMTransport",
        "init": '''        self._com = COMTransport(cfg.get("prog_id", "Vendor.Application"),
                                 timeout=float(cfg.get("timeout", 60.0)))''',
        "connect": "        self._com.open()",
        "disconnect": "        self._com.close()",
        "selftest": '''        return bool(self._com.call(lambda o: o.Version))''',
        "notes": """    Windows COM/ActiveX. COM is apartment-threaded; COMTransport pins every
    call to one dedicated thread for you. Do not hold the returned COM objects
    across calls -- fetch what you need inside the lambda and return plain
    Python values.""",
    },
    "sila2": {
        "import": "from labaiagent.transports import SiLA2Transport",
        "init": '''        self._sila = SiLA2Transport(cfg.get("host", "127.0.0.1"),
                                    int(cfg.get("port", 50051)),
                                    insecure=bool(cfg.get("insecure", True)))''',
        "connect": "        self._sila.open()",
        "disconnect": "        self._sila.close()",
        "selftest": "        return self._sila.client is not None",
        "notes": """    SiLA 2. Where the vendor already ships a SiLA server, use it -- this driver
    wraps it so the instrument joins the same manifest, safety and audit fabric
    as everything else, without replacing what already works.""",
    },
    "subprocess": {
        "import": "from labaiagent.transports import SubprocessTransport",
        "init": '''        self._cli = SubprocessTransport(cfg.get("executable", "vendor-cli"),
                                        cwd=cfg.get("cwd"),
                                        timeout=float(cfg.get("timeout", 300.0)))''',
        "connect": "        self._cli.open()",
        "disconnect": "        self._cli.close()",
        "selftest": '''        return bool(self._cli.request(["--version"]))''',
        "notes": """    Vendor CLI wrapper. Also the right choice for an SDK pinned to a different
    Python version -- run it in its own interpreter rather than fighting the
    dependency conflict.""",
    },
    "sdk": {
        "import": "# import your vendor SDK here",
        "init": '''        self._sdk = None  # constructed in _connect so import errors surface there
        self._cfg = cfg''',
        "connect": '''        # import inside _connect so a missing SDK produces an actionable
        # connect-time error rather than an import error at startup
        # import vendor_sdk
        # self._sdk = vendor_sdk.connect(**self._cfg)
        raise NotImplementedError("wire up the vendor SDK here")''',
        "disconnect": '''        if self._sdk is not None:
            # self._sdk.close()
            self._sdk = None''',
        "selftest": "        return self._sdk is not None",
        "notes": """    Native vendor SDK. Keep the SDK import inside _connect so a missing or
    mis-versioned package fails loudly at connect time with a message naming
    the package, rather than at process start.""",
    },
}


DRIVER_TEMPLATE = '''"""{class_name} -- LabAIAgent driver for the {vendor} {model}.

Generated by `labaiagent scaffold {key}`.

INTEGRATION CHECKLIST
  [ ] Fill in _connect / _disconnect / _self_test / _halt
  [ ] Replace the example capabilities below with the instrument's real ones
  [ ] Give every numeric parameter a unit and a Range -- an unbounded numeric
      input to a physical device is how a stage gets driven into a hard stop
  [ ] Write `notes` -- this is what an agent reads to learn the instrument's
      quirks, and it is the only place tacit bench knowledge gets captured
  [ ] Override _halt() if anything on this instrument moves
  [ ] labaiagent verify {module_hint}:{class_name}
"""

from __future__ import annotations

from typing import Any

from labaiagent import Device, Param, Range, OneOf, Risk, read, write, procedure
from labaiagent.core.errors import PhysicalError, TransientError, TransportError
from labaiagent.core.registry import register_driver
{transport_import}


@register_driver("{key}")
class {class_name}(Device):
    """{vendor} {model} ({category})."""

    vendor = "{vendor}"
    model = "{model}"
    category = "{category}"
    driver_version = "0.1.0"

    #: Operating notes. An agent reads this before touching the instrument.
    #: Write down what you would tell a new postdoc on their first day.
    notes = """
{notes}
    """

    def __init__(self, device_id: str, **kw: Any) -> None:
        cfg = kw.get("config", {{}}) or {{}}
{transport_init}
        super().__init__(device_id, **kw)

    # -- lifecycle --------------------------------------------------------

    def _connect(self) -> None:
{connect_body}

    def _disconnect(self) -> None:
{disconnect_body}

    def _self_test(self) -> bool:
        """Cheap identity/liveness check run immediately after connect."""
{selftest_body}

    def _halt(self) -> None:
        """Best-effort immediate stop. Called on emergency stop. Must not raise.

        If this instrument moves, heats, or dispenses, implement this. The
        conformance suite treats an unimplemented _halt on a moving instrument
        as an error, because it makes the lab-wide e-stop a no-op here.
        """
        # try:
        #     self._link.request("ABORT")
        # except Exception:
        #     pass

    # -- reads ------------------------------------------------------------

    @read("status", description="Instrument status word")
    def get_status(self) -> str:
        raise NotImplementedError

    @read("temperature", unit="degC", description="Current temperature")
    def get_temperature(self) -> float:
        raise NotImplementedError

    # -- writes -----------------------------------------------------------
    #
    # Name a write the same as its matching read when they address the same
    # physical state variable. LabAIAgent keys them separately (read:temperature
    # vs write:temperature), and the conformance suite warns about any
    # writable setpoint with no readback -- an agent that cannot confirm a
    # setpoint took effect silently loses track of the instrument.

    @write("temperature", risk=Risk.MEDIUM, unit="degC",
           description="Set the temperature setpoint",
           params=[Param("value", float, "degC", "Target temperature",
                         limits=Range(4.0, 100.0, "degC"))],
           est_duration_s=60.0)
    def set_temperature(self, value: float) -> float:
        raise NotImplementedError

    # -- procedures -------------------------------------------------------

    @procedure(
        "run",
        risk=Risk.HIGH,
        reversible=False,
        description="Execute the instrument's primary operation",
        params=[Param("duration", float, "s", "Run duration",
                      limits=Range(1.0, 3600.0, "s"))],
        consumes=("sample",),
        est_duration_s=300.0,
        # Declare any physical precondition that must hold. Register the
        # interlock on the session with @session.define_interlock(...).
        # interlocks=("lid_closed",),
    )
    def run(self, duration: float) -> dict[str, Any]:
        raise NotImplementedError
'''


TEST_TEMPLATE = '''"""Conformance test for {class_name}.

Run with: pytest {test_file}

The conformance suite is the integration gate. A driver that passes at
`strict` is safe to add to a lab config; one that does not is not.
"""

from __future__ import annotations

import pytest

from labaiagent.core.conformance import verify_driver
from {module} import {class_name}


@pytest.fixture
def device():
    # Adjust config for your instrument. Point at a simulator or a bench unit.
    return {class_name}("test_{key_id}", config={{}})


def test_conformance_static(device):
    """Contract checks that need no hardware."""
    report = verify_driver(device, exercise=False)
    assert report.passed, report.render()


def test_conformance_strict(device):
    """Full check including warnings. Aim to keep this passing."""
    report = verify_driver(device, exercise=False)
    assert report.passed_at("strict"), report.render()


@pytest.mark.hardware
def test_conformance_live(device):
    """Connects to real hardware. Run with: pytest -m hardware"""
    report = verify_driver(device, exercise=True)
    assert report.passed, report.render()


def test_manifest_is_serialisable(device):
    import json
    json.dumps(device.manifest())


def test_reference_sheet_mentions_every_capability(device):
    sheet = device.reference_sheet()
    for cap in device.capabilities.values():
        assert cap.name in sheet
'''


def render_driver_template(*, key: str, class_name: str, vendor: str, model: str,
                           category: str, transport: str) -> str:
    t = TRANSPORT_SETUP.get(transport, TRANSPORT_SETUP["serial"])
    return DRIVER_TEMPLATE.format(
        key=key, class_name=class_name, vendor=vendor, model=model,
        category=category,
        module_hint=key.replace(".", "_") + ".py",
        transport_import=t["import"],
        transport_init=t["init"],
        connect_body=t["connect"],
        disconnect_body=t["disconnect"],
        selftest_body=t["selftest"],
        notes=t["notes"],
    )


def render_test_template(*, key: str, class_name: str, module: str) -> str:
    return TEST_TEMPLATE.format(
        class_name=class_name, module=module,
        key_id=key.replace(".", "_"),
        test_file=f"test_{key.replace('.', '_')}.py",
    )


__all__ = ["render_driver_template", "render_test_template", "TRANSPORT_SETUP"]
