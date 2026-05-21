# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A self-contained learning testbed for troubleshooting HL7 v2 ↔ FHIR integrations. Not a library, not a product — a reproducible local pipeline plus a long-form setup guide. `SETUP.md` is the primary artifact; everything else (compose file, Python scripts, exported channel XML) exists to support it.

Pipeline shape:

```
sender.py / generate.py ── MLLP/TCP 6661 ──► Mirth (container) ── HTTPS 4005 ──► HAPI FHIR (container)
```

If you're being asked to change behavior, default to assuming the user wants the **walkthrough in SETUP.md** to remain accurate. Updates to the channel JS, the destination template, or the senders almost always need a parallel edit to SETUP.md.

## Authoritative artifacts vs. documentation copies

This repo has an unusual split. Read this before editing channel logic:

| File | Status | Loaded at runtime? |
|---|---|---|
| `mirth/channels/hl7v2-adt-to-fhir-patient.xml` | **Authoritative**. Exported from the running Mirth channel via REST API. | Yes — imported into Mirth's Derby DB. |
| `mirth/source-transformer.js` | **Documentation copy.** Mirrors the JS pasted into the channel's Source > Transformer tab. | No. |
| `mirth/destination-template.json` | **Documentation copy.** Mirrors the Content textarea of the HTTP Sender destination. | No. |

Changing the JS or JSON files alone does nothing to a running Mirth instance. To actually update behavior:
1. Edit the JS/JSON files (so source control reflects intent).
2. Paste the new content into the Mirth admin client.
3. Save + redeploy the channel in the admin client.
4. Re-export the channel XML over the REST API (see SETUP.md → "Preserving channels across recreates") and commit it.

The `.xml` and the JS/JSON files drifting apart is a real failure mode — when reviewing changes, check that all three are consistent.

## Common commands

```bash
# Stack lifecycle (compose file ships with the right ports, env vars, restart policy)
docker compose up -d
docker compose ps
docker compose logs -f mirth
docker compose stop          # keep state
docker compose down          # remove containers, named volumes survive
docker compose down -v       # nuke channel state too

# Verify HAPI FHIR is up
curl -s http://localhost:4005/fhir/metadata | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['software']['name'], d['software']['version'], '/', d['fhirVersion'])"

# Wait for Mirth REST API readiness (30–90s cold start)
until curl -k -s -o /dev/null -w '%{http_code}\n' https://localhost:4443/api/users/_login | grep -q 200; do sleep 2; done

# Send the canonical sample message
python3 sender.py
python3 sender.py 127.0.0.1 6662   # override host/port

# Scenario generator (14 named failure modes across baseline/bad-data/structure/transport)
python3 generate.py list
python3 generate.py send admit
python3 generate.py send-all --delay 0.5

# Read back from HAPI
curl -s 'http://localhost:4005/fhir/Patient?family=Doe' | python3 -m json.tool
curl -s 'http://localhost:4005/fhir/Patient?identifier=http://test-facility.example.org/mrn|MRN-1001' | python3 -c "import json,sys; print('count:', json.load(sys.stdin)['total'])"
```

No build, no lint, no test suite — Python scripts use stdlib only (`socket`, `argparse`, `datetime`). Don't add a `requirements.txt` or third-party HL7 library; the hand-rolled MLLP framing is pedagogically intentional.

## Port map (memorize this — most "it doesn't work" issues map back here)

| Host | Container | Purpose |
|---|---|---|
| 4005 | hapi-fhir:8080 | HAPI FHIR REST API |
| 4443 | mirth:8443 | Mirth admin HTTPS / REST |
| 4006 | mirth:8080 | Mirth admin HTTP |
| 6661 | mirth:6661 | MLLP TCP listener (the channel binds this) |

The write path's last leg goes Mirth → `host.docker.internal:4005` → host → HAPI. It does **not** use a shared Docker network; that's deliberate.

## Editing the senders

`sender.py` and `generate.py` build HL7 v2 messages by string concatenation. The "patient block" at the top of `sender.py` (MRN, name, DOB, gender, address) is the editable surface; everything else is segment/framing scaffolding. Field separators (`|`, `^`, `&`) and MLLP framing bytes (`\x0b`, `\x1c`, `\x0d`) are load-bearing — don't replace them with helpers without updating SETUP.md's "what MLLP actually is" sections.

When adding a new scenario to `generate.py`, register it in the `SCENARIOS` list with `(key, group, expected_behavior, fn)`. Groups currently in use: `baseline`, `bad-data`, `structure`, `transport`. The `expected` string surfaces in `list` output and should describe what a learner should observe in the Mirth Message Browser — not just "this fails."

## Editing the Mirth channel logic

The destination URL uses a FHIR **conditional update** pattern: `PUT /fhir/Patient?identifier=system|value`. The `:`, `/`, and `|` inside the query value **must be URL-encoded** (`%3A`, `%2F`, `%7C`). Mirth wraps Java's `java.net.URI`, which is RFC 3986 strict — most other HTTP clients tolerate raw pipes; Mirth does not. Don't "fix" the encoding by removing it.

The source transformer uses E4X-style access (`msg['PID']['PID.3']['PID.3.1']`). This requires the **Source Inbound** data type to be `HL7 v2.x` in the channel's "Set Data Types" dialog. If someone reports the transformer suddenly returning empty strings, that setting reverting to `Raw` is the first thing to check.

## Documentation discipline

`SETUP.md` is the deliverable. When editing it:

- The troubleshooting table is the most-used section — every entry should encode a real failure that was actually hit, with the actual fix, not generic advice.
- Code blocks in SETUP.md should be runnable as-is on macOS. Linux variations (e.g. `sed -i` without the `''`) are noted inline rather than duplicating the whole block.
- Don't hard-wrap markdown prose paragraphs — long lines render fine and survive copy-paste into GitHub/Notion.

`diagrams/` holds hand-authored SVGs referenced by SETUP.md. They're not generated from any source — edit the SVG directly if a diagram needs to change.

## Scope expectations for this repo

- macOS-specific instructions are fine and expected (Mac is the documented target). Don't bend the guide to be cross-platform; note Linux deltas inline where they're trivial.
- The setup is intentionally not production-shaped (Derby in-container DB, self-signed certs, `admin/admin` defaults). Don't "harden" it without being asked — the learning goal is to see the moving parts.
- The pipeline is **one-way** (v2 → FHIR). There is no FHIR → v2 path. If asked to add one, that's a new channel, not a modification of the existing one.
