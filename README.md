# POCUS Ghost-Exam Detector

Estimate the true number of point-of-care ultrasound (POCUS) exams performed on
a device by mining its own system logs — including exams that were never
documented in the EMR, never saved to PACS, and never saved anywhere at all.

## The problem this solves

In departments like the ED, ICU, perioperative, and other critical-care areas,
ultrasound machines are frequently used by physicians for quick bedside
assessments and procedural guidance (e.g. vascular access, nerve blocks).
Unlike dedicated ultrasound department workflows, there is often no reliable
way to know how many exams were actually performed:

- The exam may never be logged as an order in the EMR.
- The images may never be saved to PACS.
- The images may not even be saved locally on the device.

Device system logs, however, record hardware- and UI-level activity (scan
start/stop, freeze/unfreeze, image parameter changes, save actions) regardless
of whether the user did any of the above. This project parses those logs
across multiple ultrasound vendors/models into a common event schema, groups
events into clinical "sessions," and classifies each session by what evidence
of documentation is actually present.

## Exam classification taxonomy

Each detected session is classified into one of:

| Classification   | Meaning                                                                                                        |
|------------------|----------------------------------------------------------------------------------------------------------------|
| `FULL_WORKFLOW`  | Scanning occurred, an image was saved, and exam lifecycle events (patient registration/completion) are present |
| `SCAN_SAVED`     | Scanning occurred and an image was saved, but no lifecycle/documentation signal was found                      |
| `GHOST_SCAN`     | Scanning occurred, but nothing was ever saved — the core metric this tool exists to surface                    |
| `LIFECYCLE_ONLY` | Only lifecycle events are present, no evidence of actual scanning                                              |
| `NOISE`          | Neither scanning nor lifecycle evidence - not counted toward exam estimates                                    |

## Quick start

```bash
git clone <this-repo>
cd <this-repo>

# Try it on the bundled synthetic sample logs first
cp -r sample_logs logs
python3 parser.py
python3 sessionizer.py
```

This produces:
- `output/<vendor>/<device_id>_events.json` — normalized per-line event records
- `output_sessions/<vendor>/<device_id>_sessions.json` — reconstructed sessions with classification, across the **full** log span
- `device_summary.csv` — one row per device, with both full-dataset totals and a sampled-window view (see below)

No external dependencies — Python 3.8+ standard library only.

## Using it on real logs

Real device logs almost certainly contain PHI. Do not commit them to version
control (`.gitignore` already excludes `/logs/`). Place your own exports under:

```
logs/<Vendor>/<device_id>/<any files>
```

`<Vendor>` must match one of the vendor keys in `rules.json` (case-insensitive):
`GE`, `GE_Logiq`, `Mindray`, `Mindray_M6`, `Sonosite`. All files inside a
device folder are treated as logs — no extension filtering, so plain-text logs
and CSV exports can sit in the same structure.

## Architecture

Three stages, each independently runnable:

1. **`rules.json`** — the single source of truth. A declarative library mapping
   vendor-specific log-line patterns to a common vocabulary of clinical events
   (`scan_started`, `freeze_pressed`, `image_saved`, `exam_started`, etc.), each
   with a confidence weight (1.0 = definitive, down to 0.3-0.6 for
   corroborating/ambiguous signals) and optional exclude patterns (e.g. to
   distinguish a manual freeze from an automatic power-save freeze). Also
   defines per-vendor timestamp formats, so **adding a new vendor or a new
   firmware variant of an existing vendor is a JSON edit, not a code change.**

2. **`parser.py`** — streams every log file line by line, matches each line
   against the pre-compiled patterns for that file's vendor, extracts a
   timestamp and minimal context (probe/application), and writes a normalized
   event stream per device.

3. **`sessionizer.py`** — groups each device's event stream into sessions
   (splitting on a configurable time gap or a hard boundary event like
   power-on/shutdown), classifies each session per the taxonomy above, and
   writes both per-device session detail and a cross-device summary CSV.

See [`docs/METHODS.md`](docs/METHODS.md) for the full design rationale,
validation approach, and known limitations - the same content submitted as the
methods section of the accompanying paper.

## Reading `device_summary.csv`

The most important thing to know: **use the `*_full` columns for exam-count
estimates, not the `*_window` columns.**

- `ghost_scans_full`, `full_workflow_full`, etc. are computed across the
  **entire** log span in the file and are the true totals.
- `ghost_scans_window`, `full_workflow_window`, etc. are computed only over
  the busiest contiguous multi-day window found in the log, and exist purely
  to give session-shape statistics (`avg_events_per_session`,
  `avg_session_duration_sec`, etc.) without those averages being skewed by
  long idle stretches. **For devices with bursty/episodic usage, this window
  can badly undercount the true total** — it is a sample, not a total.

## Extending to a new vendor or device model

1. Get a real log sample - ideally one spanning actual patient use, not just
   boot/setup activity.
2. Identify the timestamp format and add a `regex` + `strptime` pair under
   `_timestamp_formats` in `rules.json`.
3. Identify candidate event lines for each category in the taxonomy above and
   add them under a new vendor key, with a weight reflecting your confidence.
4. Validate before trusting a pattern: run the parser, then check whether the
   new events actually co-occur with other independent evidence (e.g. do
   proposed "manipulation" events fall within an active scan interval? Is a
   proposed "freeze" event a rapid, deliberate toggle rather than an idle
   timeout?). Don't assume a plausible-looking log line means what it seems to
   - several patterns in this project's history were revised or removed after
     this kind of check (see `docs/METHODS.md` for examples).
5. Watch for multiple firmware/product variants under one vendor "brand" (this
   project encountered this twice - GE Venue Go vs. LOGIQ e, and current
   Mindray vs. older M6 firmware) - if the log schema differs, give it its own
   vendor key rather than trying to unify the patterns.

## Known limitations

- Detection confidence varies by vendor/event - patterns with a low weight in
  `rules.json` typically have a `note` field explaining why they're uncertain
  and what would firm them up.
- Session classification depends on `SCAN_EVENTS`/`SAVE_EVENTS`/
  `LIFECYCLE_EVENTS` definitions in `sessionizer.py`, which were validated
  empirically against the log samples available during development - a new
  device/firmware version may need re-validation.
- This tool estimates *device-side* evidence of scanning and saving. It does
  not know what happened in the EMR or PACS; cross-referencing against those
  systems is a separate step outside this repo's scope.
- Session-boundary and gap-threshold constants (`SESSION_GAP_MINUTES`,
  `MIN_SCAN_EVENTS`, `QUALIFYING_WEIGHT` in `sessionizer.py`) are tunable and
  will affect how conservative or liberal the resulting counts are.

## Citation

If you use this tool in academic work, please cite: [paper citation - update
once published].

## License

MIT - see `LICENSE`.
