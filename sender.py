#!/usr/bin/env python3
"""Send a sample HL7 v2 ADT^A01 (patient admission) message over MLLP.

Default target is localhost:6661 — the Mirth Connect MLLP listener you'll
configure in the channel walkthrough. Mirth's channel converts the v2 message
into a FHIR Patient + Encounter and POSTs them to the HAPI FHIR server on 4005.

Usage:
    python3 sender.py                       # localhost:6661
    python3 sender.py 127.0.0.1 6662        # override host/port
"""
import socket
import sys
from datetime import datetime

SB = b"\x0b"
EB = b"\x1c"
CR = b"\x0d"

# --- EDIT THIS PATIENT BLOCK to send different data through the pipeline ----
# After sending, query HAPI to confirm the values land correctly:
#   curl -s 'http://localhost:4005/fhir/Patient?family=Doe' | jq
PATIENT_MRN   = "MRN-1001"
LAST_NAME     = "Doe"
FIRST_NAME    = "John"
DOB           = "19850412"       # YYYYMMDD
GENDER        = "M"              # M / F / O / U
ADDRESS_LINE  = "123 Main St"
ADDRESS_CITY  = "Atlanta"
ADDRESS_STATE = "GA"
ADDRESS_ZIP   = "30301"
# ---------------------------------------------------------------------------

now = datetime.now().strftime("%Y%m%d%H%M%S")
msg_ctrl_id = f"MSG{now}"

# v2 segments are separated by \r. Within a segment, | separates fields,
# ^ separates components, & separates sub-components. The escape is `\&` in MSH.
segments = [
    f"MSH|^~\\&|SENDER|TEST_FACILITY|MIRTH|TEST_FACILITY|{now}||ADT^A01|{msg_ctrl_id}|P|2.5",
    f"EVN|A01|{now}",
    f"PID|1||{PATIENT_MRN}^^^TEST_FACILITY^MR||{LAST_NAME}^{FIRST_NAME}||{DOB}|{GENDER}|||"
    f"{ADDRESS_LINE}^^{ADDRESS_CITY}^{ADDRESS_STATE}^{ADDRESS_ZIP}",
    f"PV1|1|I|WARD^101^A|||||||MED",
]
hl7 = "\r".join(segments) + "\r"

host = sys.argv[1] if len(sys.argv) > 1 else "localhost"
port = int(sys.argv[2]) if len(sys.argv) > 2 else 6661

print(f"--- Sending {len(segments)} segments to {host}:{port} ---")
print(hl7.replace("\r", "\n"))

with socket.create_connection((host, port), timeout=10) as s:
    s.sendall(SB + hl7.encode("utf-8") + EB + CR)
    buf = b""
    while EB not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk

ack = buf.translate(None, SB + EB + CR).decode("utf-8", errors="replace")
print("--- ACK from receiver ---")
print(ack if ack else "(empty — likely a connection or framing problem)")
