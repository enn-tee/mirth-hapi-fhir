# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A self-contained learning testbed for troubleshooting HL7 v2 ↔ FHIR integrations. Not a library, not a product — a reproducible local pipeline plus a long-form setup guide. `SETUP.md` is the primary artifact (the long-form tutorial); `README.md` is the quick reference with a mermaid system diagram and CLI cheatsheet; everything else (compose file, Python scripts, exported channel XML) exists to support those two.

Pipeline shape:

```
scripts/hl7.py ── MLLP/TCP 6661 ──► Mirth (container) ── HTTPS 4005 ──► HAPI FHIR (container)
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

# First-time setup (only third-party dep is faker)
pip install -r requirements.txt

# CLI discoverability
python3 scripts/hl7.py                   # prints help
python3 scripts/hl7.py list              # show message-types, patient sources, error overlays

# Canonical sample message (ADT^A01, John Doe)
python3 scripts/hl7.py send

# Composable axes: message-type × patient × error
python3 scripts/hl7.py send --random                                # faker patient, default ADT^A01
python3 scripts/hl7.py send --error bad-date                        # apply error overlay
python3 scripts/hl7.py send --random --locale de_DE --error pipe-in-name --message-type adt-a08
python3 scripts/hl7.py send-all --delay 0.5                         # one of every error overlay

# Override transport
python3 scripts/hl7.py send --host 127.0.0.1 --port 6662

# Read back from HAPI
curl -s 'http://localhost:4005/fhir/Patient?family=Doe' | python3 -m json.tool
curl -s 'http://localhost:4005/fhir/Patient?identifier=http://test-facility.example.org/mrn|MRN-1001' | python3 -c "import json,sys; print('count:', json.load(sys.stdin)['total'])"
```

No build, no lint, no test suite. The only third-party dependency is **Faker** (used by `random_patient()` to generate fake demographics). Don't add an HL7-parsing library — the hand-rolled MLLP framing and segment construction in `scripts/hl7.py` are pedagogically intentional.

## Port map (memorize this — most "it doesn't work" issues map back here)

| Host | Container | Purpose |
|---|---|---|
| 4005 | hapi-fhir:8080 | HAPI FHIR REST API |
| 4443 | mirth:8443 | Mirth admin HTTPS / REST |
| 4006 | mirth:8080 | Mirth admin HTTP |
| 6661 | mirth:6661 | MLLP TCP listener (the channel binds this) |

The write path's last leg goes Mirth → `host.docker.internal:4005` → host → HAPI. It does **not** use a shared Docker network; that's deliberate.

## Editing `scripts/hl7.py`

The CLI is structured top-down in one file: framing constants → `Patient` dataclass → `canonical_patient()` / `random_patient()` → message-type builders (`build_adt_a01`, …) → error overlays + `ERRORS` registry → `build_framed_message()` compose → `send_to_mirth()` → argparse wiring. Field separators (`|`, `^`, `&`) and MLLP framing bytes (`\x0b`, `\x1c`, `\x0d`) are load-bearing — don't replace them with helpers without updating SETUP.md's "what MLLP actually is" sections.

Three orthogonal axes, all composable: `--message-type` × `--patient` × `--error`. Every combination must run without raising — overlays that target a missing field should no-op rather than throw.

**Adding a new error overlay**: append to the `ERRORS` registry construction with `ErrorOverlay(name, group, description, apply_fn, stage="message")`. Groups in use: `bad-data`, `structure`, `transport`. Stage is `"message"` (list of segments in/out), `"serialize"` (separator string in/out — `wrong-line-endings`), or `"frame"` (framed bytes in/out — `no-mllp-frame`). The `description` string surfaces in `list` output and during `send-all` — write it so a learner can predict what they'll see in the Message Browser, not just "this fails."

**Adding a new message type**: add a `build_<type>(patient: Patient) -> list` function and register it in `MESSAGE_TYPES`. Reuse `_msh()` and `_pid()` helpers to stay consistent.

**Refining `random_patient()`**: search for the `DESIGN-CHOICE:` comment — the current Faker field choices (MRN format, DOB range, locale handling) are documented with alternatives so you can swap shapes without re-reading the whole function.

## Editing the Mirth channel logic

The destination URL uses a FHIR **conditional update** pattern: `PUT /fhir/Patient?identifier=system|value`. The `:`, `/`, and `|` inside the query value **must be URL-encoded** (`%3A`, `%2F`, `%7C`). Mirth wraps Java's `java.net.URI`, which is RFC 3986 strict — most other HTTP clients tolerate raw pipes; Mirth does not. Don't "fix" the encoding by removing it.

The source transformer uses E4X-style access (`msg['PID']['PID.3']['PID.3.1']`). This requires the **Source Inbound** data type to be `HL7 v2.x` in the channel's "Set Data Types" dialog. If someone reports the transformer suddenly returning empty strings, that setting reverting to `Raw` is the first thing to check.

## Documentation discipline

`SETUP.md` is the deliverable. `README.md` is the GitHub-facing entry point. When editing either:

- The troubleshooting table in SETUP.md is the most-used section — every entry should encode a real failure that was actually hit, with the actual fix, not generic advice.
- Code blocks should be runnable as-is on macOS. Linux variations (e.g. `sed -i` without the `''`) are noted inline rather than duplicating the whole block.
- Don't hard-wrap markdown prose paragraphs — long lines render fine and survive copy-paste into GitHub/Notion.
- README.md has a mermaid diagram (`flowchart LR` with nested `direction TB` subgraphs); GitHub's mermaid renderer handles it. Validate with the Mermaid Chart MCP tool before committing changes to it.

`diagrams/` holds hand-authored SVGs referenced by SETUP.md. They're not generated from any source — edit the SVG directly if a diagram needs to change. The mermaid diagram in README.md is the only renderer-generated diagram in this repo; the SVGs and mermaid coexist on purpose (SVGs are bigger and more detailed; mermaid is the inline cheatsheet view).

## Scope expectations for this repo

- macOS-specific instructions are fine and expected (Mac is the documented target). Don't bend the guide to be cross-platform; note Linux deltas inline where they're trivial.
- The setup is intentionally not production-shaped (Derby in-container DB, self-signed certs, `admin/admin` defaults). Don't "harden" it without being asked — the learning goal is to see the moving parts.
- The pipeline is **one-way** (v2 → FHIR). There is no FHIR → v2 path. If asked to add one, that's a new channel, not a modification of the existing one.
