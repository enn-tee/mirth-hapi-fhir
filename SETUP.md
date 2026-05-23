# HL7 v2 ↔ FHIR Integration Testbed — Setup Guide

A reproducible setup for learning how to troubleshoot HL7 v2 to FHIR integrations on a local machine. When complete you will have an end-to-end pipeline: a Python script sends MLLP-framed HL7 v2 `ADT^A01` (patient admission) messages, Mirth Connect receives them, transforms each into a FHIR Patient resource, and POSTs that resource to a HAPI FHIR R4 server. Every step is observable in Mirth's Message Browser, which is the actual tool integration engineers use to troubleshoot production interfaces.

## What you'll end up with

| Component | Image | Host port → container port | Purpose |
|---|---|---|---|
| HAPI FHIR server | `hapiproject/hapi:latest` | `4005 → 8080` | Destination FHIR R4 server (clean, empty) |
| Mirth Connect | `nextgenhealthcare/connect:latest` | `4443 → 8443` (admin HTTPS), `4006 → 8080` (admin HTTP), `6661 → 6661` (MLLP listener) | Integration engine — receives v2 over MLLP, converts to FHIR, POSTs to HAPI |
| `scripts/hl7.py` (host) | — | — | Python MLLP CLI: composable `send` / `send-all` / `list` with `--message-type`, `--patient`, `--error` axes. Uses [Faker](https://faker.readthedocs.io/) for random patients. |
| Mirth admin GUI (Java Swing) | Launched on host via the **Mirth Connect Administrator Launcher** (native install4j app from NextGen) | — | Where you build channels, inspect messages, debug failures |

Data flow:

```
hl7.py ── MLLP (port 6661) ──► Mirth channel ── HTTPS (port 4005) ──► HAPI FHIR
                                      │
                                      └─► Source transformer (JS): parse PID, normalize dates/gender
                                          Destination (HTTP Sender): PUT Patient?identifier=…
```

## Architecture and data flow

```
                              YOUR MAC (host)
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │                                                                              │
   │    $ python3 scripts/hl7.py send                      $ curl '...'           │
   │    ┌─────────────────────┐                            ┌─────────────────┐    │
   │    │   scripts/hl7.py    │                            │      curl       │    │
   │    │  builds HL7 v2 +    │                            │  HTTP client    │    │
   │    │  MLLP framing       │                            │                 │    │
   │    └──────────┬──────────┘                            └────────┬────────┘    │
   │               │                                                │             │
   │      ① MLLP/TCP                                          ⑦ HTTP GET          │
   │        localhost:6661                                      localhost:4005    │
   │               │                                                │             │
   │   ════════════╪════════════ Docker ═══════════════════════════╪════════════  │
   │               ▼                                                ▼             │
   │   ┌─────────────────────────────────┐         ┌──────────────────────────┐   │
   │   │  CONTAINER: mirth               │         │  CONTAINER: hapi-fhir    │   │
   │   │                                 │         │                          │   │
   │   │  ② TCP Listener (MLLP)          │         │  ⑥ Conditional update    │   │
   │   │     strip <VT>…<FS><CR>         │         │     ?identifier=…|MRN    │   │
   │   │     parse → msg['PID']…         │         │     →  match? update     │   │
   │   │                                 │         │        no?  create       │   │
   │   │  ③ Source Transformer (JS)      │         │     persist to H2 DB     │   │
   │   │     channelMap.put(MRN,…)       │         │                          │   │
   │   │     YYYYMMDD → YYYY-MM-DD       │         │  ⑧ Search                │   │
   │   │     M → male                    │         │     resourceType=Patient │   │
   │   │                                 │         │     family=Doe           │   │
   │   │  ④ HTTP Sender                  │         │     query H2, build      │   │
   │   │     expand ${PATIENT_MRN}…      │         │     searchset Bundle     │   │
   │   │     body = FHIR Patient JSON    │         │                          │   │
   │   │                                 │  ⑤ HTTPS PUT                       │   │
   │   │  PUT host.docker.internal:4005  │ ──────► │  HTTP 200/201 +          │   │
   │   │      /fhir/Patient?identifier=… │ ◄────── │  Location header         │   │
   │   │                                 │         │                          │   │
   │   │  Auto-build HL7 v2 ACK,         │         │                          │   │
   │   │  return over TCP socket         │         │                          │   │
   │   └─────────────────┬───────────────┘         └─────────────┬────────────┘   │
   │                     │                                       │                │
   │            ⓪ ACK back to hl7.py               ⑨ Bundle JSON to curl          │
   │                     │                                       │                │
   │              MSH|…|ACK|…|AA|             {"resourceType":"Bundle","total":1, │
   │              MSA|AA|MSG…                   "entry":[{"resource":{...}}]}     │
   │                                                                              │
   └──────────────────────────────────────────────────────────────────────────────┘
```

**Write path** (`python3 scripts/hl7.py send`):
1. `hl7.py` builds an `ADT^A01` HL7 v2 message, wraps it in MLLP framing bytes (`\x0b … \x1c\x0d`), opens a TCP socket to `localhost:6661`.
2. Mirth's TCP Listener strips the framing, parses the v2 message into a navigable `msg` object.
3. Source Transformer (JS) extracts PID fields, normalizes them (`YYYYMMDD` → `YYYY-MM-DD`, `M` → `male`), stashes them in `channelMap`.
4. HTTP Sender expands `${PATIENT_MRN}` etc. in the URL and JSON body templates.
5. Mirth issues `PUT http://host.docker.internal:4005/fhir/Patient?identifier=…` — leaves Docker via the host gateway and re-enters via the HAPI port mapping.
6. HAPI runs a FHIR conditional update: search for a Patient with that identifier, update if found / create if not, persist to H2.
7. (ACK return) Mirth auto-generates an HL7 v2 ACK (`MSH|…|ACK|…|AA|` + `MSA|AA|…`), returns it over the still-open TCP socket. `hl7.py` prints it.

**Read path** (`curl '…?family=Doe'`):
1. `curl` sends `GET /fhir/Patient?family=Doe` to `localhost:4005`, mapped to HAPI's port `8080` inside the container.
2. HAPI translates the FHIR search into an SQL query against the H2 index for `Patient.family`, builds a FHIR Bundle of type `searchset`, returns it as JSON.

### Things worth noticing in the topology

- The two flows touch HAPI through *different* network paths. The READ path is direct host-to-container via Docker port mapping. The WRITE path's last leg leaves the Mirth container, traverses `host.docker.internal`, comes *back* through the host port mapping into HAPI. That's the cost of using `host.docker.internal` to avoid coupling the two containers on a shared Docker network.
- The ACK is generated by **Mirth**, not by HAPI. `AA` (Application Accept) only means *Mirth* accepted the message — it does *not* mean HAPI successfully stored it. Production systems usually have Mirth's response transformer inspect HAPI's HTTP status and downgrade the ACK to `AE`/`AR` on failure; otherwise senders get false confirmations.
- The transformation happens in **one place** (Mirth) and in **one direction** (v2 → FHIR). There's no reverse path here. Real bidirectional integrations need a second channel or destination going FHIR → v2.

## Prerequisites

- **macOS** (steps for the Mirth admin client are Mac-specific; the Docker side is platform-agnostic)
- **Docker Desktop** running
- **Homebrew**
- **Python 3.8+** with [Faker](https://faker.readthedocs.io/) installed (the only third-party dep): `pip install -r requirements.txt`. Faker is used by `scripts/hl7.py` to generate fake patient details when you pass `--random`. The rest of the script is stdlib-only.
- A web browser, but **only briefly** — and not your normal one if it has cookies for `localhost`. Either use a fresh browser, an incognito window, or skip the browser entirely with the curl approach below.

### Docker Desktop sizing

On macOS, Docker Desktop runs containers inside a hard-allocated VM. The VM's size is a *reservation*, not a high-water mark — whatever you allocate is unavailable to the host even when containers are idle. For this two-container sandbox, **3 GiB is the sweet spot**: the actual containers use ~1.8 GiB combined (Mirth ~700 MiB, HAPI FHIR ~1 GiB), leaving ~1 GiB of headroom for buildkit and the linuxkit kernel. Anything larger just steals from the host's working set.

Docker whale icon → **Settings → Resources** → set **Memory** to ~3 GiB, then **Apply & Restart**. Verify with `docker info --format '{{.MemTotal}}' | awk '{printf "%.1f GiB\n", $1/1024/1024/1024}'`.

## Quick start with `docker compose`

The repo ships a `docker-compose.yml` that captures everything Steps 1 and 2 describe: both services, the right port mappings, `DATABASE=derby`, `VMOPTIONS=-Xmx2g,-Xms512m`, named volumes for Mirth state, and `restart: unless-stopped` so the stack survives Docker Desktop restarts.

```
cd /path/to/this/repo
docker compose up -d
```

That's the daily-driver invocation. Steps 1 and 2 below preserve the bare `docker run` commands the compose file is built from — keep them around as the *explanation* of why each port and env var is what it is, not as the way to launch.

```
docker compose ps                # see status
docker compose logs -f mirth     # tail server logs
docker compose stop              # stop without removing
docker compose down              # remove containers; named volumes survive
docker compose down -v           # nuke everything including channel state
```

## Step 1 — Start the HAPI FHIR server

```
docker run -d --name hapi-fhir \
  -p 4005:8080 \
  -e SERVER_MAX_HTTP_REQUEST_HEADER_SIZE=65536 \
  hapiproject/hapi:latest
```

The env var bumps Tomcat's request-header limit from its default 8 KB to 64 KB. Without it, browser-mediated requests to HAPI fail with `HTTP 400 Bad Request` once accumulated localhost cookies (from any dev server you've ever run) exceed 8 KB. HAPI is a Spring Boot app, so any Spring property can be overridden via env var by uppercasing and substituting `_` for `.` and `-`.

Wait ~30 seconds for it to boot, then verify:

```
curl -s http://localhost:4005/fhir/metadata | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['software']['name'], d['software']['version'], '/', d['fhirVersion'])"
```

Expected output: `HAPI FHIR Server 8.x.x / 4.0.1`.

## Step 2 — Start Mirth Connect

```
docker run -d --name mirth \
  -p 4006:8080 \
  -p 4443:8443 \
  -p 6661:6661 \
  -e DATABASE=derby \
  -e VMOPTIONS="-Xmx2g,-Xms512m" \
  nextgenhealthcare/connect:latest
```

`DATABASE=derby` uses the in-container embedded database. Channel definitions live there; if you `docker rm` the container, channels are lost. For learning, that's fine. For longer-lived setups, mount a volume or swap in Postgres. Below is an export/import pattern for moving channels across container recreates without a volume.

The `VMOPTIONS` env var is **critical for usability**. The image's default `-Xmx256m` is laughably small once the Swing admin client connects — the JVM GC-thrashes and the admin UI becomes painfully slow (15+ seconds per row click in the Message Browser). Bumping to 2 GB heap eliminates the lag. The entrypoint splits this value on commas and appends each token to `mcserver.vmoptions`; HotSpot uses the *last* `-Xmx` flag, so our `-Xmx2g` overrides the bundled `-Xmx256m`. Container memory limit (`docker stats`) is a separate, *outer* envelope — bumping that without bumping `-Xmx` has zero effect, because Java refuses to use more heap than its own cap regardless of how much the container offers.

Mirth takes 30–90 seconds to boot. Watch for readiness:

```
until curl -k -s -o /dev/null -w '%{http_code}\n' https://localhost:4443/api/users/_login | grep -q 200; do sleep 2; done
echo "Mirth REST API is up"
```

Confirm the MLLP TCP port is also bound (the listener won't have any application logic behind it until we deploy a channel, but the port itself should be reachable):

```
nc -z localhost 6661 && echo "MLLP port 6661 reachable"
```

## Step 3 — Install the Mirth Connect Administrator Launcher (one-time, Mac)

Mirth's admin GUI is a Java Swing application originally delivered as a Java Web Start app. Apple removed Java Web Start from macOS years ago. NextGen now ships a native install4j-packaged launcher that bundles its own JRE *and* JavaFX — which is what the Message Browser's content panes rely on for syntax highlighting. **This is the path this guide uses.** (Avoid the OpenWebStart Java Web Start launcher: on the machines this guide was built on it launched the client but then failed to connect to the server — and its bundled JRE lacks JavaFX, so the Message Browser content tabs render blank.)

The NextGen marketing page hides this behind a form; the direct, no-login source is the Mirth download archive. Check your CPU with `uname -m` and grab the matching build:

- **Apple Silicon (`arm64`):** <https://s3.us-east-1.amazonaws.com/downloads.mirthcorp.com/connect-client-launcher/mirth-administrator-launcher-latest-macos-aarch64.dmg>
- **Intel (`x86_64`):** <https://s3.us-east-1.amazonaws.com/downloads.mirthcorp.com/connect-client-launcher/mirth-administrator-launcher-latest-macos.dmg>
- Older/specific versions: the [MCAL download archive](https://mirthdownloadarchive.s3.amazonaws.com/mcal-downloads.html).

`latest` is currently 1.4.2 and bundles its own OpenJDK 17 — the runtime the 4.5.2 client needs. Downloading with `curl -L -o ~/Downloads/<file>.dmg <url>` instead of a browser avoids the Gatekeeper quarantine flag. Mount it, drag **Mirth Connect Administrator Launcher.app** into `/Applications/`. Verify:

```
ls "/Applications/Mirth Connect Administrator Launcher.app"
```

## Step 4 — Configure and launch the admin GUI

Open the launcher from LaunchPad. It presents a "Connections" list on the left and a settings panel on the right. Add a connection with:

- **Address:** `https://localhost:4443/webstart`
- **Java Home:** Bundled — **Java 17**
- **Max Heap Size:** `2g` — *critical for usability*. The launcher's default is 1 GB, and under heavy Message Browser scrolling the Swing client GC-thrashes. Bumping to 2 GB eliminates the lag.
- Everything else: leave at defaults (SSL Protocols Default, no Java Console).

Click **Save**, then **Launch**.

Alternatively, skip the Connections UI: right-click the repo's `mirth-administrator.jnlp` in Finder → **Open With → Mirth Connect Administrator Launcher**, and choose **Change All** so double-clicking it works thereafter. The launcher reads the server URL from the file. Either route reaches the same client.

What you should see, in order:

1. A certificate warning (self-signed cert on `localhost`). Click **Allow** / **Trust**, optionally tick "always trust".
2. The launcher downloads the client JARs from `https://localhost:4443/webstart/client-lib/` (~30 files, 10–20s first run, faster after — they cache).
3. The **Mirth Connect Administrator** Swing window opens at the login screen.
4. Login: `admin` / `admin`. Mirth forces a password change on first login — pick something memorable.

To verify heap is actually 2 GB, in another terminal:

```
jps -lvm | grep -i mirth
```

You should see `-Xmx2048m` (or `-Xmx2g`) in the args. The main class is `install4j.com.mirth.connect.client.launcher.ui.MirthWrapper com.mirth.connect.client.ui.Mirth …` — that's confirmation you're on the native launcher path.

If the launcher silently fails to appear, check System Settings → Privacy & Security for a Gatekeeper prompt waiting on you.

## Step 6 — Create the v2→FHIR channel

In the admin: **Channels** (left panel) → **New Channel** (toolbar).

### 6a. Summary tab
- **Name:** `HL7v2 ADT to FHIR Patient`
- **Description:** anything

### 6b. Source tab
- **Connector Type:** `TCP Listener`
- **Listener Address:** `0.0.0.0`
- **Listener Port:** `6661`
- **Receive Timeout:** `30000` (ms)
- **Transmission Mode:** `MLLP` — *critical*. This turns on the `\x0b … \x1c\x0d` framing bytes HL7 v2 senders expect. Leaving it on `Basic` makes Mirth ignore the framing and your messages get dropped.
- Open the **Set Data Types…** dialog. The button is in *three* places — pick whichever you find first: (a) the **Summary** tab, in the Channel Properties section, on the row labeled "Data Types"; (b) the **Channel menu** in the top menu bar; (c) the channel-editor toolbar. The dialog has one row per connector; set:
  - **Source Inbound:** `HL7 v2.x`
  - **Source Outbound:** `HL7 v2.x`
  - **Destination "POST Patient to HAPI" Inbound:** `HL7 v2.x`
  - **Destination "POST Patient to HAPI" Outbound:** `JSON`
  - These settings — message-level data types, not the transport-level `Data Type: Binary/Text` radio on the TCP Listener tab — are what make `msg['PID']['PID.5']['PID.5.1']` work in the JS transformer. If Inbound is left as `Raw`, the script gets a string blob and field access throws.
  - New HL7-themed channels often default to `HL7 v2.x` for the first three rows already; in that case just change Destination Outbound to `JSON`.

### 6c. Source transformer

**The source transformer is reached via the left sidebar, not via any tab.** Mirth's left **Channel Tasks** sidebar is *context-sensitive* — its entries change based on which tab is active:

| Tab active | What appears in Channel Tasks sidebar |
|---|---|
| Summary | Export / Debug / Deploy Channel (channel-level only) |
| **Source** | adds **Edit Filter**, **Edit Transformer**, Validate Connector, Import/Export Connector, etc. |
| Destinations | similar contextual additions, but scoped to the selected destination |
| Scripts | channel-lifecycle scripts (Deploy / Undeploy / Pre / Postprocessor) |

If you don't see "Edit Transformer" anywhere, you're almost certainly not on the Source tab. Click it first. Once you do, **Edit Transformer** appears in the sidebar — click it and the main pane swaps to the transformer editor.

> **The Scripts tab is not the transformer.** It holds channel-*lifecycle* scripts that run on channel start/stop (Deploy / Undeploy) and once per message at channel entry/exit (Preprocessor / Postprocessor). Mirth has multiple JavaScript scopes — Channel scripts, Source filter, **Source transformer** (what you want here), Destination filter, Destination transformer, Response transformer — each with different scope and different available variables. Picking the wrong one is a classic source of "why doesn't my variable have a value" bugs.

**If you've already deployed the channel without a transformer:** the channel is still fully editable. From the Dashboard, right-click the channel → **Edit Channel**, switch to the **Source** tab, click **Edit Transformer** in the sidebar, add the step, **Back to Channel**, **Save Changes**, then redeploy. Without a transformer the channel still ACKs incoming messages and fires HTTP requests at HAPI, but every `${PATIENT_MRN}`, `${LAST_NAME}`, etc. expands to an empty string — so HAPI receives a Patient resource with empty fields. This "the pipeline runs but the data is empty" failure mode is exactly what the Message Browser's Mappings tab is for diagnosing.

In the transformer editor:
- Click **Add New Step** → type **JavaScript**.
- Name it `Extract PID fields`.
- Paste this into the Script editor:

```javascript
// Mirth Connect — Source Transformer
// Extracts patient demographics from the PID segment, normalizes them into
// the shapes FHIR wants, and stashes them in channelMap for the destination.

channelMap.put('PATIENT_MRN', msg['PID']['PID.3']['PID.3.1'].toString());
channelMap.put('LAST_NAME',   msg['PID']['PID.5']['PID.5.1'].toString());
channelMap.put('FIRST_NAME',  msg['PID']['PID.5']['PID.5.2'].toString());

// v2 ships DOB as YYYYMMDD, FHIR wants YYYY-MM-DD.
var dob = msg['PID']['PID.7']['PID.7.1'].toString();
channelMap.put(
    'DOB_FHIR',
    dob.length === 8
        ? dob.substring(0, 4) + '-' + dob.substring(4, 6) + '-' + dob.substring(6, 8)
        : ''
);

// v2 gender code -> FHIR controlled vocabulary.
var v2Gender = msg['PID']['PID.8']['PID.8.1'].toString();
var fhirGender = ({ M: 'male', F: 'female', O: 'other', U: 'unknown' })[v2Gender] || 'unknown';
channelMap.put('FHIR_GENDER', fhirGender);

// PID-11 is a repeating field; take the first repetition.
var addr = msg['PID']['PID.11'][0] || msg['PID']['PID.11'];
channelMap.put('ADDR_LINE',  addr['PID.11.1'].toString());
channelMap.put('ADDR_CITY',  addr['PID.11.3'].toString());
channelMap.put('ADDR_STATE', addr['PID.11.4'].toString());
channelMap.put('ADDR_ZIP',   addr['PID.11.5'].toString());
```

To leave the transformer view, click **Back to Channel** in the Channel Tasks sidebar.

### 6d. Destinations tab
- Rename Destination 1 to `POST Patient to HAPI`.
- **Connector Type:** `HTTP Sender`
- **URL:** `http://host.docker.internal:4005/fhir/Patient?identifier=http%3A%2F%2Ftest-facility.example.org%2Fmrn%7C${PATIENT_MRN}`
  - `host.docker.internal` resolves to your host machine from inside Docker on Mac and Windows. On Linux, run Mirth with `--add-host host.docker.internal:host-gateway` or use the host bridge IP.
  - The `?identifier=…` query is FHIR's **conditional update** pattern — combined with `PUT`, it creates if no match, updates if one exists. Without it, every resend creates a duplicate Patient.
  - **The `:`, `/`, and `|` in the system URL must be URL-encoded** (`%3A`, `%2F`, `%7C`). FHIR's identifier search syntax is `system|value`, but Java's `java.net.URI` parser (which Mirth uses) is strict per RFC 3986 and rejects raw pipes/colons/slashes inside query values. Most other HTTP clients tolerate them silently; Mirth doesn't. The decoded form is `?identifier=http://test-facility.example.org/mrn|${PATIENT_MRN}`. The `${PATIENT_MRN}` value itself is URL-safe so it doesn't need encoding.
- **Method:** `PUT`
- **Headers:** add row `Content-Type` → `application/fhir+json`
- **Content section, Content-Type dropdown:** `application/json`
- **Content textarea:**

```json
{
  "resourceType": "Patient",
  "identifier": [
    {
      "system": "http://test-facility.example.org/mrn",
      "value": "${PATIENT_MRN}"
    }
  ],
  "name": [
    {
      "use": "official",
      "family": "${LAST_NAME}",
      "given": ["${FIRST_NAME}"]
    }
  ],
  "gender": "${FHIR_GENDER}",
  "birthDate": "${DOB_FHIR}",
  "address": [
    {
      "line": ["${ADDR_LINE}"],
      "city": "${ADDR_CITY}",
      "state": "${ADDR_STATE}",
      "postalCode": "${ADDR_ZIP}"
    }
  ]
}
```

The `${VAR}` placeholders are Mirth's template-replacement syntax; they pull values from `channelMap` at send time.

### 6e. Save and deploy
- File → **Save Changes** (`Cmd+S`).
- Right-click the channel in the Dashboard → **Deploy Channel**.

The MLLP listener on port 6661 is now active.

## Step 7 — The HL7 v2 sender CLI

The repo ships `scripts/hl7.py` — a single CLI that supersedes the older `sender.py` and `generate.py`. It composes three orthogonal axes:

| Axis | Flag | Values |
|---|---|---|
| Message type | `--message-type` (default `adt-a01`) | `adt-a01`, `adt-a03`, `adt-a08`, `oru-r01` |
| Patient source | `--patient` (default `canonical`) | `canonical` (fixed John Doe), `random` (Faker-generated; use `--locale` to vary names/addresses) |
| Error overlay | `--error` (default none) | 9 registered overlays in three groups: `bad-data`, `structure`, `transport` |

The only piece of v2 framing the script actually depends on is three magic bytes:

```python
SB = b"\x0b"   # MLLP start block (vertical tab)
EB = b"\x1c"   # MLLP end block (file separator)
CR = b"\x0d"   # carriage return
# every framed message on the wire is exactly:  SB + body + EB + CR
```

Everything else (segments, fields, identifiers, the FHIR conditional-update URL pattern) is built from string concatenation in `scripts/hl7.py`. Read the file once; it's ~350 lines and structured top-down (Patient → message builders → error overlays → CLI). Discover the CLI with:

```
python3 scripts/hl7.py            # prints help
python3 scripts/hl7.py list       # shows every message-type, patient source, and error overlay with one-line teaching descriptions
```

## Step 8 — Run the pipeline

```
python3 scripts/hl7.py send
```

You should see the sent message followed by an `MSH|…|ACK|…|AA|…` ACK from Mirth. Then verify the FHIR Patient landed in HAPI:

### Step 8b (optional) — Exercise the failure modes

Each `--error` overlay corresponds to a real-world integration bug. Apply one to any base message and watch where the failure surfaces:

```
python3 scripts/hl7.py send --error bad-date            # AA from Mirth, but birthDate is empty in HAPI (silent corruption)
python3 scripts/hl7.py send --error missing-mrn         # destination 4xx — empty identifier in the FHIR URL
python3 scripts/hl7.py send --error no-pid              # transformer throws — see Errors tab
python3 scripts/hl7.py send --error no-mllp-frame       # no ACK at all — Mirth doesn't even see the message
python3 scripts/hl7.py send --random --error pipe-in-name --message-type adt-a08  # composability: random patient, update event, name-corruption overlay
```

To populate Mirth's Message Browser with one example of each failure mode in a single pass:

```
python3 scripts/hl7.py send-all            # 9 overlays, default ~0.5s spacing
```

Expect roughly: Mirth ACKs every scenario `AA` (regardless of what HAPI does downstream); `missing-mrn` and `no-pid` fail at the destination; `no-mllp-frame` never reaches Mirth (no ACK); the remaining overlays land in HAPI with intentionally corrupt data. The educational value is in opening each message in the Message Browser and seeing **where** the failure surfaced — or didn't. The "silent corruption" cases (Mirth says `AA`, HAPI happily stores nonsense) are the most important to recognize, because in production they're the ones nobody notices for weeks.

```
curl -s 'http://localhost:4005/fhir/Patient?family=Doe' | python3 -m json.tool
```

Expected: a FHIR `Bundle` with at least one `Patient` entry whose `identifier.value` is `MRN-1001`.

Resend the same message: HAPI should *update* the existing Patient rather than create a duplicate, thanks to the conditional update in the destination URL. Verify with:

```
curl -s 'http://localhost:4005/fhir/Patient?identifier=http://test-facility.example.org/mrn|MRN-1001' | python3 -c "import json,sys; print('Patient count:', json.load(sys.stdin)['total'])"
```

The count should stay at `1` no matter how many times you resend.

## Troubleshooting (the issues we actually hit)

| Symptom | Root cause | Fix |
|---|---|---|
| HAPI: `HTTP 400 Bad Request` only from browser, fine from `curl` | Browser cookies for `localhost` share across all ports; combined cookie payload exceeds Tomcat's default 8 KB header limit. | `SERVER_MAX_HTTP_REQUEST_HEADER_SIZE=65536` env var on the HAPI container (already in Step 1). |
| Mirth: `HTTP 431 Request Header Fields Too Large` from browser at `https://localhost:4443/` | Same cookie problem; Jetty returns the proper 431. Mirth doesn't expose a config knob for Jetty's header limit. | Don't use a browser to launch the admin GUI — use the native launcher (Steps 3–4), which talks to the API directly with its own empty cookie store. (If you must open Mirth's web page, use a private window or clear `localhost` cookies first.) |
| Mirth admin "cannot reach server at localhost:8443" | A JNLP fetched fresh from the server embeds the container-*internal* port `8443`; from the host the admin API is published on `4443`. The `mirth-administrator.jnlp` shipped in this repo already uses `4443`. | If you re-fetched the JNLP yourself, change its `<argument>` line from `https://localhost:8443` to `https://localhost:4443`. |
| `scripts/hl7.py send`: connection refused | Channel not deployed, or Mirth not running. | Check Dashboard — channel state should be green/Started. Redeploy if needed. |
| `scripts/hl7.py send`: sends, no ACK, no Patient in HAPI | Transformer threw, or destination request failed. | In Mirth admin → Dashboard → right-click channel → **View Messages**. Open the failed message; the Errors tab shows the JS stack trace; the Destinations tab shows the HTTP response code from HAPI. |
| `scripts/hl7.py`: `error: the faker library is required.` | Faker isn't installed in the current Python environment. | `pip install -r requirements.txt`. Faker is the only third-party dep — the rest is stdlib. |
| FHIR returns 422 Unprocessable Entity | Resource didn't validate (e.g., empty `birthDate` with a profile constraint, bad gender code). | Look at the `OperationOutcome` body in the Mirth destination response. Adjust the transformer to coerce or omit the offending field. |
| Wrong field values land in HAPI | Transformer mis-mapped a PID component. | Same Message Browser → **Mappings** tab. Compare `channelMap` values against what you sent. |
| HAPI Patient lands with all fields empty (no name, no DOB, etc.) | Channel deployed without the source transformer — the destination's `${PATIENT_MRN}` etc. expanded to empty strings. | Edit the channel, add the JavaScript transformer (Step 6c), save, redeploy. Confirm via Message Browser → Mappings tab — populated `channelMap` is the success signal. |
| Can't find "Edit Transformer" anywhere | Mirth's left **Channel Tasks** sidebar is context-sensitive — Edit Transformer only appears when the **Source** tab is selected. Switching to Summary, Destinations, or Scripts hides it. | Click the Source tab first, then look in the left sidebar. |
| Destination errors `java.net.URISyntaxException: Illegal character in query at index N` | Raw `\|`, `:`, or `/` in the URL's query string. Java's `URI` parser is strict per RFC 3986; FHIR's `system\|value` identifier search syntax tripwires it. | URL-encode the literal characters: `:` → `%3A`, `/` → `%2F`, `\|` → `%7C`. Only encode the query value, not the `?identifier=` key itself or the `${PATIENT_MRN}` placeholder. |
| Double-clicking `mirth-administrator.jnlp` → *"Unable to locate a Java Runtime that supports javaws."* | `javaws` was removed from Java 11+, so a modern JDK has no Web Start; macOS falls back to its dead `/usr/bin/javaws` stub, which only prints this error. | Install the native launcher (Step 3) and open the `.jnlp` with it (right-click → **Open With → Mirth Connect Administrator Launcher** → **Change All**). The launcher supplies the Web Start runtime. OpenWebStart is **not** a workaround — it launches the client but then fails to connect to the server. |
| Mirth **server** in container is slow (channel ops, deploys take 15+ seconds) | Container JVM heap default is `-Xmx256m` which thrashes GC under the Swing client's REST chatter. The container has plenty of memory; the JVM just refuses to use it. | The compose file already sets `-e VMOPTIONS="-Xmx2g,-Xms512m"`. Verify with `docker exec mirth grep -E "^-Xm" /opt/connect/mcserver.vmoptions` — last `-Xmx` line should be `-Xmx2g` (HotSpot uses the last flag). |
| Mirth **admin client** is slow (UI lag, "stuck" feel on message clicks, sluggish scrolling) | Two separate causes, often confused. (a) Client JVM heap defaulted to 1 GB in the launcher's per-server config; (b) — and much more common in practice — *host* memory pressure forces macOS to swap the client JVM's working set to disk, making everything feel stuck. The containers themselves are usually idle when this happens. | (a) Launcher → Edit Server → **Max Heap Size** = `2g`. (b) Check swap with `sysctl vm.swapusage`; if `used` is several GB, close memory-hungry apps (browsers, Slack, idle terminals) and re-test. See "Performance and host resources" section below. |
| Containers don't auto-start after Docker Desktop restarts (VM resize, OS reboot, etc.) | Default `docker run` restart policy is `no`. Bare-command setups stay stopped. | The compose file uses `restart: unless-stopped`, which fixes this. If you started with `docker run`, recreate via `docker compose up -d`. |
| Mirth channel disappears when you `docker rm` the container | Channels live in in-container Derby; no volume = no persistence. | Use the REST API to export channels to XML before recreating, then re-import. See "Preserving channels across recreates" below. The compose file mounts named volumes for `appdata`/`custom-extensions`, so `docker compose down` (without `-v`) preserves deployed channels; `docker compose down -v` wipes them. |

The **Message Browser** is the single most important troubleshooting tool — any real-world HL7 integration problem you'll ever have to debug shows up here. Spend time clicking around in it.

## Conceptual notes you'll want to remember

- **HAPI is two completely different things.** `hapiproject/hapi` is **HAPI FHIR** (a FHIR R4/R5 REST server). **HAPI HL7v2** is an unrelated Java library for parsing/writing v2 messages, typically embedded inside integration code. Same project family, different software. Don't conflate them — many blog posts do.
- **HL7 v2 vs FHIR.** HL7 v2 (1989+) is pipe-delimited messaging over MLLP/TCP. FHIR (2014+) is REST/JSON resources over HTTP. Hospitals run both side-by-side; "integration troubleshooting" usually means making v2 traffic produce correct FHIR resources, which is why this testbed exists.
- **MLLP framing.** Every HL7 v2 message on the wire is bracketed by start (`\x0b`), end (`\x1c`), and carriage return (`\x0d`). If you write a custom sender or receiver, this is the #1 source of "messages don't arrive" bugs.
- **The integration engine pattern.** Listener → filter → transformer → destination. Mirth, Rhapsody, InterSystems IRIS, Cloverleaf, NextGen Connect all use this shape. Learn it once, port your skills anywhere.

## Preserving channels across recreates

Because the channel lives in in-container Derby, anything that removes the container (image upgrade, JVM tuning, port changes) wipes channels. The fix is the export/import workflow via Mirth's REST API — no volume needed, and the resulting XML is a useful versioned artifact to keep alongside the rest of the repo.

**Export** (run before stopping/removing the container):

```bash
cookie=$(mktemp)
curl -k -s -c "$cookie" -X POST "https://localhost:4443/api/users/_login" \
  -H "X-Requested-With: curl" \
  -d "username=admin&password=YOUR_PASSWORD" > /dev/null

# List channel ids
curl -k -s -b "$cookie" -H "X-Requested-With: curl" -H "Accept: application/json" \
  "https://localhost:4443/api/channels" | python3 -m json.tool

# Export one channel by id
CHANNEL_ID=03dab84b-8685-4782-82ad-74ab00e9d9d8
mkdir -p mirth/channels
curl -k -s -b "$cookie" -H "X-Requested-With: curl" -H "Accept: application/xml" \
  "https://localhost:4443/api/channels/$CHANNEL_ID" \
  -o mirth/channels/hl7v2-adt-to-fhir-patient.xml
```

**Import + deploy** (after recreating the container; first login may be forced to `admin/admin` then change the password back):

```bash
cookie=$(mktemp)
curl -k -s -c "$cookie" -X POST "https://localhost:4443/api/users/_login" \
  -H "X-Requested-With: curl" \
  -d "username=admin&password=YOUR_PASSWORD" > /dev/null

curl -k -s -b "$cookie" -X POST "https://localhost:4443/api/channels" \
  -H "X-Requested-With: curl" -H "Content-Type: application/xml" \
  --data-binary @mirth/channels/hl7v2-adt-to-fhir-patient.xml

curl -k -s -b "$cookie" -X POST "https://localhost:4443/api/channels/_deploy" \
  -H "X-Requested-With: curl" -H "Content-Type: application/json" \
  -d "{\"set\":{\"string\":[\"$CHANNEL_ID\"]}}"
```

After import you'll need to reconnect the Swing admin client (it was bound to the old container's session); close and re-launch via the JNLP.

## Performance and host resources

By the time this testbed is humming, you have:

- A 2.8–3 GiB Docker VM running two JVM containers (Mirth ~700 MiB, HAPI ~1 GiB resident)
- A second JVM on the host running the Mirth Administrator Swing client (~700 MiB resident with `-Xmx2g`)
- Whatever else the host is doing (browsers, IDE, terminals, Slack, …)

When the admin UI starts feeling "stuck," **the bottleneck is almost never inside Docker**. `docker stats` will show both containers at low single-digit CPU and stable memory. The real culprits, in observed order of frequency:

1. **Host swap pressure.** macOS pages working sets of idle apps to disk. Once swap is engaged, *any* touch of a swapped page costs a disk round trip. The Mirth client polls the server every few seconds, so its working set gets touched constantly — if part of it is on disk, the UI hitches on every poll. Diagnose:
   ```
   sysctl vm.swapusage
   ```
   `used > 2 GB` → close memory-hungry apps (browsers especially — Chrome/Brave aggregated across helper processes is usually the top offender) and re-test.

2. **Docker Desktop VM oversized.** macOS Docker Desktop allocates a hard memory reservation to its VM. Anything you allocate is unavailable to the host even when containers are idle. For this sandbox, 3 GiB is plenty; 7+ GiB starves the host for no benefit.

3. **Admin client heap too small.** The native launcher defaults its per-server config to 1 GB max heap. Under sustained Message Browser use (lots of messages, lots of clicking), that's tight. Bump to 2 GiB.

4. **Server heap too small.** The container image's default `-Xmx256m` is the original 2024-era complaint that prompted the `VMOPTIONS` env var. The compose file already overrides it; verify with `docker exec mirth grep -E '^-Xm' /opt/connect/mcserver.vmoptions`.

Quick triage:

```
docker stats --no-stream                # both containers should be idle <5% CPU
sysctl vm.swapusage                     # ideally used near 0
jps -lvm | grep -i mirth                # confirm client -Xmx2g/-Xmx2048m
```

The educational point is that "JVM tuning" and "host memory hygiene" are *separate* layers — and in a desktop dev environment, the host layer dominates.

## Teardown

```
docker compose stop                     # stop both, keep state
docker compose down                     # remove containers; named volumes survive
docker compose down -v                  # nuke everything including Mirth channel state
```

To start over from scratch on the same machine, run `docker compose up -d` and then re-import the channel from `mirth/channels/hl7v2-adt-to-fhir-patient.xml` via the admin client (Channels → Import Channel) — or via the REST API workflow under "Preserving channels across recreates" above.
