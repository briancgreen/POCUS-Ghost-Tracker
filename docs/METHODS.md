# Methods: Log-Based Estimation of Undocumented POCUS Utilization

## 1. Background and rationale

Point-of-care ultrasound (POCUS) devices deployed in emergency departments,
intensive care units, and perioperative/critical-care settings are typically
operated directly by treating physicians rather than sonographers, and used
for rapid diagnostic or procedural-guidance purposes (e.g., vascular access,
regional anesthesia nerve blocks, FAST exams) rather than formal diagnostic
imaging studies. Conventional utilization-tracking methods — counting studies
in PACS, or counting orders in the EMR — systematically miss exams that were
performed but never saved to PACS and never documented as a discrete order.
This undercounting is not a hypothetical concern: in the analysis supporting
this tool's development, one device with substantial confirmed scanning
activity had documentation-linked events in fewer than 1% of its detected
scan sessions, while another device from an anesthesia service documented
closer to 40% - both entirely invisible to PACS- or EMR-based counting alone.

The premise of this approach is that ultrasound devices log low-level
hardware and UI activity (scan start/stop, freeze/unfreeze, probe and image
parameter changes, save actions) independently of whether the operator takes
any documentation action. These logs are not designed for utilization
analysis - they are engineering/diagnostic logs, vary enormously in format
and reliability between vendors and even between firmware versions of the
same vendor, and mix a very high proportion of non-clinical telemetry with
clinically meaningful lines. This method treats log parsing as a
signal-detection problem: identify which log lines correspond to genuine
clinical actions, assign each a confidence weight, and reconstruct exam
sessions from the resulting event stream.

## 2. Exam documentation taxonomy

Every reconstructed session is classified into one of four categories,
motivated by the distinct downstream fates a real exam can have:

1. **Documented** - an order or note exists in the EMR (not assessed by this
   tool directly; requires cross-referencing against EMR data outside this
   pipeline's scope).
2. **Saved to PACS** - images were sent to the enterprise archive.
3. **Saved locally, not to PACS** - a traditional "ghost exam": images exist
   on the device but were never archived centrally.
4. **Not saved at all** - no persistent record of the exam exists anywhere.
   This category is the primary target of this method, since it is
   invisible to every conventional utilization-tracking approach.

The pipeline's session classifications (`FULL_WORKFLOW`, `SCAN_SAVED`,
`GHOST_SCAN`, `LIFECYCLE_ONLY`, `NOISE`) are a device-log-observable proxy
for categories 3 and 4 above (`GHOST_SCAN` = category 4; `SCAN_SAVED` can
correspond to category 3 when no lifecycle/documentation signal is present).
Categories 1-2 require joining device-log output against EMR/PACS data,
which is outside this tool's scope but is a natural downstream use of its
output.

## 3. System architecture

The pipeline is organized into three sequential, independently runnable
stages, deliberately separating *what counts as a clinical event*
(configuration) from *how logs are parsed* (mechanism) from *how events
become exam counts* (aggregation logic):

### 3.1 Rule definition (`rules.json`)

A declarative library mapping vendor- and firmware-specific log-line
patterns to a common vocabulary of clinical events (e.g. `scan_started`,
`freeze_pressed`, `image_saved`, `exam_started`, `exam_completed`). Each
pattern carries:
- a regular expression matched against a raw log line,
- a confidence weight from 0.0 (noise, explicitly catalogued but never
  contributing to session evidence) to 1.0 (definitive single-purpose
  signal), with intermediate weights for corroborating or ambiguous signals,
- optional exclude patterns (used, for example, to distinguish a
  user-initiated freeze from an automatic power-save freeze that
  superficially resembles it in the log text),
- a free-text rationale note recording why a given weight/pattern was
  chosen and what would be needed to raise or lower confidence in it.

Timestamp parsing is defined the same way: one regular expression plus a
`strptime`-style format string per vendor, rather than hardcoded per-vendor
parsing logic. This was a deliberate refactor during development: an earlier
version of the parser hardcoded timestamp formats directly in code for
performance reasons, but profiling showed the actual performance-sensitive
operation was per-line pattern matching, not the comparatively rare
timestamp-extraction step - so timestamp handling could be moved fully into
configuration with no performance cost, while patterns are still compiled
once at load time rather than recompiled per line.

### 3.2 Log parsing (`parser.py`)

Streams each device's log file(s) line by line (regardless of underlying
file format - plain text or delimited/structured export), matches each line
against the pre-compiled pattern set for that device's vendor, and emits a
normalized event record (device, timestamp, event type, confidence weight,
plus minimal contextual fields such as probe/application where extractable)
for every matching line. Vendor identity is resolved from a directory-naming
convention, so no per-vendor branching exists in the parsing logic itself -
all vendor-specific behavior lives in `rules.json`.

### 3.3 Session reconstruction and classification (`sessionizer.py`)

Groups each device's event stream into discrete sessions by splitting on a
configurable maximum time gap between consecutive events, or on encountering
a hard session-boundary event (power-on/shutdown). Each session is then
classified by which categories of evidence it contains - scanning activity,
a save action, and/or exam lifecycle events - into the taxonomy in Section 2.
Classification deliberately does **not** require the presence of exam
lifecycle events (patient registration, exam completion) to recognize
scanning activity as a real exam; see Section 4.2.

## 4. Key design decisions and their empirical validation

Several design choices in this pipeline were arrived at, or revised,
specifically by testing an initial assumption against real device logs
rather than by specification alone. This section documents the more
consequential of those decisions and the evidence behind them, both as
methodological justification and as a record for future extension.

### 4.1 Weighted evidence rather than binary detection

Rather than treating every matched pattern as equally certain evidence of
its designated event, each pattern carries a confidence weight, and a
minimum qualifying weight threshold filters out low-confidence matches
before session reconstruction. This allows genuinely ambiguous or
multi-purpose log lines to be included as corroborating evidence (e.g., a
device-status line that could plausibly but not certainly indicate patient
registration) without letting them independently drive a classification
decision the way a definitive, single-purpose log line can.

### 4.2 Scan/manipulation evidence, not exam-lifecycle evidence, as the basis for detecting a real exam

An early design question was whether a session should require an
exam-lifecycle signal (e.g., patient registration) to be counted as a real
exam. Empirical analysis of real device logs showed this would
systematically undercount the exact phenomenon under study: on one
emergency-department-labeled device, 1,133 of 1,140 valid sessions (99.4%)
contained clear scanning evidence (scan start/stop, active image parameter
adjustment) but zero exam-lifecycle signal of any kind. Gating detection on
lifecycle events would have reported this device as having almost no
utilization at all, when in fact it had substantial unrecorded scanning
activity - precisely the pattern this method is designed to surface.
Classification therefore depends only on scan/save evidence; lifecycle
events are used to refine classification (e.g., distinguishing
`FULL_WORKFLOW` from `SCAN_SAVED`) but are never required for a session to
be recognized as a real exam.

### 4.3 Validating that "manipulation" events indicate genuine scanning

Several event types (adjusting gain, depth, cine-frame position, imaging
mode toggles, etc.) were incorporated as scan evidence on the premise that a
user does not adjust image parameters unless they are actively scanning.
This was tested rather than assumed: across two independent vendors, the
proportion of such parameter-adjustment events falling within an
independently-confirmed active-imaging interval (defined by the vendor's own
imaging-start/imaging-stop events, ±2 seconds) was 99.9-100%. A similar
check ruled out at least one plausible-looking candidate signal - a
transducer-bay status-change event initially hypothesized to indicate a
mid-exam probe swap - which was found to co-occur with system boot events
91% of the time, indicating it was predominantly a boot-time hardware
enumeration event rather than a live user action, and was excluded from scan
evidence accordingly.

### 4.4 Distinguishing genuine per-exam signals from batch/administrative events

Not every plausible-looking "documentation" signal turned out to be
per-exam. An event initially mapped as a per-exam PACS-send confirmation was
later found, on inspection of its distribution, to occur 360 times within
only 12 distinct minutes of device uptime, with associated study identifiers
spanning several months of prior exam dates - indicating a single bulk
historical export operation rather than 360 individual real-time PACS sends.
This pattern was removed from the rule set rather than merely down-weighted,
since it did not represent the phenomenon it was assumed to represent at
all. This case illustrates a general principle followed throughout rule
development: a pattern is validated against the distribution and context of
its real occurrences before being trusted, not adopted on the strength of a
plausible-sounding log message alone.

### 4.5 Full-dataset totals versus sampled-window statistics

An early version of the session-summary step selected a single
representative multi-day "busiest window" from each device's full log span,
on the reasoning that log retention/export practices are inconsistent across
sites and a bounded representative sample would produce more comparable
per-day rates than an arbitrary full export. This produced a significant
undercount for devices with bursty, episodic usage patterns: for one device,
the sampled window reported 37 ghost-scan sessions, while the true total
across the device's full 261-day log span was 1,133 - because the busiest
single day in the log was immediately followed by two nearly idle days,
which still produced the numerically "busiest" contiguous window despite not
being representative of typical usage. The pipeline now reports full-dataset
classification totals as the primary exam-count estimate, and retains the
windowed sample only for session-shape statistics (e.g., average session
duration) that benefit from a bounded, comparable sample.

### 4.6 Vendor and firmware heterogeneity

Two vendors in this analysis required more than one rule set because a
single "vendor" designation did not correspond to a single log schema:
- Two product lines from the same manufacturer (referred to here as
  variants A and B) used different logging subsystems and message formats
  for what were functionally the same clinical events.
- An older firmware revision of a second manufacturer's device used an
  entirely different logging channel for exam lifecycle and scan events
  than the current firmware, to the point that none of the patterns
  developed for the current firmware matched the older revision's logs at
  all.

Rather than attempting to unify these into a single pattern set per
manufacturer, each was given an independent rule set within the same
configuration file, with detection code unaffected — only the declarative
rules differ. This generalizes the same way to any future device or
firmware variant.

## 5. Noise filtering

Ultrasound device logs are dominated by non-clinical telemetry: battery
status polling, touch-input coordinates, network/Wi-Fi signal quality,
memory/performance counters, and UI navigation chatter. In one representative
export, a single battery-telemetry event type accounted for approximately
37% of all log lines. Non-clinical patterns are explicitly catalogued in the
rule set at zero weight (rather than left undocumented and simply
unmatched), both so that a future maintainer can see that a given log line
type was considered and deliberately excluded, and so any line not matching
a cataloged pattern at all is visibly a genuine gap rather than an
assumed-safe omission.

## 6. Limitations

- Confidence in individual patterns varies; several patterns carry
  intermediate weights and an explicit rationale note where the available
  log samples did not permit full validation (e.g., a device's specific
  freeze/unfreeze semantics, or its patient-registration signal reliability,
  could not be confirmed against every observed firmware variant).
- Session-boundary and classification thresholds (maximum inter-event gap,
  minimum scan-event count, minimum qualifying confidence weight) are
  configurable constants whose values affect how conservative or liberal the
  resulting exam-count estimates are; they were set based on the log samples
  available during development and may warrant re-validation for other
  sites or device fleets.
- This method estimates device-side evidence only. It cannot determine
  whether a given exam was ultimately documented in the EMR or reviewed
  clinically; establishing that requires cross-referencing this pipeline's
  output against EMR/PACS data, which is outside its scope.
- All validation in this document was performed against a convenience
  sample of device exports available during development; broader validation
  across additional sites, device models, and firmware versions is an
  area for future work.

## 7. Software availability

The complete implementation (`rules.json`, `parser.py`, `sessionizer.py`),
synthetic sample logs sufficient to reproduce a demonstration run, and this
documentation are available at: [repository URL - update on publication].
The software has no external dependencies beyond the Python 3 standard
library.
