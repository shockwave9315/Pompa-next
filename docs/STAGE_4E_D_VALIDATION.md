# Stage 4E-D — CT109 validation contract and evidence plan

## 1. Authority, candidate and execution boundary

**4E-D-A: IMPLEMENTED / AWAITING OWNER FINAL REVIEW.** This document defines production validation;
no phase below has been executed in this checkpoint. Independent review and owner acceptance
of this document precede generation of the 4E-D-B execution runbook. CT109 validation is
**NOT STARTED**. Stage 4E-C is **OWNER ACCEPTED/CLOSED** at:

`fa4d49e233927210260595143a0de01f12f05399`

That exact SHA is the **candidate under test**. A documentation-only checkpoint does not
silently replace it with the branch tip. Any replacement candidate needs an explicit reviewed
SHA and owner decision before deployment; record both candidate and accepted plan revision.
The runtime candidate stays `fa4d49e233927210260595143a0de01f12f05399`; the validation-plan
revision is the separate documentation commit containing the owner-accepted version of this
file. 4E-D-B must consume both identities, never deploy the plan revision implicitly.

This is the authoritative **validation procedure** for Stage 4E-D. Product semantics remain
in [ARCHITECTURE.md §25.5](ARCHITECTURE.md#255-stage-4e--isolated-control-and-final-backend-api),
[API.md Stage 4E](API.md#stage-4e-control-frozen-in-4e-a-implemented-in-4e-c) and
`backend/pompa/control.py`. Procedural skips below are owner validation constraints, not new
API prerequisites, ranges or safety metadata. A contract contradiction stops execution and
returns to analysis; it does not authorize a runtime redesign.

The remaining purposes are:

1. Deploy the exact reviewed candidate to CT109.
2. Sample the real control API/write path on the owner's HeishaMon/K-series installation.
3. Measure readback latency and decide whether provisional W = 15 s changes.
4. Freeze the complete backend API and collect evidence for whole-stage review and merge.

This is not exhaustive real-device execution of all commands, qualification of protocol ranges,
command history, HA reconciliation, retry/replay, Optional PCB commissioning or frontend work.
Local 4E-B/C protocol, encoding, transport and adversarial tests remain evidence for unexercised
controls. No production contact, credentials, deploy, command or executable CT109 runbook is
authorized in 4E-D-A. The later owner performs approved actions; scripts never sweep the matrix.

## 2. Known context versus MUST OBSERVE BEFORE WRITE

| Known project context | Value |
|---|---|
| Runtime checkout / Compose project | `/opt/pompa-next` / `pompa-next` |
| Backend API port | 8001 |
| MariaDB target family | 11.4 |
| Stage 4E schema migration | None; existing recorder schema initialization remains unchanged |
| MQTT command client | Existing shared paho client/connection; QoS 0, retain=false |
| Provisional W | 15 s; module constant, not a runtime setting |
| Controls / capabilities / readable physical slots | 63 / 218 / 157 |

**MUST OBSERVE BEFORE WRITE:** deployed SHA and running source identity; container count, images,
health and mounts; actual DB version and volume; configuration identity; actual MQTT host,
prefix and client id (privately); firmware; MQTT connected/alive/LWT/clock facts; current TOPs,
activity, all selected controls' live schemas and prerequisites; HA retained commands and
other writers; actual operating conditions. Development snapshots, examples and earlier CT109
reports do not establish today's values. In particular, do not assume TOP110=0, TOP76=0 or any
particular TOP4 mode. Re-observe relevant facts immediately before each mutation and restore.

## 3. Phases and owner gates

| Phase | Purpose | Exit evidence / gate |
|---|---|---|
| 0 | Deployment and identity, no intentional command publication | Exact running identity, rollback point, preserved configuration/data, startup/health proof |
| 1 | Read-only pre-check, zero control POSTs or MQTT publications | Owner accepts Phase 0/1 evidence: **OWNER GATE 1** |
| 2 | Selected safe reversible candidates, one deliberate action at a time | Changes/equal-value probes/restores accounted for; **OWNER GATE 2** |
| 3 | Selected state-changing but restorable candidates | Separate owner approval per test and current conditions; dedicated operation-mode gate |
| 4 | Latency analysis, W decision and final API freeze criteria | No new command class; accepted evidence and explicit owner W decision |
| 5 | Whole Stage 4E review and closeout | Independent reviews and owner merge decision |

Acceptance of this plan allows later runbook preparation, not unattended write execution.
Gate 1 names the Phase 2 subset and proposed values/restores. Gate 2 reviews every outcome,
restore and anomaly before approving any Phase 3 subset. Before every individual POST, including
an equal-value probe or restore, the owner confirms the captured current facts and exact semantic
value. No concurrent tests, automatic matrix loop, retries or automatic retained cleanup.

### Phase 0 — deployment and identity

Before activation the owner captures:

- Existing checkout SHA, running backend image id/digest, service/container status and health.
- Existing DB container/image/version, mount identity and the actual `pompa-next_db-data` volume.
  Verify its mount really belongs to this Compose project.
- Existing `.env` presence, permissions and checksum, Compose configuration/source identity and
  private confirmation of MQTT/database configuration. Do not print `.env`, credentials or a
  full environment dump into evidence. Preserve the MQTT client id and all existing settings.
- Bounded recent backend logs, recorder/database status, oldest/newest minute and rolled state.
  Save one small immutable, fully closed historical query with exact URL, response bytes and
  checksum; choose an existing representable rolled interval so normal raw purge cannot change it.
- **An owner-created, usable Proxmox CT109 rollback/snapshot point before activation.** Use the
  owner's existing Proxmox workflow; record node/CT identity, snapshot identifier/time, included
  rootfs/mounts and confirmation that the database volume/configuration are covered, plus how
  the owner restores it. The repository does not establish a named snapshot command or storage
  layout; those must be observed. If coverage or usability cannot be confirmed, do not deploy.

Deployment targets `/opt/pompa-next` only. Never touch `/opt/pompa`, delete/recreate `db-data`,
use `docker compose down -v`, replace `.env` with an example, change MQTT client id or start a
second backend. Fetch and select the exact candidate SHA, require a clean tracked checkout,
build/update with `docker compose up -d --build`, preserving existing data/configuration. Capture
build exit result and image identity. Keep checked-out source readable by the image's UID 10001;
do not apply a restrictive global umask to source files (the Stage 4B deployment lesson).

After deployment prove checkout SHA **and running source identity**, not merely image tag:
compare hashes of the complete packaged `pompa` Python tree, `/app/requirements.txt` and
all three packaged HeishaMon references against the candidate checkout inputs. This includes
`control.py`, `control_runtime.py`, `api.py`, `mqtt.py`, `ingest.py`, `recorder.py` and `main.py`,
not only those selected modules. Record all mismatches. Verify exactly one running backend,
Compose service status, health 200, normal MQTT
connection and startup logs without unexplained traceback, DB/schema or MQTT failure.

Re-check volume/configuration identity and preserved historical response bytes. Oldest/newest
and rolled frontiers may advance normally; investigate unexplained loss, not ordinary retention.
Recorder process counters reset on restart. Retained-only restart time remains unrecorded and
may leave an expected gap; do not fabricate history or treat that gap alone as a defect.

Deployment issues no control POST or intentional command publication. The existing startup path
only subscribes and records. A software/Proxmox rollback does **not** restore heat-pump settings;
post-snapshot historical minutes may also be lost on snapshot rollback. Device restore evidence
and the owner's rollback decision are separate.

#### Application INFO-log availability (Phase 0/1 precondition)

`control_runtime.log_request()` emits INFO; `config.py` also accepts WARNING and ERROR, which
suppress it. Prove application INFO actually reaches the captured backend logs, for example the
current process's `INFO pompa: process start ...` startup line from `main.py` (correlate its time
with `/status.recorder.process_start`). A generic HTTP access line or the configured string
alone is insufficient. Capture effective LOG_LEVEL privately without printing `.env` or secrets.

If INFO is suppressed or its emission cannot be established, **Gate 1 cannot approve writes**.
The owner must explicitly choose either a temporary LOG_LEVEL=INFO configuration change, or
stop and obtain a reviewed plan correction for the evidence requirement. No automatic waiver
or silent `.env` edit is authorized. An approved change records the original setting (including
unset/default), the exact change and owner approval, restarts only the required backend,
preserves candidate SHA/configuration/data, and repeats running identity, health, MQTT and all
Phase 1 read-only checks, including the retained audit, for the new process before Gate 1.
Restoration of the original log level after validation is a separate owner decision, recorded
with any resulting controlled restart and affected read-only checks. No such action occurs here.
Every real POST still requires its one structured control log; loss of log availability stops
further writes.

### Phase 1 — read-only pre-check

Save timestamped status codes and response bodies for:

- `/health`, `/api/v1/status`, `/api/v1/live`, `/api/v1/live?include=readings`;
- `/api/v1/metrics`, `/api/v1/metrics?include=capabilities`, `/api/v1/activity/live`;
- `/api/v1/controls`: 63 unique controls, `readback_window_seconds=15`, `mqtt.connected`, each
  selected entry's identity/class/service/value schema, state, prerequisites, restrictions,
  executable and reasons. Check 218 capability entries and 157 physical reading slots; those
  counts do not imply that every slot has a live receipt.
- Small representative recent `/history` and `/activity` ranges and the immutable historical
  query from Phase 0. Read existing optional-history selection/series without PUT; use a small
  optional query only if an already-persisted series exists. Do not change selection.
- One day `/report` query, using the Warsaw date derived from `/status.now`, not host timezone.
  Record period, observation and coverage partitions; use the existing smoke where practical.
  If a report cannot be obtained, record the cause and resolve it before Gate 1 rather than
  silently dropping the check. No large historical scans are required.

Require observation of **TOP44, TOP110, TOP4, TOP76, TOP81**, plus every readback, prerequisite
and context TOP of each chosen candidate. Capture raw, semantic value where defined, mode,
available and receipt time. For an intended write, the relevant baseline and restoration value
must be live, available, mapped and inside the live schema. Retained/stale/unknown state is not
a usable original value. GET `executable=true` alone does not prove that condition.

Save `/status` MQTT connected/alive, epoch/connect/disconnect counters, LWT, last live time,
clock-step facts, recorder queue/error facts and database status. Observe the actual HeishaMon
firmware/version from existing `{prefix}/stats` JSON, preserving raw JSON, receipt time and retain
flag privately. Pompa Next has no stats/version endpoint; do not invent one. If the version field
is absent/unrecognizable, record unknown and resolve with the owner before writes. The pinned
v4.2.2 development reference is not proof of installed firmware. Capture publication cadence
from stats if exposed, otherwise from passive TOP receipts; do not change HeishaMon settings.

#### Mandatory retained-command and external-writer audit

Before writes use a read-only subscriber with a unique client id, never the backend's or HA's id,
on the privately observed `{prefix}/commands/#`. It must publish nothing, including no LWT.
Record successful connection, SUBACK/result, audit start/end, retain flags and **all** received
`commands/#` topics/payloads, identifying retained deliveries separately. The bounded window is
at least 5 s after SUBACK. **SUBACK alone is not proof of complete read visibility**: ACL/broker
behavior can limit delivery despite subscription acceptance; no probe publication or destructive
permission test is allowed.

Cross-check with `/status.mqtt.uncatalogued_topics` after backend startup and at audit end.
The backend subscribes to `{prefix}/#`; ingest records relative `commands/...` names there.
Normalize the passive observer's prefix before comparing command-topic sets. Save the full
status list and observation times. This is a process-lifetime set of names, capped at 500 total
uncatalogued topics; it has no payloads, retain flags or per-topic timestamps. Older backend-only
names need explanation, not an assertion that those commands are still retained. A saturated
set or missing/misaligned observation weakens the cross-check.

Agreement strengthens the bounded audit; disagreement makes visibility **UNKNOWN** until owner
resolution at Gate 1. Zero/zero remains bounded absence, not proof that no writer can publish
later. Unresolved ACL/broker behavior or cross-check limitations cannot be treated as a complete
audit. Do not publish a probe to overcome the uncertainty.

For each retained topic record full topic, payload, candidate catalog command/semantic control
mapping, apparent or owner-confirmed HA ownership and overlap with a planned target. Unknown
ownership stays unknown. `SetCurves` is shared by four controls: inspect the retained partial JSON
for zone/mode/field overlap; unreadable JSON leaves overlap unresolved. Also record unmapped or
mistyped topics rather than assigning them a working control. The pinned HA integration's
SET37/38 names differ from `SetBivalentAPStartTemp`/`SetBivalentAPStopTemp`; inspect actual topics.

**No automatic cleanup.** No retained payload is cleared or modified without a separate owner
approval; this plan provides no cleanup action. Absence during the audit is a bounded fact, not
proof that HA will never publish later. Keep a passive observer during each approved mutation
and restore; distinguish retained startup deliveries from new forwarded publications.
Re-audit retained state immediately before every POST (including restore) and after the
post-restore window. A forwarded retain flag alone does not reveal stored retained state;
re-audit any overlapping command traffic whose retention is unclear.

**Approval invalidation after Gate 1:** a new retained command or changed retained payload
that overlaps a still-planned control invalidates that control's approval. STOP testing it,
re-audit payload/current TOP/schema/context/prerequisites/HA and external writers, then require
explicit owner re-approval before any mutation. Do not automatically update the approved value
or continue because the retained payload equals the planned restore. An unresolved overlap or
a failed re-audit likewise cannot authorize the next POST.

**OWNER GATE 1:** owner reviews identity, health, INFO-log emission, firmware, schemas,
TOP44/activity, restoration values, retained visibility/conflicts and the §5 post-restore window.
Approve a specific Phase 2 subset, safe alternate choices and restore actions, or resolve/skip affected rows. No write begins before that decision.

## 4. Shared real-write protocol and selection rules

The categories below exhaust all 63 definitions: **A=10, B=7, C=46**. They classify validation
sampling, not API executability. Being a candidate does not mean its full accepted range is safe
on this installation. `range_basis=protocol` proves encodability, not a device operating limit.

Every A/B row inherits these requirements:

1. Capture its complete GET entry, live original semantic value/raw TOP/receipt, schema,
   prerequisite/context facts, retained audit, TOP44 and `/activity/live` immediately before write.
   Confirm owner-approved consequences and an exact original restoration value before mutation.
2. Present the exact §4 current-condition fact sets and evaluate preferred/fallback directions
   separately. Validate both original and selected alternate against the current schema. Never clamp,
   substitute a default, reinterpret a context or select an undocumented enum. Skip if either
   value is unmapped/out of schema or a small safe alternate is not obvious to the owner.
3. One deliberately approved semantic POST; save full request/response, transport status and
   the single structured backend request log. Expected change response: HTTP 200, `sent`,
   normally `matched` to the selected value. Timeout/mismatch still returns factual 200;
   `unchanged_match` on an intended different request needs analysis, not forced success.
4. Capture fresh read-only state, TOP44 and activity after response. Never issue the next item
   while unresolved. If needed, only the passive extension in §7 is allowed.
5. One separately confirmed restore POST with the **exact captured original semantic value**
   (curve: only the original requested field). Save restore response and fresh final TOP state,
   TOP44/activity and retained/external-writer observations. A restore may be `matched`, or
   `unchanged_match` if the original was already restored with continuous evidence. Final live
   state must independently equal the original; a `sent` result alone is never restore proof.
   Complete the read-only post-restore window in §5 before marking the test complete. An equal-value
   probe with no restore uses the same final-state observation window after its response.
6. The owner accounts for every changed setting and approves proceeding. Skips/aborts are evidence,
   not permission to fill the sample count with another command automatically.

Selection notation used by the tables:

- **I+**: original integer +1 if within the observed bounds; otherwise original −1 if valid.
- **I−**: original integer −1 if within the observed bounds; otherwise original +1 if valid.
- **E**: the other member of the stated two-value enum.
- Each rule additionally needs the owner's current-condition safety check. If its proposed step
  would cross an active operating threshold or create an unwanted consequence, skip; no extra
  target is invented. Evaluate the preferred direction and the schema-forced fallback independently
  for every A/B ±1 rule: a valid fallback is not automatically acceptable. Present both candidates
  (mark any out-of-schema direction invalid) and record the owner's consequence assessment.
  If current facts do not make the selected change clearly acceptable: **SKIP**. Do not switch
  to the other direction merely to bypass an unsafe preferred step. Schema validity alone is
  insufficient. The original and alternate use JSON integer/boolean/enum types exactly.

#### Required current-condition fact sets

Collect the following together from `/live?include=readings` immediately before selection and
again before restore, in addition to TOP44, `/activity/live`, context/prerequisite and original
readback evidence. Show raw/value, mode, available and received_at; an unavailable required fact
cannot silently be replaced by an assumed value. Missing/unclear context means SKIP until resolved.
These are procedural observations, not added API prerequisites or backend safety mathematics.

- **SET20 dhw_heat_delta and SET11 dhw_target_temperature:** TOP9 `DHW_Target_Temp` (target),
  TOP10 `DHW_Temp` (actual tank temperature), TOP22 `DHW_Heat_Delta` (current negative heating
  delta), and shared TOP4/current activity. Present target, actual and delta together with both
  proposed ±1 alternatives. Owner evaluates whether each alters present DHW reheat/trigger
  eligibility. Assess SET20's preferred +1 and fallback −1 and SET11's preferred −1 and
  fallback +1 independently; none is safe solely by schema.
  The tracked reference does not specify a complete DHW hysteresis algorithm; do not invent
  an exact trigger equation or use these facts to guarantee cycle behavior.
- **SET46 heater_on_outdoor_temperature:** TOP14 `Outside_Temp`; TOP78
  `Heater_On_Outdoor_Temp` (below this threshold backup heat is allowed by heating logic);
  TOP59 `Room_Heater_State` (permission: 0=disabled, 1=enabled); TOP60
  `Internal_Heater_State` (actual internal backup activity: 0=inactive, 1=active); TOP61
  `External_Heater_State` (actual external backup/booster activity: 0=inactive, 1=active).
  Permission is not activity. Keep I− preferred; if original is the lower bound and forces +1,
  owner evaluates whether raising the threshold changes present heater eligibility, using all
  five facts plus shared TOP4/activity. Do not equate eligibility with heater activation.
- **SET18 heat_delta:** TOP23 `Heat_Delta`; TOP1 `Pump_Flow` (l/min); TOP5 `Main_Inlet_Temp`,
  TOP6 `Main_Outlet_Temp`, TOP7 `Main_Target_Temp` (water inlet/outlet/target, °C); TOP8
  `Compressor_Freq` (Hz); TOP4 `Operating_Mode_State`; TOP14 `Outside_Temp`; TOP76 `Heating_Mode`
  (compensation/direct); TOP94 `Zones_State`, together with `/activity/live`. This fixed set
  shows current water conditions, flow, target, compressor activity and heating context for
  the ±1 K judgment. The reference documents floor heating delta but no exact controller or
  safe-threshold formula. Do not derive a safe range or a guaranteed outcome from water delta.
  Owner assesses both preferred +1 and fallback −1; unresolved consequences mean SKIP.

The existing row-specific facts remain required for SET29 (TOP14/TOP77), bivalent thresholds
(TOP129/TOP130 plus target TOP), curves (full TOP29–32) and requests (TOP27/TOP76). The independent
preferred/fallback assessment applies to them too; the runbook never substitutes a safety rule.

### A — SAFE REVERSIBLE VALIDATION CANDIDATE (Phase 2)

All ten are `setting`, `service=false`, state readback, with **no API prerequisite or context**.
Every row uses the shared initial evidence, outcome, exact restore and abort rules above.
HA audit applies to **every** row; the notes identify particular known conflicts, not an allowlist.

| Control key | Identity | Class | Readback | Value schema | Alternate | Additional phase condition / HA consideration |
|---|---|---|---|---|---|---|
| `quiet_mode_priority` | SET41 | setting | TOP141 | enum: sound, capacity | E | Confirm a brief priority change is acceptable; audit actual HA writes |
| `heating_control` | SET39 | setting | TOP139 | enum: comfort, efficiency | E | Brief control-policy change approved in current activity |
| `smart_dhw` | SET40 | setting | TOP140 | enum: variable, standard | E | Brief DHW-policy change approved; do not force a cycle |
| `heat_delta` | SET18 | setting | TOP23 | integer 1..15 K | I+ | Collect the SET18 fact set in §4; assess both ±1 directions; no undocumented safety formula |
| `dhw_heat_delta` | SET20 | setting | TOP22 | integer -12..-2 K | I+ | Collect TOP9/TOP10/TOP22 together; assess both ±1 directions against current DHW demand |
| `heating_off_outdoor_temperature` | SET29 | setting | TOP77 | integer 5..35 °C | I+ | Observe current outside TOP14; do not cross an active heating cutoff |
| `heater_on_outdoor_temperature` | SET46 | setting | TOP78 | integer -15..20 °C | I− | Collect TOP14/TOP78/TOP59/TOP60/TOP61; distinguish permission/activity; independently approve fallback +1 eligibility; no owner HA-write evidence assumed |
| `bivalent_start_temperature` | SET36 | setting | TOP131 | integer -15..35 °C | I− | Require live TOP129=0 (bivalent off); observe TOP130; HA may retain SetBivalentStartTemp |
| `bivalent_advanced_start_temperature` | SET37 | setting | TOP134 | integer -15..35 °C | I− | Require live TOP129=0; capture TOP130; actual correct AP command versus HA's historical wrong name |
| `bivalent_advanced_stop_temperature` | SET38 | setting | TOP135 | integer -15..35 °C | I− | Require live TOP129=0; capture TOP130; actual correct AP command versus HA's historical wrong name |

TOP129/130 and threshold watches are **procedure conditions**, not added `control.py`
prerequisites. Do not turn bivalent off or change another installer setting just to qualify a test.
Each row aborts on §8, loss of its usable original/readback or inability to prove exact restore.
SET46 is the explicit A candidate; SET43–45 remain excluded below.

#### Equal-to-current probes

After Gate 1, use at least one approved A scalar (for example one of the three enums), requesting
its freshly captured current semantic value. Optionally use a Phase 3 single-field curve only
with that phase's approval. No temporary or service trigger is added for this purpose.

With the whole requested baseline still matching and no contradiction or continuity loss,
expect `unchanged_match`, observing the **full W** and recording `waited_seconds` (approximately
15 s or longer due to scheduling, not a brittle upper-time threshold). No restore is needed if
state remained original. If it changed, stop and obtain an owner restoration decision.

Capture TOP receipt times before/during/after and any naturally occurring same-value publication.
A same-value republish must not upgrade a continuously unchanged request to `matched`. If none
occurs within W, say so: real evidence covers the unchanged baseline/full wait; the republish
precedence remains additionally proved by committed local regressions. Do not manufacture TOPs,
change firmware cadence or step clocks to force coverage. Exact baseline object identity and
backward-clock attribution are local implementation proofs; the production probe tests their
observable contract without pretending to expose the private object identity.

A genuine different→expected transition or continuity recovery can legitimately produce
`matched`; record the observations and investigate rather than treating it as an unchanged
success. `unchanged_match` is **never** a latency sample.

**OWNER GATE 2:** accept Phase 2 outcomes, equal-value evidence, exact restores and absence of
unresolved anomalies. Only then approve selected Phase 3 tests using current operating conditions.

### B — STATE-CHANGING BUT RESTORABLE (Phase 3)

All seven have `service=false`. Shared TOP44/activity watches run before, after and after restore.
Expected changed readback is the requested semantic value, not proof of physical actuation.

| Control key | Identity | Class | Readback | Value schema | API prerequisite / context | Selection, impact, restore and skip |
|---|---|---|---|---|---|---|
| `dhw_target_temperature` | SET11 | setting | TOP9 | integer 40..75 °C | None | Prefer I− (±1 °C inside live schema); may change DHW demand. Present TOP9/TOP10/TOP22 together; independently evaluate preferred −1 and fallback +1 against current DHW demand. Restore exact original; skip an unsafe/unknown target |
| `zone1_heat_curve` | SET16 | curve | TOP29,TOP30,TOP31,TOP32 | curve fields integer -127..127 °C, protocol | None | Capture full curve; change only outside_low at TOP32 using I+. Preserve other fields, restore only exact original outside_low. May change heating target; skip unclear curve meaning or unsafe current thermal consequence |
| `zone1_heat_request` | SET5 | setting | TOP27 | request_temperature: shift -5..5 K; direct 20..127 °C, protocol | Context TOP76 | Observe active shift/direct from live TOP76 (0/1). Use I+ only in that active range and if small safe change is obvious. Never change context for testing. Direct water-sensor restriction is informational, not enforced. Restore exact original under unchanged context; otherwise stop |
| `quiet_mode` | SET3 | setting | TOP18 | enum: off, level_1, level_2, level_3 | None | Owner approves a brief neighboring level: off↔level_1, level_2→level_1, level_3→level_2. May alter capacity/noise. Restore exact original, not an assumed off; skip invalid state or unsafe capacity reduction |
| `powerful_mode` | SET4 | temporary | TOP17 | enum: off, min_30, min_60, min_90 | None | Only original off: owner-approved brief min_30, then restore off. May boost operation; skip already-active timer or unsafe conditions. Restoring an active original would re-arm its duration, so do not test it |
| `force_dhw` | SET10 | temporary | TOP2 | boolean | dhw_operation_mode: TOP4 in {3,4,5,6,8} | Require satisfied=true and original false; owner approves true, restore false. May start/prioritize DHW. False deasserts the request, NOT a stop/cancel of an already-running DHW cycle; require acceptance of natural completion. Skip unknown/non-DHW mode, already true or unacceptable cycle consequences |
| `operation_mode` | SET9 | setting | TOP4 | enum: heat, cool, auto, dhw, heat_dhw, cool_dhw, auto_dhw | None | Dedicated owner gate chooses alternate after seeing TOP4/activity/conditions. No algorithmic target. Restore original semantic mode; raw auto 2/7 and auto_dhw 6/8 may differ without changing semantic mode. Skip if consequences or restoration are unclear |

For the curve, only TOP32 is requested readback; the other fields are captured/watched, not
required to arrive in one packet or treated as requested matches. The protocol bound is not a
safe real operating envelope. Context/prerequisite changes can make restore unavailable: abort,
do not change another setting to bypass validation. Unknown prerequisites may proceed in the
API, but this procedure requires true for the selected Force DHW test.

**Dedicated operation_mode owner gate:** approve its original, exact temporary alternate,
expected activity consequences and exact restore after Gate 2 and an immediate fresh pre-check.
No inference from HA labels and no automatic transition into cooling or another operating mode.

### C — NOT EXERCISED ON REAL DEVICE IN 4E-D

All remaining definitions are explicitly excluded from real POSTs, including equal-value probes.
Additional writes would require a new reviewed owner plan, not expansion by the future runbook.
`None` below means no readback, not missing evidence to be invented.

| Control key | Identity | Class | Readback | Exclusion reason / relevant context |
|---|---|---|---|---|
| `heat_pump_power` | SET1 | setting | TOP0 | Whole-machine start/stop |
| `holiday_mode` | SET2 | setting | TOP19 | Operating/schedule disruption |
| `zone1_cool_request` | SET6 | setting | TOP28 | Cooling operation; context TOP81 |
| `zone2_heat_request` | SET7 | setting | TOP34 | Unsampled second zone; context TOP76 |
| `zone2_cool_request` | SET8 | setting | TOP35 | Cooling/second zone; context TOP81 |
| `force_defrost` | SET12 | trigger | TOP26 | Service trigger; effect, not acknowledgement |
| `force_sterilization` | SET13 | trigger | TOP69 | Service trigger; effect, not acknowledgement |
| `pump_service_mode` | SET14 | setting | None | Service maximum-speed action, no state readback |
| `max_pump_duty` | SET15 | setting | TOP95 | Service-menu hydraulic setting |
| `zone1_cool_curve` | SET16 | curve | TOP72,TOP73,TOP74,TOP75 | Cooling settings; no additional curve sweep |
| `zone2_heat_curve` | SET16 | curve | TOP82,TOP83,TOP84,TOP85 | Unsampled second zone |
| `zone2_cool_curve` | SET16 | curve | TOP86,TOP87,TOP88,TOP89 | Cooling/second zone |
| `active_zones` | SET17 | setting | TOP94 | Installer topology |
| `cool_delta` | SET19 | setting | TOP24 | Cooling setting |
| `heater_delay_time` | SET21 | setting | TOP96 | Installer/heater behavior; protocol bound not device qualification |
| `heater_start_delta` | SET22 | setting | TOP97 | Installer/heater behavior |
| `heater_stop_delta` | SET23 | setting | TOP98 | Installer/heater behavior |
| `main_schedule` | SET24 | setting | TOP13 | Scheduling disruption; unnecessary transport sample |
| `alt_external_sensor` | SET25 | setting | TOP108 | Installer sensing configuration |
| `external_pad_heater` | SET26 | setting | TOP114 | Heater/installer configuration |
| `buffer_delta` | SET27 | setting | TOP113 | Buffer installation setting; unnecessary sample |
| `buffer_installed` | SET28 | setting | TOP99 | Installer topology |
| `external_control` | SET30 | setting | TOP119 | External/installer control |
| `external_error_signal` | SET31 | setting | TOP121 | External/installer error signaling |
| `external_compressor_control` | SET32 | setting | TOP122 | External/Optional PCB control |
| `external_heat_cool_control` | SET33 | setting | TOP120 | External/Optional PCB control |
| `bivalent_control` | SET34 | setting | TOP129 | Bivalent enable/disable |
| `bivalent_mode` | SET35 | setting | TOP130 | Bivalent operating policy |
| `pump_flowrate_mode` | SET42 | setting | TOP106 | Hydraulic policy; additional device applicability unproved |
| `dhw_sensor_selection` | SET43 | setting | TOP143 | Installer sensing; documented All-In-One restriction |
| `dhw_heater_allowed` | SET44 | setting | TOP58 | Heater permission |
| `room_heater_allowed` | SET45 | setting | TOP59 | Heater permission |
| `force_heater` | SET47 | setting | TOP68 | Service/emergency heater; firmware_min_4_2_0 |
| `fault_reset` | SET48 | trigger | None | Service/fault reset; TOP44 is not acknowledgement |
| `pcb_compressor_switch` | SetCompressorState | pcb_input | None | Optional PCB; P plus TOP122=1 |
| `pcb_smart_grid_mode` | SetSmartGridMode | pcb_input | None | Optional PCB; P |
| `pcb_thermostat1_demand` | SetExternalThermostat1State | pcb_input | None | Optional PCB; P; documented H/J restriction |
| `pcb_thermostat2_demand` | SetExternalThermostat2State | pcb_input | None | Optional PCB; P |
| `pcb_demand_control` | SetDemandControl | pcb_input | None | Optional PCB; P |
| `pcb_pool_temperature` | SetPoolTemp | pcb_input | None | Optional PCB; P |
| `pcb_buffer_temperature` | SetBufferTemp | pcb_input | None | Optional PCB; P; documented H/J restriction |
| `pcb_zone1_room_temperature` | SetZ1RoomTemp | pcb_input | None | Optional PCB; P; documented H/J restriction |
| `pcb_zone1_water_temperature` | SetZ1WaterTemp | pcb_input | None | Optional PCB; P |
| `pcb_zone2_room_temperature` | SetZ2RoomTemp | pcb_input | None | Optional PCB; P |
| `pcb_zone2_water_temperature` | SetZ2WaterTemp | pcb_input | None | Optional PCB; P |
| `pcb_solar_temperature` | SetSolarTemp | pcb_input | None | Optional PCB; P |

**Optional PCB policy.** No real input writes or enabling Optional PCB for tests. P is the actual
API prerequisite pair: `heat_pump_optional_pcb` (TOP110=1) and
`heishamon_optional_pcb_emulation` (not observable, identity=null). The compressor switch also
requires `external_compressor_control` (TOP122=1). Read-only TOP110=0 live evidence should make
all twelve controls non-executable with `prerequisite_not_met`; unknown TOP110 is not false.
Never POST merely to prove rejection. Emulation is not inferred from HA command echoes. The
architecture documents H74 when required emulation datagrams stop for about 40 s; do not
commission or interrupt them. Local 4E-B/C evidence remains their validation/encoding/transport
proof. `SetHeatCoolMode` and firmware-only `SetOptPCBByte9` are not executable controls at all.

## 5. HA coexistence and safety watch

HA can retain commands, retry and later overwrite settings. Pompa Next uses retain=false,
reads current TOP state, and deliberately does not reconcile, fight HA or clear retained topics.
For overlapping topics keep two separate observations:

- A: Pompa Next request, local accepted-publish/log fact and its factual readback outcome.
- B: later TOP changes and externally observed command traffic, with times and ownership evidence.

An HA replay after successful readback is not automatically a product defect. If it makes
restoration ambiguous, stop further tests of that control. Any owner-approved handling of HA
must be recorded separately; no automatic disablement or cleanup is part of this contract.

A passive broker observer can record command counts/payloads/QoS and TOP receipts, but MQTT 3.1.1
delivery does not identify the publishing client, and a forwarded message's retain flag does not
prove the sender used retain=false. Correlate API times/logs and re-audit retained state after a
completed test. If HA emits indistinguishable commands, mark attribution ambiguous rather than
claiming Pompa Next retried. Exactly-one-publish runtime behavior is supported by accepted code
identity and local transport tests plus correlated runtime evidence, not by treating HTTP `sent`
as a broker acknowledgement. Do not induce reconnects or an outage to exercise replay.

**TOP44 and current activity:** capture live TOP44 and `/activity/live` before write, after the
response and after restore, including raw/mode/available/receipt and activity inputs. Compare
with the initial error state and owner-approved expected consequences. Do not universally decode
all TOP44 strings or automatically reset faults. New/unexpected errors abort. Initially abnormal/
unknown errors or inadequate live classifier evidence require owner resolution before a write;
`unknown` is a factual class, not a guessed off state.

Record every observed activity transition during mutation, restore and final observation,
including off/idle/co/dhw/transition/defrost/unknown, its time and supporting TOP/status facts.
The **owner**, not the runbook, classifies each transition:

1. **EXPECTED / TEST-ATTRIBUTABLE:** a consequence explicitly approved for this control/test.
2. **INDEPENDENT NATURAL OPERATION:** consistent with ordinary heat-pump operation, not attributed
   to the test (for example a supported natural compressor transition or defrost).
3. **UNEXPLAINED / UNAPPROVED:** cannot confidently be placed in 1 or 2.

Abort progression for category 3 or a test-attributable effect exceeding approval. Natural
operation is evidence, not an automatic defect/abort; classification must be recorded before
proceeding. Never auto-classify every defrost as natural. Final activity need not equal initial
activity for **any** control, including settings: restoring a setting does not rewind physical
operation. Final proof requires restored target setting, acceptable TOP44, understood/classified
final activity and no unresolved consequence.

#### Connection/LWT watch and attribution reset

During every mutation/restore/final-observation interval capture timestamped `/status` snapshots
before, during and after, plus passive LWT receipts. Record exact fields:
`mqtt.connected`, `mqtt.epoch`, `mqtt.connects`, `mqtt.disconnects`, `mqtt.connected_at`,
`mqtt.disconnected_at`, `mqtt.lwt.state`, `mqtt.lwt.retained`, `mqtt.lwt.received_at`,
`mqtt.lwt.messages`, `mqtt.alive`, `mqtt.last_live_message_at`, `mqtt.clock_steps`,
`mqtt.last_clock_step_at` and `recorder.process_start`. Compare counters/times across snapshots,
not only final connected=true or the final LWT string.

Any connection epoch/reconnect/disconnect, LWT state/receipt/count or process-start change means
**POSSIBLE RETAINED REPLAY / ATTRIBUTION RESET**. STOP progression for that control and return
to owner analysis. A retained command can be delivered specifically to reconnecting HeishaMon
without a new publication visible to the passive observer. Changed context is not proof that
HA replay occurred; factual API TOP readback remains recorded, but later-state attribution and
latency eligibility are uncertain. Do not induce a reconnect or ignore a brief event because
the final connection is healthy again.

#### Post-restore read-only observation window

Use **POST_RESTORE_OBSERVE_SECONDS = 45 s minimum**, measured monotonically from final live
restore confirmation (or final original-state confirmation after an equal-value probe). Gate 1
records this duration; the owner may choose longer for observed installed HA behavior, never
silently shorten it. Continuous passive commands/TOP/LWT collection and timestamped read-only
status/activity snapshots continue through this window; collect before/after snapshots and
snapshots on observed changes. Finish with fresh target/TOP44/activity/status and retained audit.
No extra POST is automatic.

Evidence: pinned `kamaradclimber/heishamon-homeassistant@e206e023af5453e4fefbbf3545e9c60d5ca79fd5`,
`custom_components/aquarea/retry_mixin.py`, has RETRY_TIMEOUT=10 s, jitter ±1 s and MAX_RETRIES=3;
number/select callbacks re-register the same command while preserving retry count. This is
**three retries after the initial publish**, not three total attempts: approximately 9–11,
18–22 and 27–33 s, plus a later check that ends the pending command. Forty-five seconds covers
that nominal retry/check sequence with margin. Scheduling can delay it and the installed HA
version/settings are not assumed; this is a validation minimum, not a bound on all external
writers or a product timing guarantee. Record actual observation start/end/duration and limits.

Any external overwrite during this window invalidates test completion even if later restored
by another writer. New/changed overlapping retained commands require re-approval; connection/LWT
changes trigger attribution reset; activity uses the three-way classification above. Stop and
return to owner analysis without an automatic corrective POST or restarted observation loop.

## 6. Canonical one-write / one-restore evidence record

Evidence is a human validation artifact kept privately, with raw response/log files and checksums;
it is **not product command persistence**. A later concise accepted summary can be materialized
in project docs without secrets. Every mutation has one record; equal-value probes use the same
record with restore marked not needed only after unchanged final state is verified.

| Record field | Required content |
|---|---|
| Identity | Test id, phase, owner approval, candidate SHA, accepted plan revision, control key, SET/PCB identity, class, service flag |
| Original | Original semantic value and raw TOP, mode/available/received_at; all requested curve fields and full curve snapshot |
| Validation | Full value schema/range_basis; prerequisite results and raw TOPs; context/active range; required current-condition fact set; separate preferred/fallback assessments; intended impact and skip decision |
| Coexistence | Connection/SUBACK, all passive topics/retain flags, backend uncatalogued cross-check/visibility, retained payload revisions, approvals, HA ownership confidence, external-writer observations |
| Watch before | TOP44 and current activity/inputs; exact §5 MQTT/LWT/clock/process-start fields; INFO-log availability proof |
| Request | Exact semantic request value, owner request wall time and response receipt time, HTTP status and full response |
| Publish | publish.status, publish.at; exactly one structured request log (error/sent/error code as applicable); passive command evidence and attribution limits |
| Readback | identity/fields, kind, expected, outcome, observed raw/value, observed.received_at for each requested field, window_seconds, waited_seconds |
| Latency | Applicable calculation in §7, sample inclusion/exclusion reason; distinct passive late observation if any |
| Watch after | Fresh control/TOP state, TOP44; every observed activity transition with three-way owner classification; §5 connection/LWT/clock facts and attribution reset if any |
| Restore | Original value reconfirmed within current schema/context; owner approval; exact restore request/time, HTTP/full response and corresponding log |
| Final | Final live restored semantic TOP and raw/receipt, untouched curve fields, acceptable TOP44, classified final activity; post-restore duration/start/end, retained audit, connection/LWT facts and external overwrites |
| Conclusion | Verdict: observed-and-restored / equal-value-observed / skipped / aborted / unresolved; anomalies, abort notes, outstanding device state and owner decision |

For a curve keep per-field qualifying timestamps and calculate only over requested fields.
Every mutation must end restored or explicitly unresolved/aborted; never omit the restore record
because publication was accepted, a timer may expire, or the HTTP response was lost. A restore
proves reported state, not reversal of all physical effects or an active timer's elapsed duration.

## 7. Latency, passive extension and W decision

W remains **15 s provisional**. There is no environment setting for it and no change in this
checkpoint. Qualifying latency samples require accepted publish, `matched`, qualifying
post-publish expected receipt(s), no continuity/reconnect/LWT ambiguity, no conflicting
external-writer attribution and a passing clock sanity check below.
Baseline receipts remain PRE even if numerically newer after a clock correction.

- Scalar latency = `observed.received_at - publish.at`.
- Curve completion latency = latest qualifying **requested-field** `received_at - publish.at`.
- `waited_seconds` is monotonic request-window elapsed time, not a substitute for receipt latency.
  For every otherwise eligible `matched` sample calculate **wall_latency** using the scalar receipt
  or last required qualifying curve-field receipt above, and
  **clock_difference = abs(wall_latency - waited_seconds)**. If wall_latency < 0 or
  clock_difference > **1.0 s**, classify **DIAGNOSTIC ONLY — CLOCK/ATTRIBUTION UNCERTAIN**.
  Exclude it from min/median/max/p95 and successful-latency evidence for W. Save both numbers,
  the difference and classification in the record; do not round before the comparison beyond
  the precision already exposed by the API.
- The 1.0 s tolerance is a validation sanity threshold, not a product timing guarantee.
  `publish.at` is sampled just before publish; the monotonic window begins after acceptance and
  initial readback setup. Small publish/setup, observation scheduling and millisecond serialization
  differences are expected. Larger discrepancies are conservatively excluded even if caused by
  scheduling. Recorder detects sufficiently large **backward** corrections, not forward steps;
  unchanged clock_steps cannot establish clock sanity. This comparison can expose forward
  distortion but cannot prove the absence of every small or offsetting clock movement.
- `unchanged_match`, `not_observed` and `not_applicable` are excluded as successful samples.
  Same-value probes and ignored retained receipts do not measure successful command latency.
- Clock correction, inconsistent/negative deltas or indistinguishable concurrent external writes
  make a sample diagnostic only. Report the limitation; do not invent causal device latency.
  Even eligible results measure reported-state receipt latency, not physical execution time.

Record change and restore samples separately by control, direction and readback kind. Phase 4
reports sample count, min/median/max, outliers and publication cadence from actual stats/passive
receipts. Report p95 only for at least 20 eligible samples in the stated group, using nearest-rank
`ceil(0.95*n)`; label it descriptive, not a reliable population bound. For smaller groups report
count/min/median/max only. No automatic repeats are allowed to manufacture a percentile.
Explain differences across control/readback types and firmware cadence rather than pooling away
outliers. Forced effect triggers are excluded, so no real effect-latency claim can be made.

**60-second passive extension:** when an ordinary change returns `not_observed` within W, stop
new writes. The owner may continue read-only TOP/GET observation up to 60 s total from
`publish.at`, with timestamped receipts and no second POST. If the expected TOP appears later,
record its time/delta as **later external observation**, not `matched`, not a retroactively
changed API response or an eligible successful-W sample. If it does not appear, record that
bounded absence. Loss of continuity or a clock correction is an abort, not justification for
longer or repeated writes. This is a diagnostic procedure, not production configuration.

After a timeout the owner decides restoration from current facts. If current original state is
already present, document it; any restore POST is still deliberate, never a retry disguised as
restore. Temporaries and triggers are never automatically retried. A lost response or
unexpected 500 may follow accepted publication: inspect state/logs before any new action.

The owner decides final W from eligible samples **and** timeouts/late observations/uncertainty,
not just fast matches. Increasing, reducing or keeping 15 s must be evidence-based; no direction
is predetermined. Insufficient coverage must be stated, and any further sample requires owner approval. A W change requires a
separately reviewed implementation/doc correction and regression validation before API freeze;
this document does not authorize changing W or adding a setting.

## 8. Fail-closed abort model

Stop the current phase with **no next matrix item** on:

- Intended-write 503, unexpected HTTP 500, lost/ambiguous response or unexpected documented refusal.
- Any §5 connection/LWT/process-start attribution reset, clock correction or relevant readback
  availability loss during mutation/restore/final observation.
- Unexpected `not_observed`, intended-change `unchanged_match`, mismatch or unexplained state
  that leaves the final value ambiguous (passive diagnostics are allowed as in §7, not more writes).
- Failure to restore/prove the exact original semantic value.
- New/unexpected TOP44 error, UNEXPLAINED / UNAPPROVED activity, or a test-attributable effect
  exceeding approval; independent natural operation is recorded, not automatically aborted.
- New/changed overlapping retained command after Gate 1 (invalidates that control approval),
  unresolved audit visibility, or external overwrite during post-restore observation.
- Missing/suppressed structured INFO request logs or loss of their capture.
- Original/alternate/restore outside the live schema, changed context, or false/unknown
  prerequisite where this procedure requires true.
- Deployed checkout/running identity mismatch, more or fewer than one backend, unexplained
  63/218/157 count mismatch, or unexplained startup/runtime/schema/database errors.

Collect response, TOP/activity/status, passive traffic and bounded backend-log evidence and
return to owner analysis. Do not continue, automatically retry, clear retained state, reset
faults, switch modes to regain prerequisites, deploy another candidate or roll back automatically.
If the device still holds a mutated value, flag it immediately; any further restore or physical
panel intervention is an explicit owner decision using current facts. A previously approved
normal restore is not a blanket recovery authorization after connectivity/error conditions change.

## 9. API final-freeze and Stage 4E closeout criteria

API is final only when the owner accepts:

- No existing-resource regression on CT109: health/live/metrics/status, representative history,
  activity, optional resources and report, including preserved immutable history/data.
- Truthful GET `/controls`: counts, state/provenance/availability, schema, prerequisites,
  restrictions and executable facts match the observed runtime; no Optional PCB echo as state.
- Selected real POSTs use one accepted publish per approved request, with no retry/replay
  evidence, truthful logs/outcomes and no control-driven recorder/history/storage change.
- Equal-value requests show expected full-W `unchanged_match`; changed requests show factual
  `matched` or accounted-for `not_observed`, with no unresolved device state or contract defect.
- Every mutation has demonstrated exact reported-state restoration and explained activity/error
  consequences; HA coexistence and retained overlap are understood.
- Actual latency/cadence and late-observation evidence support an explicit final W decision.
- No unresolved Stage 4E-C contract defect; skipped disruptive/PCB controls remain explicitly
  justified by accepted local protocol/runtime evidence, not claimed real-device-tested.

A runtime contract defect blocks freeze and returns to implementation/review. Small live samples
cannot prove every device range, physical effect, or absence of all possible external writers.

After CT109 evidence is accepted, the remaining sequence is:

1. Materialize concise runtime evidence and the final owner W decision.
2. Update `docs/API.md` if W or another factual contract changed.
3. Update architecture/status/roadmap with accepted facts.
4. Run full local regression again, including MariaDB and affected runtime/transport checks.
5. Whole-PR independent Opus review.
6. Resolve findings and validate corrections.
7. GitHub Codex review/nożownik; resolve any accepted findings.
8. Owner merge decision (PR remains OPEN + DRAFT until the owner changes it).
9. Only after closure/merge: Stage 4 DONE.
10. Stage 5 frontend NEXT, not implemented by this work.

No step in that sequence is performed by 4E-D-A.

## 10. 4E-D-B boundary and repository evidence

Only after independent review and owner acceptance of this contract may 4E-D-B generate the
execution runbook, untracked under `scratchpad/`. It must implement the accepted phases,
evidence fields, dynamic selection, per-action owner gates and aborts mechanically. It cannot
substitute a branch tip, add controls, auto-restore after an abort, auto-retry, clean retained
commands or silently relax unavailable pre-checks. No `scratchpad/stage-4e-d-ct109-runbook.sh`
or executable CT109 POST commands are created in this checkpoint.

Implementation evidence: `control.py` owns all definitions/schema/prerequisites/mappings;
`control_runtime.py` owns baseline receipt identity, continuity/precedence, W and factual log
results; `mqtt.py` owns connected-only one-attempt QoS 0 non-retained transport; `main.py` wires
one shared client and one process; `api.py` parses semantic bodies and maps expected errors.
`ingest.py`/`recorder.py` preserve generic live continuity and independent recorder facts.
`docker-compose.yml`, `backend/Dockerfile`, `.env.example` and `scripts/smoke.sh` establish the
existing deployment model; the example is never a replacement for runtime configuration.

Committed tests in `test_control.py` and `test_control_api.py` prove protocol golden payloads,
all schemas/maps, prerequisites, mixed curves, baseline/clock/continuity corrections, full-W
unchanged precedence, refusal logging, one attempt, no wait for absent readback, per-key
concurrency and no database dependency. Capability/contract tests pin counts and earlier APIs.

Prior runtime conventions were inspected in repository history: Stage 4A preparation `6aa6bed`
and runtime evidence in STATUS; Stage 4C closure `44c7509`; Stage 4D preparation `e775132` and
runtime materialization `7e3ad1a`. They establish owner execution, exact checkout/running source
hashes, one backend, preserved data/immutable response, truthful restart gaps and bounded logs.
They do not establish a named Proxmox snapshot workflow; the owner must supply the actual usable
rollback point in Phase 0. This document is a validation contract, not a deployment log or
product command-history feature.
