#!/usr/bin/env python3
"""HL7 v2 sender + scenario CLI for the v2 -> FHIR pipeline testbed.

Three orthogonal axes:
  --message-type   adt-a01 | adt-a03 | adt-a08 | oru-r01
  --patient        canonical | random  (+ --locale for random)
  --error          one of the registered overlays (see `list`)

Every combination is valid: an error overlay is just a transform layered on
top of a base message, so any patient source + any message type + any error
will produce a defined byte stream.

Examples:
  python3 scripts/hl7.py send                           # canonical, ADT^A01
  python3 scripts/hl7.py send --random                  # faker-generated patient
  python3 scripts/hl7.py send --error bad-date
  python3 scripts/hl7.py send --random --locale de_DE --error pipe-in-name
  python3 scripts/hl7.py send-all                       # populate Message Browser
  python3 scripts/hl7.py list                           # show all options
"""
from __future__ import annotations

import argparse
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

try:
    from faker import Faker
except ImportError:
    sys.stderr.write(
        "error: the `faker` library is required.\n"
        "Install it with:\n"
        "    pip install -r requirements.txt\n"
    )
    sys.exit(2)


# MLLP framing — every HL7 v2 message on the wire is bracketed by these bytes.
SB = b"\x0b"   # start block (vertical tab)
EB = b"\x1c"   # end block (file separator)
CR = b"\x0d"   # carriage return

DEFAULT_HOST = "localhost"
DEFAULT_PORT = 6661


# ─── Patient model ──────────────────────────────────────────────────────────


@dataclass
class Patient:
    mrn: str
    last_name: str
    first_name: str
    dob: str          # YYYYMMDD per HL7 v2
    gender: str       # M / F / O / U
    addr_line: str
    addr_city: str
    addr_state: str
    addr_zip: str


CANONICAL = Patient(
    mrn="MRN-1001",
    last_name="Doe",
    first_name="John",
    dob="19850412",
    gender="M",
    addr_line="123 Main St",
    addr_city="Atlanta",
    addr_state="GA",
    addr_zip="30301",
)


def canonical_patient() -> Patient:
    return CANONICAL


def random_patient(locale: str = "en_US") -> Patient:
    """Faker-generated patient. Locale picks names + address shape.

    DESIGN-CHOICE: This is the default Faker mapping. Reasonable alternatives:
      - MRN format: uuid4() is unique but ugly; ssn() is realistic but
        ethically iffy for tests; bothify("MRN-####") matches our hand-rolled
        style and is the current pick.
      - DOB range: minimum_age=0,maximum_age=100 covers neonates through
        centenarians, which is good for edge-case hunting.
      - state_abbr() exists only on en_US; Faker silently falls back to en_US
        on other locales, so non-US runs get a US state code — fine for a
        testbed, but if it bothers you, replace with administrative_unit().
    """
    fake = Faker(locale)
    return Patient(
        mrn=fake.bothify("MRN-####"),
        last_name=fake.last_name(),
        first_name=fake.first_name(),
        dob=fake.date_of_birth(minimum_age=0, maximum_age=100).strftime("%Y%m%d"),
        gender=fake.random_element(["M", "F", "O", "U"]),
        addr_line=fake.street_address(),
        addr_city=fake.city(),
        addr_state=fake.state_abbr() if hasattr(fake, "state_abbr") else "",
        addr_zip=fake.postcode(),
    )


# ─── Message builders ───────────────────────────────────────────────────────


def _now() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _msh(message_type: str) -> str:
    ts = _now()
    return (
        f"MSH|^~\\&|SENDER|TEST_FACILITY|MIRTH|TEST_FACILITY|{ts}||"
        f"{message_type}|MSG{ts}|P|2.5"
    )


def _pid(p: Patient) -> str:
    return (
        f"PID|1||{p.mrn}^^^TEST_FACILITY^MR||{p.last_name}^{p.first_name}||"
        f"{p.dob}|{p.gender}|||"
        f"{p.addr_line}^^{p.addr_city}^{p.addr_state}^{p.addr_zip}"
    )


def build_adt_a01(p: Patient) -> list:
    return [
        _msh("ADT^A01"),
        f"EVN|A01|{_now()}",
        _pid(p),
        "PV1|1|I|WARD^101^A|||||||MED",
    ]


def build_adt_a03(p: Patient) -> list:
    return [
        _msh("ADT^A03"),
        f"EVN|A03|{_now()}",
        _pid(p),
        "PV1|1|O|WARD^102^A|||||||MED",
    ]


def build_adt_a08(p: Patient) -> list:
    return [
        _msh("ADT^A08"),
        f"EVN|A08|{_now()}",
        _pid(p),
        "PV1|1|I|WARD^102^A|||||||MED",
    ]


def build_oru_r01(p: Patient) -> list:
    return [
        _msh("ORU^R01"),
        _pid(p),
        "OBR|1|||CBC^Complete Blood Count|||20260520120000",
        "OBX|1|NM|WBC^WBC|1|7.2|10*3/uL|4.0-11.0|N|||F",
    ]


MESSAGE_TYPES = {
    "adt-a01": ("ADT^A01 — patient admission",  build_adt_a01),
    "adt-a03": ("ADT^A03 — patient discharge",  build_adt_a03),
    "adt-a08": ("ADT^A08 — patient update",     build_adt_a08),
    "oru-r01": ("ORU^R01 — lab result",         build_oru_r01),
}


# ─── Error overlays ─────────────────────────────────────────────────────────
#
# Each overlay declares which stage of the pipeline it hooks into:
#   "message"    — operates on list[str] segments before join
#   "serialize"  — operates on the separator used to join segments
#   "frame"      — operates on the framed byte string


@dataclass
class ErrorOverlay:
    name: str
    group: str          # "bad-data" | "structure" | "transport"
    description: str
    apply: Callable
    stage: str = "message"


def _replace_pid_field(segments: list, index: int, value: str) -> list:
    """Helper: rewrite PID field at `index` (0=PID, 3=MRN, 5=name, 7=DOB, 8=gender)."""
    out = []
    for seg in segments:
        if seg.startswith("PID|"):
            fields = seg.split("|")
            while len(fields) <= index:
                fields.append("")
            fields[index] = value
            out.append("|".join(fields))
        else:
            out.append(seg)
    return out


def _err_missing_mrn(segments):
    return _replace_pid_field(segments, 3, "")


def _err_missing_name(segments):
    return _replace_pid_field(segments, 5, "")


def _err_bad_date(segments):
    # DESIGN-CHOICE: '04/12/1985' is the classic 'looks-right-to-a-human,
    # wrong-format-to-the-parser' failure. Other realistic shapes worth
    # swapping in for different teaching moments:
    #   - '19851345'              impossible day; passes format check
    #   - '1985-04-12T00:00:00'   ISO with time; common upstream system bug
    return _replace_pid_field(segments, 7, "04/12/1985")


def _err_bad_gender(segments):
    return _replace_pid_field(segments, 8, "X")


def _err_pipe_in_name(segments):
    """Inject unescaped '|' mid-PID-5. Every following PID field shifts by one."""
    out = []
    for seg in segments:
        if seg.startswith("PID|"):
            fields = seg.split("|")
            # PID-5 is fields[5]; replace the first '^' inside the name only,
            # not the earlier '^' that lives in PID-3's identifier component.
            # Smith^John -> Smith|Jr^John  (Jr is then read as PID-6.)
            if len(fields) > 5 and "^" in fields[5]:
                fields[5] = fields[5].replace("^", "|Jr^", 1)
            out.append("|".join(fields))
        else:
            out.append(seg)
    return out


def _err_no_pid(segments):
    return [s for s in segments if not s.startswith("PID|")]


def _err_no_msh(segments):
    return [s for s in segments if not s.startswith("MSH|")]


def _err_wrong_line_endings(_sep: str) -> str:
    return "\n"


def _err_no_mllp_frame(framed: bytes) -> bytes:
    body = framed
    if body.startswith(SB):
        body = body[len(SB):]
    if body.endswith(EB + CR):
        body = body[: -len(EB + CR)]
    return body


ERRORS = {
    o.name: o for o in [
        ErrorOverlay("missing-mrn",        "bad-data",  "Empty PID-3 → empty identifier in FHIR URL",                _err_missing_mrn),
        ErrorOverlay("missing-name",       "bad-data",  "Empty PID-5 → Patient resource has no name",                _err_missing_name),
        ErrorOverlay("bad-date",           "bad-data",  "Non-v2 date format → birthDate extracted as ''",            _err_bad_date),
        ErrorOverlay("bad-gender",         "bad-data",  "Unknown gender code → transformer falls back to 'unknown'", _err_bad_gender),
        ErrorOverlay("pipe-in-name",       "bad-data",  "Unescaped '|' in name — corrupts following PID fields",     _err_pipe_in_name),
        ErrorOverlay("no-pid",             "structure", "Missing PID segment — transformer throws",                  _err_no_pid),
        ErrorOverlay("no-msh",             "structure", "Missing MSH header — parser rejects",                       _err_no_msh),
        ErrorOverlay("wrong-line-endings", "transport", r"Segments joined with \n instead of \r",                    _err_wrong_line_endings, "serialize"),
        ErrorOverlay("no-mllp-frame",      "transport", "Raw HL7 with no VT/FS framing bytes",                       _err_no_mllp_frame,      "frame"),
    ]
}


# ─── Compose ────────────────────────────────────────────────────────────────


def build_framed_message(
    message_type: str,
    patient: Patient,
    error: Optional[ErrorOverlay] = None,
) -> bytes:
    """Run the three-axis compose pipeline."""
    _, builder = MESSAGE_TYPES[message_type]
    segments = builder(patient)
    sep = "\r"

    if error is not None:
        if error.stage == "message":
            segments = error.apply(segments)
        elif error.stage == "serialize":
            sep = error.apply(sep)

    body = sep.join(segments) + sep
    framed = SB + body.encode("utf-8") + EB + CR

    if error is not None and error.stage == "frame":
        framed = error.apply(framed)

    return framed


# ─── Transport ──────────────────────────────────────────────────────────────


def send_to_mirth(framed: bytes, host: str, port: int, timeout: float = 5.0) -> str:
    """Send framed bytes, read ACK (or timeout). Returns the ACK string."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.sendall(framed)
            sock.settimeout(3.0)
            buf = b""
            try:
                while EB not in buf:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
            except socket.timeout:
                pass

            if not buf:
                return "(no ACK — timeout or connection closed)"
            return buf.translate(None, SB + EB + CR).decode("utf-8", errors="replace")
    except (ConnectionRefusedError, OSError) as e:
        return f"(connection failed: {e})"


def _ack_msa(ack: str) -> str:
    for line in ack.split("\r"):
        if line.startswith("MSA"):
            return line
    return ack[:160]


# ─── CLI commands ───────────────────────────────────────────────────────────


def _patient_for(args) -> Patient:
    if args.random or args.patient == "random":
        return random_patient(locale=args.locale)
    return canonical_patient()


def cmd_list(_args):
    print("\nMessage types (--message-type):")
    width = max(len(k) for k in MESSAGE_TYPES) + 2
    for key, (desc, _) in MESSAGE_TYPES.items():
        print(f"  {key:<{width}}{desc}")

    print("\nPatient sources (--patient, plus --locale when random):")
    print(f"  {'canonical':<14}Hardcoded John Doe — same identifiers across runs")
    print(f"  {'random':<14}Faker-generated. --locale en_US, de_DE, ja_JP, fr_FR, etc.")
    print("                (--random is shorthand for --patient random)")

    print("\nError overlays (--error):")
    width = max(len(e.name) for e in ERRORS.values()) + 2
    group = None
    for e in ERRORS.values():
        if e.group != group:
            print(f"\n  [{e.group}]")
            group = e.group
        print(f"    {e.name:<{width}}{e.description}")
    print()


def cmd_send(args):
    overlay = ERRORS[args.error] if args.error else None
    patient = _patient_for(args)

    label_parts = [f"message-type={args.message_type}",
                   f"patient={'random' if (args.random or args.patient == 'random') else 'canonical'}"]
    if args.random or args.patient == "random":
        label_parts.append(f"locale={args.locale}")
    if overlay:
        label_parts.append(f"error={args.error}")
    print(f"=== send: {'  '.join(label_parts)} ===")

    framed = build_framed_message(args.message_type, patient, overlay)
    ack = send_to_mirth(framed, args.host, args.port)
    print(f"  ACK: {_ack_msa(ack)}")


def cmd_send_all(args):
    print(f"=== send-all: {len(ERRORS)} error overlays, "
          f"message-type={args.message_type}, "
          f"patient={'random' if (args.random or args.patient == 'random') else 'canonical'} ===")
    for name, overlay in ERRORS.items():
        patient = _patient_for(args)
        print(f"\n[{overlay.group}] {name} — {overlay.description}")
        framed = build_framed_message(args.message_type, patient, overlay)
        ack = send_to_mirth(framed, args.host, args.port)
        print(f"  ACK: {_ack_msa(ack)}")
        time.sleep(args.delay)


# ─── argparse wiring ────────────────────────────────────────────────────────


def _add_common_send_args(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--message-type", default="adt-a01",
                    choices=list(MESSAGE_TYPES.keys()),
                    help="HL7 v2 message type (default: adt-a01)")
    sp.add_argument("--patient", default="canonical",
                    choices=["canonical", "random"],
                    help="patient data source (default: canonical)")
    sp.add_argument("--random", action="store_true",
                    help="shorthand for --patient random")
    sp.add_argument("--locale", default="en_US",
                    help="Faker locale when --patient random (default: en_US)")
    sp.add_argument("--host", default=DEFAULT_HOST,
                    help=f"MLLP host (default: {DEFAULT_HOST})")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"MLLP port (default: {DEFAULT_PORT})")


EPILOG = """\
Examples:
  python3 scripts/hl7.py send                              # canonical, ADT^A01
  python3 scripts/hl7.py send --random                     # faker patient
  python3 scripts/hl7.py send --error bad-date             # apply overlay
  python3 scripts/hl7.py send --random --locale de_DE      # non-ASCII names
  python3 scripts/hl7.py send --random --error pipe-in-name --message-type adt-a08
  python3 scripts/hl7.py send-all                          # every overlay (Message Browser)
  python3 scripts/hl7.py list                              # discover options

Run any subcommand with --help to see all flags.
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hl7.py",
        description="HL7 v2 sender + scenario CLI for the v2 -> FHIR testbed.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("list", help="show every message-type, patient source, error overlay")

    p_send = sub.add_parser("send", help="send one message",
                            formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_common_send_args(p_send)
    p_send.add_argument("--error", default=None,
                        help="error overlay name (run `list` for choices)")

    p_all = sub.add_parser("send-all", help="send every error overlay in sequence",
                           formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_common_send_args(p_all)
    p_all.add_argument("--delay", type=float, default=0.5,
                       help="seconds between scenarios (default: 0.5)")

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.cmd:
        parser.print_help()
        sys.exit(0)

    if args.cmd == "send" and args.error and args.error not in ERRORS:
        sys.stderr.write(
            f"error: unknown error overlay '{args.error}'. Run 'list' to see options.\n"
        )
        sys.exit(2)

    if args.cmd == "list":
        cmd_list(args)
    elif args.cmd == "send":
        cmd_send(args)
    elif args.cmd == "send-all":
        cmd_send_all(args)


if __name__ == "__main__":
    main()
