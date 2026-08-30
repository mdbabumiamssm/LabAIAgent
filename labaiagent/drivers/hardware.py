"""Real-hardware driver templates.

These are working reference implementations against the three transport
patterns that cover most of an installed lab. They are written to be copied
and edited, not subclassed cleverly -- when you integrate instrument N+1,
start from whichever of these matches its connectivity.

None of them are exercised against physical hardware here. Each is marked with
the specific things to verify on first connection, because the gap between
'the API docs say' and 'the instrument does' is where integration time
actually goes.
"""

from __future__ import annotations

import csv
import io
import re
import time
from pathlib import Path
from typing import Any

from ..core.capability import procedure, read, write
from ..core.device import Device
from ..core.errors import PhysicalError, TransientError, TransportError
from ..core.registry import register_driver
from ..core.types import OneOf, Param, Range, Risk
from ..transports.concrete import (
    FileWatchTransport,
    HTTPTransport,
    SerialTransport,
    TCPLineTransport,
)

# ==========================================================================
# Pattern A: HTTP/REST  -- Opentrons Flex
# ==========================================================================

@register_driver("opentrons.flex")
class OpentronsFlex(Device):
    """Opentrons Flex / OT-2 over the robot-server HTTP API (port 31950).

    Two control styles, both first-class:

    1. **Protocol runs** -- upload a Python/JSON protocol, create a run, play
       it, poll to completion (``proc:run_protocol``). Right for validated,
       repeated assays.
    2. **Live atomic commands** -- ``loadPipette`` / ``loadLabware`` /
       ``pickUpTip`` / ``aspirate`` / ``dispense`` / ``dropTip`` enqueued one
       at a time into a live run via ``POST /runs/{id}/commands`` (the same
       mechanism PyLabRobot's Opentrons backend uses). Right for agent-driven
       work, because THIS framework's safety engine then gates every single
       aspirate: volume limits, interlocks, rate limits, approval tokens and
       the audit trail apply per liquid movement, not per two-hour protocol.

    VERIFY ON FIRST CONNECT:
      - the ``Opentrons-Version`` header value your firmware expects; it is
        versioned and a mismatch produces confusing 422s rather than a clear error
      - whether your instance requires an auth token (older ones do not)
      - that the deck configuration in the app matches what your commands assume
      - labware definition namespace/version pairs against the robot's library
    """

    vendor = "Opentrons"
    model = "Flex"
    category = "liquid_handler"
    driver_version = "0.5.0"
    notes = """
    Live per-well pipetting IS available over HTTP: atomic commands are
    enqueued into a live run and polled to completion; each one is a separate
    safety-gated, audited action here. Call proc:load_pipette and
    proc:load_labware first -- aspirate/dispense address labware and pipettes
    by the IDs those return, not by names.
    For validated repeated assays prefer proc:run_protocol (upload once, run
    many); `play` is the irreversible step and a created run cannot be edited.
    Runs survive a client disconnect. On reconnect, poll `current_run` before
    starting anything, or you will queue a second run behind a live one.
    The gripper is an optional module; `has_gripper` reports whether one is
    actually attached rather than merely configured.
    wellLocation origin is the well BOTTOM; offsets are millimetres from it.
    """

    def __init__(self, device_id: str, *, host: str = "", port: int = 31950,
                 api_version: str = "4", token: str | None = None, **kw: Any) -> None:
        cfg = kw.get("config", {}) or {}
        self.host = host or cfg.get("host", "")
        self.port = int(cfg.get("port", port))
        headers = {"Opentrons-Version": str(cfg.get("api_version", api_version))}
        if token or cfg.get("token"):
            headers["Authorization"] = f"Bearer {token or cfg['token']}"
        self._http = HTTPTransport(f"http://{self.host}:{self.port}", headers=headers,
                                   timeout=float(cfg.get("timeout", 30.0)))
        self._run_id: str | None = None        # protocol run being executed
        self._live_run_id: str | None = None   # setup run for atomic commands
        super().__init__(device_id, **kw)

    def _connect(self) -> None:
        if not self.host:
            raise TransportError(
                f"{self.id}: no host configured. Set config.host to the Flex's "
                f"IP address (Settings -> Network on the robot's touchscreen).")
        self._http.open()

    def _disconnect(self) -> None:
        self._http.close()

    def _self_test(self) -> bool:
        health = self._http.request({"method": "GET", "path": "/health"})
        return bool(health and "api_version" in health)

    def _halt(self) -> None:
        for rid in (self._run_id, self._live_run_id):
            if rid:
                try:
                    self._http.request({"method": "POST",
                                        "path": f"/runs/{rid}/actions",
                                        "json": {"data": {"actionType": "stop"}}})
                except Exception:
                    pass
        # Both run handles are dead after an e-stop; a fresh live run is
        # created lazily once the operator brings the robot back.
        self._live_run_id = None

    # -- reads ------------------------------------------------------------

    @read("health", description="Robot firmware and API version report")
    def get_health(self) -> dict[str, Any]:
        return self._http.request({"method": "GET", "path": "/health"})

    @read("current_run", description="ID and status of the active run, if any")
    def get_current_run(self) -> dict[str, Any]:
        runs = self._http.request({"method": "GET", "path": "/runs"}) or {}
        data = runs.get("data", [])
        current = next((r for r in data if r.get("current")), None)
        if not current:
            return {"run_id": "", "status": "idle"}
        return {"run_id": current["id"], "status": current.get("status", "unknown")}

    @read("run_status", description="Status of a specific run",
          params=[Param("run_id", str, description="Run identifier")])
    def get_run_status(self, run_id: str) -> dict[str, Any]:
        r = self._http.request({"method": "GET", "path": f"/runs/{run_id}"})
        d = (r or {}).get("data", {})
        return {"run_id": run_id, "status": d.get("status"),
                "errors": d.get("errors", []),
                "completed_at": d.get("completedAt")}

    @read("attached_instruments", description="Pipettes and modules currently attached")
    def get_attached_instruments(self) -> dict[str, Any]:
        return self._http.request({"method": "GET", "path": "/instruments"}) or {}

    @read("has_gripper", description="Is a gripper physically attached?")
    def get_has_gripper(self) -> bool:
        inst = self.get_attached_instruments().get("data", [])
        return any(i.get("instrumentType") == "gripper" for i in inst)

    @read("lights", description="Are the deck lights on?")
    def get_lights(self) -> bool:
        return bool((self._http.request({"method": "GET", "path": "/robot/lights"})
                     or {}).get("on", False))

    # -- writes -----------------------------------------------------------

    @write("lights", risk=Risk.LOW, description="Turn the deck lights on or off",
           params=[Param("on", bool)])
    def set_lights(self, on: bool) -> bool:
        self._http.request({"method": "POST", "path": "/robot/lights",
                            "json": {"on": bool(on)}})
        return on

    # -- live atomic commands ----------------------------------------------
    #
    # Every method below enqueues ONE robot command into a live run and polls
    # it to a terminal state. A failed command surfaces as PhysicalError with
    # the robot's own errorType/detail, which routes into the incident
    # intelligence (quarantine + diagnosis) like any other physical fault.

    def _ensure_live_run(self) -> str:
        if self._live_run_id:
            return self._live_run_id
        cur = self.get_current_run()
        if cur["status"] in ("running", "paused", "blocked-by-open-door"):
            raise PhysicalError(
                f"{self.id}: run {cur['run_id']} is {cur['status']}; refusing "
                f"to open a live command session beside an active run.")
        created = self._http.request({"method": "POST", "path": "/runs",
                                      "json": {"data": {}}})
        self._live_run_id = created["data"]["id"]
        return self._live_run_id

    def _command(self, command_type: str, params: dict[str, Any], *,
                 timeout: float = 60.0, poll_s: float = 0.25) -> dict[str, Any]:
        try:
            run_id = self._ensure_live_run()
            resp = self._http.request({
                "method": "POST", "path": f"/runs/{run_id}/commands",
                "json": {"data": {"commandType": command_type, "params": params,
                                  "intent": "setup"}}})
        except TransportError:
            # The cached run may be gone (robot restart, run expired). Drop
            # the cache so the next attempt opens a fresh live run.
            self._live_run_id = None
            raise
        cid = resp["data"]["id"]
        # POINT OF NO RETURN. The command is enqueued on the robot: from here
        # NOTHING transient may escape, because the retry layer above
        # (LabSession.call retries TransientError) would re-enqueue the same
        # physical action -- a double aspirate (review finding R4-2). Poll
        # blips are absorbed here; a timeout is a PhysicalError, because the
        # command may still execute and the true state is unknown.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                got = self._http.request({
                    "method": "GET", "path": f"/runs/{run_id}/commands/{cid}"})
            except TransientError:
                time.sleep(poll_s)
                continue
            data = got["data"]
            status = data.get("status")
            if status == "succeeded":
                return data.get("result") or {}
            if status == "failed":
                err = data.get("error") or {}
                raise PhysicalError(
                    f"{self.id}: {command_type} failed "
                    f"[{err.get('errorType', 'unknown')}]: "
                    f"{err.get('detail', 'no detail from robot')}")
            time.sleep(poll_s)
        raise PhysicalError(
            f"{self.id}: {command_type} not settled after {timeout:g}s "
            f"(command {cid}, run {run_id}). It was ENQUEUED and may still "
            f"execute -- verify the robot's physical state before any retry; "
            f"do not re-send the command blindly.")

    @staticmethod
    def _well_location(offset_z: float = 0.0) -> dict[str, Any]:
        return {"origin": "bottom",
                "offset": {"x": 0.0, "y": 0.0, "z": offset_z}}

    @procedure("load_pipette", risk=Risk.LOW,
               description="Attach a pipette definition to the live command "
                           "session; returns the pipette_id the liquid "
                           "commands address",
               params=[Param("pipette_name", str,
                             description="e.g. 'p1000_single_flex', 'p300_single_gen2'"),
                       Param("mount", str, limits=OneOf("left", "right"))],
               returns="pipette_id", est_duration_s=5.0)
    def load_pipette(self, pipette_name: str, mount: str) -> dict[str, Any]:
        result = self._command("loadPipette",
                               {"pipetteName": pipette_name, "mount": mount})
        return {"pipette_id": result.get("pipetteId", ""), "mount": mount,
                "pipette_name": pipette_name}

    @procedure("load_labware", risk=Risk.LOW,
               description="Declare labware in a deck slot for the live "
                           "session; returns the labware_id the liquid "
                           "commands address",
               params=[Param("load_name", str,
                             description="e.g. 'corning_96_wellplate_360ul_flat'"),
                       Param("slot", str, description="Deck slot, e.g. '1' or 'D1'"),
                       Param("namespace", str, default="opentrons"),
                       Param("version", int, default=1, limits=Range(1, 100))],
               returns="labware_id", est_duration_s=2.0)
    def load_labware(self, load_name: str, slot: str,
                     namespace: str = "opentrons", version: int = 1) -> dict[str, Any]:
        result = self._command("loadLabware", {
            "location": {"slotName": str(slot)}, "loadName": load_name,
            "namespace": namespace, "version": int(version)})
        return {"labware_id": result.get("labwareId", ""), "slot": str(slot),
                "load_name": load_name}

    @procedure("pick_up_tip", risk=Risk.MEDIUM, consumes=("tip",),
               description="Pick up a tip from a tip-rack well",
               params=[Param("labware_id", str), Param("well", str),
                       Param("pipette_id", str)],
               est_duration_s=8.0)
    def pick_up_tip(self, labware_id: str, well: str,
                    pipette_id: str) -> dict[str, Any]:
        self._command("pickUpTip", {
            "labwareId": labware_id, "wellName": well,
            "wellLocation": self._well_location(), "pipetteId": pipette_id})
        return {"picked_up": True, "well": well}

    @procedure("aspirate", risk=Risk.MEDIUM, consumes=("sample",),
               description="Aspirate liquid from one well (safety-gated per call)",
               params=[Param("labware_id", str), Param("well", str),
                       Param("volume", float, "uL", limits=Range(0.1, 1000.0, "uL")),
                       Param("pipette_id", str),
                       Param("flow_rate", float, "uL/s", default=150.0,
                             limits=Range(0.1, 500.0, "uL/s")),
                       Param("offset_z", float, "mm", default=1.0,
                             limits=Range(0.0, 50.0, "mm"))],
               est_duration_s=6.0)
    def aspirate(self, labware_id: str, well: str, volume: float,
                 pipette_id: str, flow_rate: float = 150.0,
                 offset_z: float = 1.0) -> dict[str, Any]:
        result = self._command("aspirate", {
            "labwareId": labware_id, "wellName": well,
            "wellLocation": self._well_location(offset_z),
            "volume": float(volume), "flowRate": float(flow_rate),
            "pipetteId": pipette_id})
        return {"aspirated_uL": result.get("volume", volume), "well": well}

    @procedure("dispense", risk=Risk.MEDIUM,
               description="Dispense liquid into one well (safety-gated per call)",
               params=[Param("labware_id", str), Param("well", str),
                       Param("volume", float, "uL", limits=Range(0.1, 1000.0, "uL")),
                       Param("pipette_id", str),
                       Param("flow_rate", float, "uL/s", default=150.0,
                             limits=Range(0.1, 500.0, "uL/s")),
                       Param("offset_z", float, "mm", default=1.0,
                             limits=Range(0.0, 50.0, "mm"))],
               est_duration_s=6.0)
    def dispense(self, labware_id: str, well: str, volume: float,
                 pipette_id: str, flow_rate: float = 150.0,
                 offset_z: float = 1.0) -> dict[str, Any]:
        result = self._command("dispense", {
            "labwareId": labware_id, "wellName": well,
            "wellLocation": self._well_location(offset_z),
            "volume": float(volume), "flowRate": float(flow_rate),
            "pipetteId": pipette_id})
        return {"dispensed_uL": result.get("volume", volume), "well": well}

    @procedure("drop_tip", risk=Risk.MEDIUM,
               description="Drop the current tip into a well (tip rack or trash)",
               params=[Param("labware_id", str), Param("well", str),
                       Param("pipette_id", str)],
               est_duration_s=8.0)
    def drop_tip(self, labware_id: str, well: str,
                 pipette_id: str) -> dict[str, Any]:
        self._command("dropTip", {
            "labwareId": labware_id, "wellName": well,
            "wellLocation": self._well_location(), "pipetteId": pipette_id})
        return {"dropped": True, "well": well}

    @procedure("home", risk=Risk.LOW,
               description="Home all axes of the gantry",
               est_duration_s=30.0)
    def home(self) -> dict[str, Any]:
        self._http.request({"method": "POST", "path": "/robot/home",
                            "json": {"target": "robot"}})
        return {"homed": True}

    # -- procedures -------------------------------------------------------

    @procedure("upload_protocol", risk=Risk.LOW,
               description="Upload a Python or JSON protocol file to the robot",
               params=[Param("path", str, description="Local path to the protocol file")],
               returns="protocol_id", est_duration_s=10.0)
    def upload_protocol(self, path: str) -> dict[str, Any]:
        # multipart upload; kept explicit rather than pulling in `requests`
        import mimetypes
        import urllib.request
        import uuid as _uuid

        p = Path(path)
        if not p.exists():
            raise TransportError(f"Protocol file not found: {p}")
        boundary = _uuid.uuid4().hex
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="{p.name}"\r\n'
            f"Content-Type: {mimetypes.guess_type(p.name)[0] or 'text/plain'}\r\n\r\n"
        ).encode() + p.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            f"http://{self.host}:{self.port}/protocols", data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     **{k: v for k, v in self._http.headers.items()
                        if k != "Content-Type"}})
        import json as _json
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = _json.loads(resp.read().decode())
        pid = data["data"]["id"]
        return {"protocol_id": pid, "filename": p.name}

    @procedure(
        "run_protocol",
        risk=Risk.HIGH,
        reversible=False,
        description="Create a run from an uploaded protocol and execute it to completion",
        params=[Param("protocol_id", str),
                Param("poll_s", float, "s", default=5.0, limits=Range(1.0, 60.0, "s")),
                Param("timeout", float, "s", default=7200.0,
                      limits=Range(10.0, 86400.0, "s"))],
        interlocks=("no_device_in_error",),
        consumes=("tip", "sample", "reagent"),
        est_duration_s=1800.0,
    )
    def run_protocol(self, protocol_id: str, poll_s: float = 5.0,
                     timeout: float = 7200.0) -> dict[str, Any]:
        cur = self.get_current_run()
        if cur["status"] not in ("idle", "succeeded", "failed", "stopped"):
            raise PhysicalError(
                f"{self.id}: run {cur['run_id']} is already {cur['status']}. "
                f"Refusing to queue behind a live run.")

        created = self._http.request({
            "method": "POST", "path": "/runs",
            "json": {"data": {"protocolId": protocol_id}}})
        run_id = created["data"]["id"]
        self._run_id = run_id
        self._http.request({"method": "POST", "path": f"/runs/{run_id}/actions",
                            "json": {"data": {"actionType": "play"}}})

        deadline = time.monotonic() + timeout
        terminal = {"succeeded", "failed", "stopped"}
        status = "running"
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            try:
                st = self.get_run_status(run_id)
            except TransientError:
                continue          # poll blip; the run is already playing
            status = st.get("status") or "unknown"
            if status in terminal:
                self._run_id = None
                if status != "succeeded":
                    raise PhysicalError(
                        f"{self.id}: run {run_id} ended {status}: {st.get('errors')}")
                return {"run_id": run_id, "status": status,
                        "completed_at": st.get("completed_at")}
        # NOT TransientError: the run was already started ('play' is the
        # irreversible step); a retry would try to queue a second run.
        raise TransportError(
            f"{self.id}: run {run_id} still {status} after {timeout:g}s. "
            f"It is still executing on the robot -- poll read:run_status; "
            f"do not resubmit.")


# ==========================================================================
# Pattern B: serial / TCP line protocol -- SCPI-style instruments
# ==========================================================================

@register_driver("generic.scpi")
class GenericSCPIInstrument(Device):
    """Any instrument speaking SCPI over RS-232 or a raw TCP socket.

    Covers a large fraction of pumps, balances, temperature controllers,
    power supplies, laser drivers and older detectors. Point it at a port,
    declare the commands in config, and it is integrated.

    Command mapping lives in config so a new instrument of this class needs no
    new Python at all -- see ``config/example_lab.yaml`` for the shape.

    VERIFY ON FIRST CONNECT:
      - the line terminator (CR, LF, or CRLF -- vendors disagree and a wrong
        one looks exactly like a dead instrument)
      - whether the device echoes commands back before replying
      - the settle time after DTR assertion on USB-serial adapters
    """

    vendor = "generic"
    model = "SCPI"
    category = "generic"
    driver_version = "0.4.0"
    notes = """
    Generic SCPI bridge. Query commands must return a single line.
    Set `config.commands` to map capability names onto SCPI strings, e.g.
      commands:
        temperature:      "MEAS:TEMP?"
        set_temperature:  "SOUR:TEMP {value:.2f}"
    Values are formatted with str.format, so {value} is available and standard
    format specs work.
    If the instrument echoes, set config.echo: true or every reply will be
    off by one.
    """

    def __init__(self, device_id: str, **kw: Any) -> None:
        cfg = kw.get("config", {}) or {}
        self.commands: dict[str, str] = cfg.get("commands", {})
        self.echo = bool(cfg.get("echo", False))
        scheme = cfg.get("scheme", "serial")
        self._link: SerialTransport | TCPLineTransport
        if scheme == "serial":
            self._link = SerialTransport(
                cfg.get("port", "/dev/ttyUSB0"),
                baudrate=int(cfg.get("baudrate", 9600)),
                timeout=float(cfg.get("timeout", 2.0)),
                terminator=cfg.get("terminator", "\r\n"))
        else:
            self._link = TCPLineTransport(
                cfg.get("host", "127.0.0.1"), int(cfg.get("port", 5025)),
                timeout=float(cfg.get("timeout", 5.0)),
                terminator=cfg.get("terminator", "\n"))
        super().__init__(device_id, **kw)

    def _connect(self) -> None:
        self._link.open()

    def _disconnect(self) -> None:
        self._link.close()

    def _self_test(self) -> bool:
        try:
            idn = self._query("*IDN?")
            return bool(idn)
        except Exception:
            return False

    def _halt(self) -> None:
        stop = self.commands.get("halt")
        if stop:
            try:
                self._link.request(stop)
            except Exception:
                pass

    def _query(self, cmd: str) -> str:
        reply = self._link.request(cmd)
        if self.echo and reply.strip() == cmd.strip():
            reply = self._link.request("")   # consume the echo, read the real reply
        return reply.strip()

    @read("identity", description="SCPI *IDN? response")
    def get_identity(self) -> str:
        return self._query("*IDN?")

    @read("error_queue",
          description="Drain the SCPI error queue (SYST:ERR? until '0, No "
                      "error'). Instruments accumulate errors silently; an "
                      "undrained queue makes the NEXT query return stale junk.")
    def get_error_queue(self) -> list[str]:
        errors: list[str] = []
        for _ in range(32):   # bounded: a chattering instrument must not hang us
            reply = self._query("SYST:ERR?")
            if not reply or reply.split(",")[0].strip().lstrip("+-") == "0":
                break
            errors.append(reply)
        return errors

    @read("value", unit="", description="Read the primary measured value")
    def get_value(self) -> float:
        cmd = self.commands.get("value") or self.commands.get("measure")
        if not cmd:
            raise TransportError(
                f"{self.id}: no 'value' command configured. Add it under "
                f"config.commands.")
        raw = self._query(cmd)
        m = re.search(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", raw)
        if not m:
            raise TransportError(f"{self.id}: cannot parse a number from {raw!r}")
        return float(m.group())

    @read("raw", description="Send an arbitrary query and return the raw reply",
          params=[Param("command", str, description="SCPI query string")])
    def get_raw(self, command: str) -> str:
        if "?" not in command:
            raise ValueError(
                "read:raw is for queries only; a command without '?' would "
                "actuate. Use write:raw and accept the risk classification.")
        return self._query(command)

    @write("value", risk=Risk.MEDIUM, description="Write the primary setpoint",
           params=[Param("value", float, description="Setpoint value")])
    def set_value(self, value: float) -> float:
        tmpl = self.commands.get("set_value")
        if not tmpl:
            raise TransportError(f"{self.id}: no 'set_value' command configured.")
        # Verified write: with config.verify_writes (default on), the error
        # queue is drained BEFORE the setpoint (stale errors from earlier
        # activity are discarded, not blamed on this write -- review finding
        # R4-9) and again AFTER it, so any error raised here is attributable
        # to THIS setpoint. 'The instrument silently rejected the setpoint'
        # is the classic SCPI failure mode -- it becomes a PhysicalError here
        # instead of a wrong experiment later.
        verify = self.config.get("verify_writes", True)
        if verify:
            self.get_error_queue()          # drain stale errors, discard
        self._link.request(tmpl.format(value=value))
        if verify:
            errors = self.get_error_queue()
            if errors:
                raise PhysicalError(
                    f"{self.id}: instrument rejected the setpoint: "
                    f"{'; '.join(errors[:3])}")
        return value

    @write("raw", risk=Risk.HIGH, reversible=False, requires_confirmation=True,
           description="Send an arbitrary command. Unvalidated -- human approval required.",
           params=[Param("command", str, description="Raw SCPI command")])
    def set_raw(self, command: str) -> str:
        return self._link.request(command)


# ==========================================================================
# Pattern C: watched folder -- the instrument with no API at all
# ==========================================================================

@register_driver("generic.filewatch_reader")
class FileWatchPlateReader(Device):
    """A plate reader whose only integration surface is an export folder.

    This is the most common awkward case in an installed lab, and the one that
    usually gets written off as 'not automatable'. It is automatable: the
    vendor software drops a CSV, this driver parses it, and downstream code
    cannot tell the difference from a REST instrument.

    The operator still presses Start on the vendor GUI unless the software
    supports a watched command folder. That is an honest limitation, not a
    failure -- a semi-automated read that lands structured, audited data in
    your pipeline is worth far more than a fully manual one.

    VERIFY ON FIRST CONNECT:
      - the exact export path and file pattern in the vendor software
      - whether it writes one file per read or appends to a running file
      - the CSV dialect: many exporters emit a preamble of metadata lines
        before the actual grid, and some use semicolons
    """

    vendor = "generic"
    model = "FileWatchReader"
    category = "plate_reader"
    driver_version = "0.4.0"
    notes = """
    Reads are triggered by the vendor software, not by this driver, unless a
    command_dir is configured.
    `read_absorbance` blocks until a NEW file appears in the watch directory,
    so start it BEFORE pressing Start on the instrument, or set a long timeout.
    Files are archived on ingest if config.archive_dir is set -- do that; the
    vendor software will happily overwrite its own exports.
    """

    def __init__(self, device_id: str, **kw: Any) -> None:
        cfg = kw.get("config", {}) or {}
        self._fw = FileWatchTransport(
            watch_dir=cfg.get("watch_dir", "."),
            command_dir=cfg.get("command_dir"),
            pattern=cfg.get("pattern", "*.csv"),
            timeout=float(cfg.get("timeout", 600.0)),
            archive_dir=cfg.get("archive_dir"),
        )
        self.skip_rows = int(cfg.get("skip_rows", 0))
        self.delimiter = cfg.get("delimiter", ",")
        self._last: dict[str, Any] | None = None
        super().__init__(device_id, **kw)

    def _connect(self) -> None:
        self._fw.open()

    def _disconnect(self) -> None:
        self._fw.close()

    @read("watch_dir", description="Directory monitored for new export files")
    def get_watch_dir(self) -> str:
        return str(self._fw.watch_dir)

    @read("pending_files", unit="count",
          description="New files seen in the watch directory since connect")
    def get_pending_files(self) -> int:
        return len([p for p in self._fw.watch_dir.glob(self._fw.pattern)
                    if p.name not in self._fw._seen])

    @read("last_result", description="Parsed data from the most recent ingest")
    def get_last_result(self) -> dict[str, Any]:
        return self._last or {}

    @procedure(
        "read_absorbance",
        risk=Risk.LOW,
        description="Wait for the next export file and parse it into a well grid",
        params=[Param("wavelength", float, "nm", default=562.0,
                      limits=Range(200.0, 1000.0, "nm")),
                Param("timeout", float, "s", default=600.0,
                      limits=Range(5.0, 7200.0, "s"))],
        returns="dict of well -> value",
        est_duration_s=120.0,
    )
    def read_absorbance(self, wavelength: float = 562.0,
                        timeout: float = 600.0) -> dict[str, Any]:
        path = self._fw.await_result(timeout=timeout)
        data = self._parse_grid(path)
        self._last = {"source_file": str(path), "wavelength_nm": wavelength,
                      "n_wells": len(data), "data": data}
        return self._last

    def _parse_grid(self, path: Path) -> dict[str, float]:
        """Parse a rectangular plate export into {well: value}.

        Tolerates a metadata preamble by locating the first row whose leading
        cells look like column indices -- which is how essentially every
        vendor exporter lays out a plate grid regardless of what precedes it.
        """
        text = path.read_text(encoding="utf-8", errors="replace")
        rows = list(csv.reader(io.StringIO(text), delimiter=self.delimiter))
        rows = rows[self.skip_rows:]

        header_idx = None
        for i, row in enumerate(rows):
            cells = [c.strip() for c in row if c.strip()]
            if len(cells) >= 3 and all(c.isdigit() for c in cells[:3]):
                header_idx = i
                break
        if header_idx is None:
            raise PhysicalError(
                f"{self.id}: could not find a plate grid in {path.name}. "
                f"Check config.skip_rows and config.delimiter against the "
                f"actual export format.")

        cols = [c.strip() for c in rows[header_idx] if c.strip()]
        out: dict[str, float] = {}
        for row in rows[header_idx + 1:]:
            if not row or not row[0].strip():
                continue
            label = row[0].strip().upper()
            if not re.fullmatch(r"[A-P]", label):
                break
            for j, cell in enumerate(row[1:len(cols) + 1]):
                cell = cell.strip()
                if not cell:
                    continue
                try:
                    out[f"{label}{cols[j]}"] = float(cell)
                except ValueError:
                    continue
        if not out:
            raise PhysicalError(f"{self.id}: parsed zero wells from {path.name}")
        return out


__all__ = ["OpentronsFlex", "GenericSCPIInstrument", "FileWatchPlateReader"]
