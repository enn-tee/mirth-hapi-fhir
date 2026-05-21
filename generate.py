#!/usr/bin/env python3
"""HL7 v2 scenario generator for the v2 → FHIR pipeline.

Each scenario sends a specific shape of message (or non-message) to exercise
a different integration behavior. Use these to populate Mirth's Message
Browser with examples of the failure modes you'll need to recognize in
production troubleshooting.

Usage:
    python3 generate.py list
    python3 generate.py send admit
    python3 generate.py send-all [--delay 0.5]
"""
import argparse
import socket
import sys
import time
from datetime import datetime

SB, EB, CR = b"\x0b", b"\x1c", b"\x0d"


def ts():
    return datetime.now().strftime("%Y%m%d%H%M%S")


def msh(msg_type="ADT^A01", ctrl=None):
    ctrl = ctrl or f"MSG{ts()}"
    return (
        f"MSH|^~\\&|SENDER|TEST_FACILITY|MIRTH|TEST_FACILITY|{ts()}||"
        f"{msg_type}|{ctrl}|P|2.5"
    )


def pack(segments, sep="\r"):
    return SB + (sep.join(segments) + sep).encode("utf-8") + EB + CR


# ─── Scenario builders ──────────────────────────────────────────────────────
# Each returns a list of byte chunks to send. Most return one chunk (a fully
# framed message); a few transport-failure scenarios return raw or partial bytes.


def s_admit():
    return [pack([
        msh("ADT^A01"),
        f"EVN|A01|{ts()}",
        "PID|1||MRN-2001^^^TEST_FACILITY^MR||Smith^Alice||19900315|F|||"
        "456 Oak Ave^^Boston^MA^02101",
        "PV1|1|I|WARD^102^A|||||||MED",
    ])]


def s_update():
    return [pack([
        msh("ADT^A08"),
        f"EVN|A08|{ts()}",
        "PID|1||MRN-2001^^^TEST_FACILITY^MR||Smith^Alice||19900315|F|||"
        "789 Pine Rd^^Cambridge^MA^02139",  # new address, same MRN
        "PV1|1|I|WARD^102^A|||||||MED",
    ])]


def s_discharge():
    return [pack([
        msh("ADT^A03"),
        f"EVN|A03|{ts()}",
        "PID|1||MRN-2001^^^TEST_FACILITY^MR||Smith^Alice||19900315|F|||"
        "456 Oak Ave^^Boston^MA^02101",
        "PV1|1|O|WARD^102^A|||||||MED",
    ])]


def s_unicode_name():
    return [pack([
        msh("ADT^A01"),
        f"EVN|A01|{ts()}",
        "PID|1||MRN-2002^^^TEST_FACILITY^MR||Müller^José||19751120|M|||"
        "12 Königstrasse^^Berlin^DE^10117",
        "PV1|1|I|WARD^103^A|||||||MED",
    ])]


def s_missing_mrn():
    return [pack([
        msh("ADT^A01"),
        f"EVN|A01|{ts()}",
        "PID|1|||Last^First||19900101|M|||111 Some St^^City^ST^00000",
        "PV1|1|I|WARD^101^A|||||||MED",
    ])]


def s_missing_name():
    return [pack([
        msh("ADT^A01"),
        f"EVN|A01|{ts()}",
        "PID|1||MRN-2003^^^TEST_FACILITY^MR|||19900101|M|||"
        "111 Some St^^City^ST^00000",
        "PV1|1|I|WARD^101^A|||||||MED",
    ])]


def s_bad_date():
    return [pack([
        msh("ADT^A01"),
        f"EVN|A01|{ts()}",
        # Wrong DOB format — slashes instead of YYYYMMDD
        "PID|1||MRN-2004^^^TEST_FACILITY^MR||Smith^Bob||04/12/1985|M|||"
        "111 Some St^^City^ST^00000",
        "PV1|1|I|WARD^101^A|||||||MED",
    ])]


def s_bad_gender():
    return [pack([
        msh("ADT^A01"),
        f"EVN|A01|{ts()}",
        # Gender code 'X' is not in our M/F/O/U map — transformer falls back to 'unknown'
        "PID|1||MRN-2005^^^TEST_FACILITY^MR||Doe^Pat||19900101|X|||"
        "111 Some St^^City^ST^00000",
        "PV1|1|I|WARD^101^A|||||||MED",
    ])]


def s_pipe_in_name():
    return [pack([
        msh("ADT^A01"),
        f"EVN|A01|{ts()}",
        # Unescaped | mid-name — should be \F\. The parser treats it as a field
        # separator, shifting every following PID field by one position.
        "PID|1||MRN-2006^^^TEST_FACILITY^MR||Smith|Jr^Bob||19900101|M|||"
        "111 Some St^^City^ST^00000",
        "PV1|1|I|WARD^101^A|||||||MED",
    ])]


def s_no_pid():
    return [pack([
        msh("ADT^A01"),
        f"EVN|A01|{ts()}",
        "PV1|1|I|WARD^101^A|||||||MED",
    ])]


def s_no_msh():
    return [pack([
        f"EVN|A01|{ts()}",
        "PID|1||MRN-2007^^^TEST_FACILITY^MR||Doe^Eve||19900101|F|||"
        "111 Some St^^City^ST^00000",
        "PV1|1|I|WARD^101^A|||||||MED",
    ])]


def s_oru_result():
    return [pack([
        msh("ORU^R01"),
        "PID|1||MRN-2008^^^TEST_FACILITY^MR||Test^Lab||19900101|F",
        "OBR|1|||CBC^Complete Blood Count|||20260520120000",
        "OBX|1|NM|WBC^WBC|1|7.2|10*3/uL|4.0-11.0|N|||F",
    ])]


def s_wrong_line_endings():
    return [pack([
        msh("ADT^A01"),
        f"EVN|A01|{ts()}",
        "PID|1||MRN-2009^^^TEST_FACILITY^MR||Test^LineEnd||19900101|M|||"
        "111 Some St^^City^ST^00000",
        "PV1|1|I|WARD^101^A|||||||MED",
    ], sep="\n")]  # \n instead of \r between segments


def s_no_mllp_frame():
    body = "\r".join([
        msh("ADT^A01"),
        f"EVN|A01|{ts()}",
        "PID|1||MRN-2010^^^TEST_FACILITY^MR||Test^Raw||19900101|M",
    ]) + "\r"
    return [body.encode("utf-8")]  # no SB / EB / CR framing


# ─── Registry ───────────────────────────────────────────────────────────────

SCENARIOS = [
    # (key, group, expected, fn)
    ("admit",              "baseline",   "ADT^A01 — Patient created (HAPI 201)",                       s_admit),
    ("update",             "baseline",   "ADT^A08 — same MRN, new address; Patient updated (200)",     s_update),
    ("discharge",          "baseline",   "ADT^A03 — channel still updates Patient",                    s_discharge),
    ("unicode-name",       "baseline",   "Non-ASCII name characters preserved as UTF-8",               s_unicode_name),
    ("missing-mrn",        "bad-data",   "Empty PID-3 → empty identifier in FHIR URL",                 s_missing_mrn),
    ("missing-name",       "bad-data",   "Empty PID-5 → Patient resource has no name",                 s_missing_name),
    ("bad-date",           "bad-data",   "Non-v2 date format → birthDate extracted as ''",             s_bad_date),
    ("bad-gender",         "bad-data",   "Unknown gender code → transformer falls back to 'unknown'",  s_bad_gender),
    ("pipe-in-name",       "bad-data",   "Unescaped '|' in name — corrupts following fields",          s_pipe_in_name),
    ("no-pid",             "structure",  "Missing PID segment — transformer throws",                   s_no_pid),
    ("no-msh",             "structure",  "Missing MSH header — parser rejects",                        s_no_msh),
    ("oru-result",         "structure",  "Wrong message type (lab result) sent at ADT channel",        s_oru_result),
    ("wrong-line-endings", "transport",  "Segments joined with \\n instead of \\r",                    s_wrong_line_endings),
    ("no-mllp-frame",      "transport",  "Raw HL7 with no VT/FS framing bytes",                        s_no_mllp_frame),
]


def list_scenarios():
    width = max(len(k) for k, *_ in SCENARIOS) + 2
    group = None
    for key, g, expected, _ in SCENARIOS:
        if g != group:
            print(f"\n[{g}]")
            group = g
        print(f"  {key:<{width}}{expected}")
    print()


def send_one(key, host, port):
    match = next((s for s in SCENARIOS if s[0] == key), None)
    if not match:
        print(f"unknown scenario: {key}", file=sys.stderr)
        print("run: python3 generate.py list", file=sys.stderr)
        sys.exit(2)

    _, group, expected, fn = match
    print(f"=== {key} [{group}] — {expected} ===")
    try:
        with socket.create_connection((host, port), timeout=5) as sock:
            for chunk in fn():
                sock.sendall(chunk)
            sock.settimeout(3)
            buf = b""
            try:
                while EB not in buf:
                    data = sock.recv(4096)
                    if not data:
                        break
                    buf += data
            except socket.timeout:
                pass

            if not buf:
                print("  (no ACK — timeout or connection closed)")
                return
            ack = buf.translate(None, SB + EB + CR).decode("utf-8", errors="replace")
            msa = next((ln for ln in ack.split("\r") if ln.startswith("MSA")), None)
            print(f"  ACK: {msa or ack[:160]}")
    except (ConnectionRefusedError, OSError) as e:
        print(f"  connection failed: {e}")


def send_all(host, port, delay):
    for key, *_ in SCENARIOS:
        send_one(key, host, port)
        time.sleep(delay)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list available scenarios")

    p_send = sub.add_parser("send", help="send one scenario by name")
    p_send.add_argument("scenario")
    p_send.add_argument("--host", default="localhost")
    p_send.add_argument("--port", type=int, default=6661)

    p_all = sub.add_parser("send-all", help="send all scenarios in order")
    p_all.add_argument("--host", default="localhost")
    p_all.add_argument("--port", type=int, default=6661)
    p_all.add_argument("--delay", type=float, default=0.5,
                       help="seconds between scenarios (default 0.5)")

    args = ap.parse_args()
    if args.cmd == "list":
        list_scenarios()
    elif args.cmd == "send":
        send_one(args.scenario, args.host, args.port)
    elif args.cmd == "send-all":
        send_all(args.host, args.port, args.delay)
