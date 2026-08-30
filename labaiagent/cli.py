"""Command-line interface.

    labaiagent drivers                        list every registered driver
    labaiagent scaffold acme.pump --category pump --transport serial
    labaiagent verify path/to/driver.py:AcmePump
    labaiagent describe --lab config/lab.yaml reader
    labaiagent doctor --lab config/lab.yaml   connectivity + policy audit
    labaiagent serve --lab config/lab.yaml    start the MCP server
    labaiagent run protocol.json --lab config/lab.yaml --dry-run
    labaiagent audit verify runs/audit.jsonl

``scaffold`` is the entry point for integrating instrument N+1: it writes a
driver skeleton wired to the transport you name, with the capability
decorators, limits and conformance test already in place. Fill in the three
lifecycle hooks and the command strings, run ``labaiagent verify``, and it is
part of the lab.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from .core.conformance import verify_driver
from .core.device import Device
from .core.registry import get_driver, list_drivers
from .templates.driver import render_driver_template, render_test_template


def _load_session(lab: str | None, *, dry_run: bool = False, actor: str = ""):
    from .orchestration.session import LabSession
    if not lab:
        raise SystemExit("This command needs --lab pointing at a lab YAML file.")
    return LabSession.from_config(lab, dry_run=dry_run, actor=actor)


def _load_class_from_spec(spec: str) -> type[Device]:
    """Resolve 'path/to/mod.py:ClassName' or 'package.module:ClassName' or a
    registered driver key."""
    if ":" in spec:
        modpart, _, clsname = spec.partition(":")
        if modpart.endswith(".py"):
            path = Path(modpart).resolve()
            if not path.exists():
                raise SystemExit(f"No such file: {path}")
            mspec = importlib.util.spec_from_file_location(path.stem, path)
            assert mspec and mspec.loader
            mod = importlib.util.module_from_spec(mspec)
            sys.modules[path.stem] = mod
            mspec.loader.exec_module(mod)
        else:
            mod = importlib.import_module(modpart)
        try:
            return getattr(mod, clsname)
        except AttributeError:
            raise SystemExit(f"{modpart} has no class {clsname!r}") from None
    return get_driver(spec)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_drivers(args: argparse.Namespace) -> int:
    drivers = list_drivers()
    if args.json:
        print(json.dumps(drivers, indent=2))
        return 0
    print(f"{len(drivers)} registered driver(s)\n")
    print(f"{'KEY':<30} {'VENDOR':<14} {'MODEL':<18} {'CATEGORY':<16} VERSION")
    print("-" * 92)
    for key, d in drivers.items():
        print(f"{key:<30} {d['vendor']:<14} {d['model']:<18} "
              f"{d['category']:<16} {d['version']}")
    return 0


def cmd_scaffold(args: argparse.Namespace) -> int:
    key = args.key
    if "." not in key:
        raise SystemExit("Driver key should be dotted, e.g. 'acme.pump'.")
    vendor, _, model = key.partition(".")
    class_name = "".join(p.capitalize() for p in key.replace(".", "_").split("_"))

    out_dir = Path(args.out or ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    driver_path = out_dir / f"{key.replace('.', '_')}.py"
    test_path = out_dir / f"test_{key.replace('.', '_')}.py"

    if driver_path.exists() and not args.force:
        raise SystemExit(f"{driver_path} already exists (use --force to overwrite).")

    driver_path.write_text(render_driver_template(
        key=key, class_name=class_name, vendor=vendor.capitalize(),
        model=model.upper(), category=args.category, transport=args.transport,
    ), encoding="utf-8")
    test_path.write_text(render_test_template(
        key=key, class_name=class_name,
        module=driver_path.stem), encoding="utf-8")

    print(f"Wrote {driver_path}")
    print(f"Wrote {test_path}")
    print()
    print("Next steps:")
    print("  1. Fill in _connect / _disconnect / _self_test / _halt")
    print("  2. Replace the example capabilities with the instrument's real ones")
    print(f"  3. labaiagent verify {driver_path}:{class_name}")
    print("  4. Add a stanza to your lab YAML:")
    print(f"       - id: my_{model}")
    print(f"         driver: {key}")
    print("         config: {...}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    cls = _load_class_from_spec(args.driver)
    cfg = json.loads(args.config) if args.config else {}
    try:
        dev = cls(args.id, config=cfg)
    except Exception as exc:
        print(f"Could not instantiate {cls.__name__}: {exc}")
        return 2
    rep = verify_driver(dev, exercise=not args.static, level=args.level)
    print(rep.render())
    if args.json:
        print(json.dumps([f.__dict__ for f in rep.findings], indent=2))
    return 0 if rep.passed_at(args.level) else 1


def cmd_describe(args: argparse.Namespace) -> int:
    session = _load_session(args.lab)
    session.connect_all()
    try:
        if args.device:
            dev = session.get(args.device)
            print(json.dumps(dev.manifest(), indent=2) if args.json
                  else dev.reference_sheet())
        else:
            print(json.dumps(session.manifest(), indent=2) if args.json
                  else session.reference_sheets())
    finally:
        session.disconnect_all()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Connectivity, conformance and policy audit of a whole lab."""
    session = _load_session(args.lab)
    print(f"LAB: {session.name}\n")

    print("CONNECTIVITY")
    results = session.connect_all()
    width = max((len(k) for k in results), default=10)
    failures = 0
    for did, status in results.items():
        mark = "  ok  " if status == "ok" else " FAIL "
        if status != "ok":
            failures += 1
        print(f"  [{mark}] {did:<{width}}  {status}")

    print("\nCONFORMANCE")
    conf_errors = conf_warns = 0
    for dev in session.devices():
        rep = verify_driver(dev, exercise=dev.connected)
        conf_errors += len(rep.errors)
        conf_warns += len(rep.warnings)
        mark = "  ok  " if rep.passed else " FAIL "
        print(f"  [{mark}] {dev.id:<{width}}  {len(rep.errors)} error(s), "
              f"{len(rep.warnings)} warning(s)")
        for f in rep.errors:
            print(f"           ERROR {f.check} {f.capability}: {f.message[:90]}")
        if args.verbose:
            for f in rep.warnings:
                print(f"           warn  {f.check} {f.capability}: {f.message[:90]}")

    print("\nPOLICY")
    print(f"  autonomy ceiling : {session.safety.autonomy_ceiling.value}")
    print(f"  emergency stop   : "
          f"{'LATCHED -- ' + session.safety.estop.reason if session.safety.estop.active else 'clear'}")
    print(f"  interlocks       : {len(session.safety.interlocks.names())} registered")

    print("\nHIGH-RISK CAPABILITIES (require a human approval token)")
    ceiling = session.safety.autonomy_ceiling
    any_risky = False
    for dev in session.devices():
        for key, cap in dev.capabilities.items():
            if cap.risk.rank > ceiling.rank or cap.requires_confirmation:
                any_risky = True
                print(f"  {dev.id}.{key:<28} risk={cap.risk.value}"
                      + ("  IRREVERSIBLE" if not cap.reversible else ""))
    if not any_risky:
        print("  (none)")

    session.disconnect_all()
    print(f"\nSUMMARY: {failures} unreachable device(s), {conf_errors} conformance "
          f"error(s), {conf_warns} warning(s)")
    return 1 if (failures or conf_errors) else 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .mcp.server import build_fastmcp_server, serve_stdio
    session = _load_session(args.lab, dry_run=args.dry_run, actor=args.actor)
    session.connect_all()
    print(f"LabAIAgent server: {len(session.devices())} device(s), "
          f"readonly={args.readonly}, dry_run={args.dry_run}",
          file=sys.stderr)
    auth = None
    if args.auth:
        from .gateway.auth import Authenticator
        auth = Authenticator.from_config(args.auth)
        print(f"  auth: {len(auth.principals())} principal(s) loaded",
              file=sys.stderr)
    # Oversight configuration from the lab YAML (policy.oversight): reviewer
    # ('rules' | 'anthropic' | 'openai'), refusal-streak thresholds, feedback
    # dataset path. Networked servers get a Supervisor even without config.
    supervisor = None
    try:
        import yaml as _yaml
        _cfg = _yaml.safe_load(Path(args.lab).read_text(encoding="utf-8")) or {}
        _ov = (_cfg.get("policy") or {}).get("oversight")
    except Exception:
        _ov = None
    ctx = None
    memory = None
    if session.audit.path:
        from .gateway.registry import GatewayContext
        from .memory.store import LabMemory
        ctx = GatewayContext.for_session(session)
        memory = LabMemory(session.audit.path.parent / "labaiagent_state.db")
        ctx.memory = memory
        memory.log_startup(session.audit.session_id)
        report = memory.startup_report()
        interrupted = report["interrupted_tasks"]
        print(f"  memory: {len(interrupted)} interrupted, "
              f"{len(report['pending_tasks'])} pending task(s), "
              f"{len(report['open_incidents'])} open incident(s), "
              f"{len(report['quarantined_devices'])} quarantined device(s)",
              file=sys.stderr)
        for t in interrupted:
            print(f"    RESUME: task {t['id']} \"{t['title']}\""
                  + (" [checkpoint on disk]" if t["has_checkpoint"] else ""),
                  file=sys.stderr)
    if _ov or args.http:
        from .gateway.registry import GatewayContext
        from .oversight.supervisor import Supervisor
        fb = (str(session.audit.path.parent / "feedback.jsonl")
              if session.audit.path else None)
        supervisor = Supervisor.from_config(_ov, feedback_path=fb,
                                            memory=memory)
        GatewayContext.for_session(session).supervisor = supervisor
        print(f"  oversight: reviewer={supervisor.reviewer.name}, "
              f"suspend after {supervisor.max_refusals} refusals/"
              f"{supervisor.window_s:g}s"
              + (", state persistent" if memory else ""), file=sys.stderr)
    try:
        if args.http:
            # REST + OpenAPI + SSE events + MCP-over-HTTP, one port.
            from .gateway.rest import serve_http
            serve_http(session, host=args.host, port=args.port, auth=auth,
                       readonly=args.readonly,
                       tls_cert=args.tls_cert or None,
                       tls_key=args.tls_key or None,
                       supervisor=supervisor,
                       watchdog_interval_s=args.watchdog)
        elif args.sdk:
            server = build_fastmcp_server(session, readonly=args.readonly)
            server.run()
        else:
            serve_stdio(session, readonly=args.readonly)
    except KeyboardInterrupt:
        pass
    finally:
        session.disconnect_all()
    return 0


def cmd_hash_password(args: argparse.Namespace) -> int:
    """Generate a PBKDF2 e-signature entry for principals.yaml."""
    import getpass

    from .gateway.auth import hash_password
    pw = getpass.getpass("Password to hash (not echoed): ")
    confirm = getpass.getpass("Repeat: ")
    if pw != confirm:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    print("Add to the principal's stanza in principals.yaml:")
    print(f'    password_pbkdf2: "{hash_password(pw)}"')
    return 0


def cmd_schemas(args: argparse.Namespace) -> int:
    """Export the tool registry in any agent vendor's dialect. Needs no lab:
    the surface is fixed by design."""
    from .gateway import schemas as S
    fmt = args.format
    if fmt == "openai":
        out: Any = S.to_openai_tools(readonly_only=args.readonly)
    elif fmt == "anthropic":
        out = S.to_anthropic_tools(readonly_only=args.readonly)
    elif fmt == "gemini":
        out = S.to_gemini_tools(readonly_only=args.readonly)
    else:
        out = S.to_openapi(readonly_only=args.readonly)
    print(json.dumps(out, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from .orchestration.workflow import Protocol
    data = json.loads(Path(args.protocol).read_text(encoding="utf-8"))
    proto = Protocol.from_dict(data)
    session = _load_session(args.lab, dry_run=args.dry_run, actor=args.actor)
    session.connect_all()
    try:
        problems = proto.validate(session)
        if problems:
            print("VALIDATION FAILED:")
            for p in problems:
                print("  -", p)
            return 1
        print(f"Validation passed: {len(proto.steps)} step(s)")
        if args.validate_only:
            return 0
        proto.checkpoint_path = Path(args.checkpoint) if args.checkpoint else None
        if proto.checkpoint_path and args.resume:
            n = proto.resume_from_checkpoint()
            print(f"Resumed: {n} step(s) already complete")
        proto.run(session, validate=False,
                  progress=lambda s: print(f"  [{s.status.value:>7}] {s.name}"))
    except Exception as exc:
        print(f"\n{type(exc).__name__}: {exc}")
        print()
        print(proto.report())
        return 1
    finally:
        session.disconnect_all()
    print()
    print(proto.report())
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    from .core.audit import AuditLog
    log = AuditLog(args.path)
    if args.action == "verify":
        ok, msg = log.verify()
        print(f"{'VALID' if ok else 'INVALID'}: {msg}")
        return 0 if ok else 1
    if args.action == "summary":
        print(json.dumps(log.summary(), indent=2))
        return 0
    print(json.dumps(log.tail(args.limit), indent=2, default=str))
    return 0


# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="labaiagent",
        description="Connect, govern and audit laboratory instruments.")
    p.add_argument("--version", action="store_true")
    sub = p.add_subparsers(dest="command")

    d = sub.add_parser("drivers", help="List registered drivers")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_drivers)

    s = sub.add_parser("scaffold", help="Generate a driver skeleton for a new instrument")
    s.add_argument("key", help="Dotted driver key, e.g. acme.pump")
    s.add_argument("--category", default="generic",
                   help="pump | plate_reader | thermocycler | robot_arm | ...")
    s.add_argument("--transport", default="serial",
                   choices=["serial", "tcp", "http", "filewatch", "com", "sila2",
                            "subprocess", "sdk"])
    s.add_argument("--out", default=".", help="Output directory")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_scaffold)

    v = sub.add_parser("verify", help="Run the conformance suite against a driver")
    v.add_argument("driver", help="'file.py:ClassName', 'pkg.mod:ClassName', or a driver key")
    v.add_argument("--id", default="probe")
    v.add_argument("--config", help="JSON config dict for instantiation")
    v.add_argument("--static", action="store_true",
                   help="Skip checks that connect to the instrument")
    v.add_argument("--level", default="normal", choices=["normal", "strict"])
    v.add_argument("--json", action="store_true")
    v.set_defaults(func=cmd_verify)

    de = sub.add_parser("describe", help="Print the operating reference for a lab or device")
    de.add_argument("device", nargs="?")
    de.add_argument("--lab", required=True)
    de.add_argument("--json", action="store_true")
    de.set_defaults(func=cmd_describe)

    do = sub.add_parser("doctor", help="Connectivity, conformance and policy audit")
    do.add_argument("--lab", required=True)
    do.add_argument("--verbose", "-v", action="store_true")
    do.set_defaults(func=cmd_doctor)

    sv = sub.add_parser(
        "serve",
        help="Serve the lab to agents: MCP over stdio (default) or, with "
             "--http, REST + OpenAPI + SSE events + MCP-over-HTTP")
    sv.add_argument("--lab", required=True)
    sv.add_argument("--readonly", action="store_true",
                    help="Expose only non-actuating tools")
    sv.add_argument("--dry-run", action="store_true",
                    help="Validate and audit every call but never actuate")
    sv.add_argument("--sdk", action="store_true", help="Use the official MCP SDK")
    sv.add_argument("--http", action="store_true",
                    help="Serve HTTP (REST + MCP) instead of stdio")
    sv.add_argument("--host", default="127.0.0.1",
                    help="Bind address for --http (non-loopback requires --auth)")
    sv.add_argument("--port", type=int, default=8859)
    sv.add_argument("--auth", default="",
                    help="principals.yaml with API keys, roles and ceilings")
    sv.add_argument("--tls-cert", default="",
                    help="PEM certificate chain; enables HTTPS for --http")
    sv.add_argument("--tls-key", default="",
                    help="PEM private key for --tls-cert")
    sv.add_argument("--actor", default="agent:claude")
    sv.add_argument("--watchdog", type=float, default=0.0, metavar="SECONDS",
                    help="Heartbeat-watchdog polling interval; 0 disables. "
                         "Devices failing 3 consecutive self-tests are marked "
                         "ERROR (refusing new actuation) until they answer "
                         "again. HTTP mode only.")
    sv.set_defaults(func=cmd_serve)

    sc = sub.add_parser(
        "schemas",
        help="Export the fixed tool registry as OpenAI/Anthropic/Gemini tool "
             "schemas or an OpenAPI 3.1 document")
    sc.add_argument("--format", default="openapi",
                    choices=["openai", "anthropic", "gemini", "openapi"])
    sc.add_argument("--readonly", action="store_true",
                    help="Export only the non-actuating tools")
    sc.set_defaults(func=cmd_schemas)

    hp = sub.add_parser(
        "hash-password",
        help="Generate a PBKDF2 e-signature password entry for principals.yaml")
    hp.set_defaults(func=cmd_hash_password)

    r = sub.add_parser("run", help="Execute a protocol JSON file")
    r.add_argument("protocol")
    r.add_argument("--lab", required=True)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--validate-only", action="store_true")
    r.add_argument("--checkpoint")
    r.add_argument("--resume", action="store_true")
    r.add_argument("--actor", default="")
    r.set_defaults(func=cmd_run)

    a = sub.add_parser("audit", help="Inspect or verify an audit log")
    a.add_argument("path")
    a.add_argument("action", nargs="?", default="tail",
                   choices=["tail", "verify", "summary"])
    a.add_argument("--limit", type=int, default=20)
    a.set_defaults(func=cmd_audit)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "version", False):
        from . import __version__
        print(f"labaiagent {__version__}")
        return 0
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
