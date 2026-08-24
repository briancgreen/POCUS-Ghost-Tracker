import os
import re
import json
import io
from collections import deque
from datetime import datetime

LOG_ROOT = "logs"
CONTEXT_WINDOW = 10  # lines to keep before current for context enrichment


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------
#
# Formats themselves live in rules.json under "_timestamp_formats" (a LIST of
# regex + strptime pairs per vendor, since one vendor can produce more than
# one log format/file type - e.g. Sonosite's CSV export vs. its native
# system.log) and are precompiled once in load_rules(), same as the
# event-detection patterns. Adding a new format is a rules.json edit only -
# no changes needed here.

def extract_timestamp(line, vendor, timestamp_formats):
    """
    Parse a timestamp for the given vendor's log line by trying each
    precompiled (regex, strptime_format) pair for that vendor in order,
    stopping at the first that matches. Returns ISO 8601 string, or None if
    the vendor has no configured format or no format matches the line.
    """
    for fmt in timestamp_formats.get(vendor, []):
        m = fmt["compiled_re"].search(line)
        if not m:
            continue
        try:
            return datetime.strptime(m.group(1), fmt["strptime"]).isoformat()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Context extraction helpers
# ---------------------------------------------------------------------------
# Trimmed to just probe + application - the only two fields sessionizer.py
# actually reads downstream (they feed the "applications"/"probes" lists in
# each session summary). Previously this also extracted study_uid, dicom_file,
# shutdown_mode, worklist_result_count, save_mode/file_type/patient_folder,
# probe_serial/scan_frames, and user - all captured but never consumed
# anywhere in sessionizer.py or the CSV output, so removed rather than kept
# as unused complexity. If any of those turn out to be needed later (e.g.
# per-user accountability), they're straightforward to re-add.

# GE Venue / LOGIQ
_PROBE_RE = re.compile(r'Probe\("([^"<][^"]*)"\)')
_APPL_RE  = re.compile(r'Appl\("([^"<][^"]*)"\)')

# Mindray TEX / M6
_MR_PROBE_RE    = re.compile(r'Current:Probe\[([^\]]+)\]')
_MR_EXAMMODE_RE = re.compile(r'ExamMode\[([^\]]+)\]')


def extract_context_fields(line, recent_lines):
    """
    Return probe/application fields extracted from the matched line, falling
    back to the recent-lines buffer if not present on the matched line itself
    (GE only - Mindray's ExamMode line is always the line that matched, so no
    fallback is needed there).
    """
    extra = {}

    # --- GE Venue/LOGIQ: Probe + Application from ESStatus lines ---
    probe_match = _PROBE_RE.search(line)
    appl_match  = _APPL_RE.search(line)
    if probe_match:
        extra["probe"] = probe_match.group(1)
    if appl_match:
        extra["application"] = appl_match.group(1)

    # Fall back to recent context if not on this line
    if "probe" not in extra or "application" not in extra:
        for ctx_line in reversed(recent_lines):
            if "probe" not in extra:
                m = _PROBE_RE.search(ctx_line)
                if m and m.group(1) not in ("<none>", ""):
                    extra["probe"] = m.group(1)
            if "application" not in extra:
                m = _APPL_RE.search(ctx_line)
                if m and m.group(1) not in ("<none>", ""):
                    extra["application"] = m.group(1)
            if "probe" in extra and "application" in extra:
                break

    # --- Mindray TEX/M6: Probe + ExamMode from HandleExamModeChanged ---
    mr_probe = _MR_PROBE_RE.search(line)
    mr_exam  = _MR_EXAMMODE_RE.search(line)
    if mr_probe and "probe" not in extra:
        extra["probe"] = mr_probe.group(1).strip()
    if mr_exam and "application" not in extra:
        extra["application"] = mr_exam.group(1).strip()

    return extra


# ---------------------------------------------------------------------------
# Rules loading — pre-compiles all patterns at startup
# ---------------------------------------------------------------------------

def load_rules(file_path):
    """
    Load rules.json and pre-compile every regex pattern so match_events()
    never recompiles on the hot path. Same treatment for "_timestamp_formats"
    so extract_timestamp() also never recompiles per line.

    Builds per vendor a flat list of:
        (event_name, weight, compiled_re, [compiled_exclude_re, ...])

    Ignore rules are skipped entirely — noise lines won't match clinical
    patterns anyway, so there's nothing to suppress.

    Also builds _vendor_aliases for case-insensitive folder-name resolution.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        rules = json.load(f)

    vendor_aliases = {}
    compiled_by_vendor = {}  # vendor -> {"rules": [...]}

    for event_name, event_data in rules.items():
        if event_name.startswith("_") or event_name == "ignore":
            continue

        for vendor, vdata in event_data.get("vendors", {}).items():
            vendor_aliases[vendor.lower()] = vendor

            if vendor not in compiled_by_vendor:
                compiled_by_vendor[vendor] = {"rules": []}

            excl_compiled = [
                re.compile(ex)
                for ex in vdata.get("exclude_patterns", [])
            ]

            for p in vdata.get("patterns", []):
                compiled_by_vendor[vendor]["rules"].append(
                    (event_name, p["weight"], re.compile(p["regex"]), excl_compiled)
                )

    # --- Precompile timestamp formats (a list of regex + strptime pairs per vendor) ---
    timestamp_formats = {}
    for vendor, fmt_list in rules.get("_timestamp_formats", {}).items():
        if vendor.startswith("_"):
            continue
        timestamp_formats[vendor] = [
            {"compiled_re": re.compile(fmt["regex"]), "strptime": fmt["strptime"]}
            for fmt in fmt_list
        ]

    rules["_vendor_aliases"]      = vendor_aliases
    rules["_compiled"]            = compiled_by_vendor
    rules["_timestamp_compiled"]  = timestamp_formats
    return rules


# ---------------------------------------------------------------------------
# Vendor resolution (case-insensitive folder name → canonical rules.json key)
# ---------------------------------------------------------------------------

def resolve_vendor(rules, folder_name):
    aliases = rules.get("_vendor_aliases", {})
    return aliases.get(folder_name.lower(), folder_name)


# ---------------------------------------------------------------------------
# Event matching — works on pre-compiled structures only
# ---------------------------------------------------------------------------

def match_events(line, compiled_vendor):
    """
    Match a single log line against pre-compiled rules for one vendor.
    Returns the first matching {event, weight} dict as a single-item list,
    or an empty list if nothing matches.

    Each line maps to at most one event — the first rule whose pattern fires
    wins and we exit immediately. This is safe because clinical event patterns
    are mutually exclusive by design.
    """
    for event_name, weight, pattern_re, excl_res in compiled_vendor["rules"]:
        if any(ex.search(line) for ex in excl_res):
            continue
        if pattern_re.search(line):
            return [{"event": event_name, "weight": weight}]
    return []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def dedupe_events(events):
    seen   = set()
    unique = []
    for e in events:
        key = (e.get("timestamp"), e.get("event_type"), e.get("line"))
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_output(device_id, vendor, events, output_root="output"):
    output_dir = os.path.join(output_root, vendor)
    os.makedirs(output_dir, exist_ok=True)
    file_path = os.path.join(output_dir, f"{device_id}_events.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2)
    return file_path


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def get_all_log_files(root_folder):
    """
    Expects folder structure:  logs/<vendor>/<device_id>/
    All files in the device folder are treated as logs — no extension filtering.
    """
    for vendor in os.listdir(root_folder):
        vendor_path = os.path.join(root_folder, vendor)
        if not os.path.isdir(vendor_path):
            continue
        for device_id in os.listdir(vendor_path):
            device_path = os.path.join(vendor_path, device_id)
            if not os.path.isdir(device_path):
                continue
            for file_name in os.listdir(device_path):
                file_path = os.path.join(device_path, file_name)
                if os.path.isfile(file_path):
                    yield {
                        "vendor":    vendor,
                        "device_id": device_id,
                        "file_path": file_path,
                        "file_name": file_name,
                    }


# ---------------------------------------------------------------------------
# Log reading — buffered for large files
# ---------------------------------------------------------------------------

def read_log_file(file_path):
    with open(file_path, "r", encoding="utf-8", errors="ignore",
              buffering=io.DEFAULT_BUFFER_SIZE * 4) as f:
        for line in f:
            yield line.strip()


# ---------------------------------------------------------------------------
# Main entry point — callable directly (GUI, scripts) or via CLI
# ---------------------------------------------------------------------------

def run_parser(log_root=LOG_ROOT, output_root="output", rules_path="rules.json", progress=print):
    """
    Run the full parse pass: scan log_root for vendor/device folders, match
    every line against the rules for that vendor, and write one normalized
    events JSON file per device under output_root.

    progress: callable taking a single string, used for status messages.
    Defaults to print() for CLI use; a GUI can pass a function that appends
    to a text widget instead.

    Returns a list of (device_id, vendor, output_file_path) for what was written.
    """
    rules = load_rules(rules_path)

    progress("Scanning for log files...\n")

    device_event_map = {}

    for file_info in get_all_log_files(log_root):
        vendor    = resolve_vendor(rules, file_info["vendor"])
        device_id = file_info["device_id"]
        key       = f"{vendor}_{device_id}"

        # Get the pre-compiled rule set for this vendor (empty bucket if unknown)
        compiled_vendor = rules["_compiled"].get(vendor, {"rules": []})

        if key not in device_event_map:
            device_event_map[key] = {
                "vendor":    vendor,
                "device_id": device_id,
                "events":    [],
            }

        progress(f"  Processing: {vendor} | {device_id} | {file_info['file_name']}")

        # Rolling buffer of recent lines for context enrichment
        recent_lines = deque(maxlen=CONTEXT_WINDOW)

        for line_number, line in enumerate(read_log_file(file_info["file_path"]), start=1):
            matches = match_events(line, compiled_vendor)

            for m in matches:
                timestamp = extract_timestamp(line, vendor, rules["_timestamp_compiled"])
                extra     = extract_context_fields(line, recent_lines)

                record = {
                    "device_id":   device_id,
                    "vendor":      vendor,
                    "timestamp":   timestamp,
                    "source_file": file_info["file_name"],
                    "line_number": line_number,
                    "event_type":  m["event"],
                    "weight":      m["weight"],
                    "line":        line,
                }

                if extra:
                    record.update(extra)

                device_event_map[key]["events"].append(record)

            recent_lines.append(line)

    # Deduplicate, sort, and save
    written = []
    for key, data in device_event_map.items():
        progress(f"\nDeduplicating events for {data['device_id']}...")

        events = dedupe_events(data["events"])
        removed = len(data["events"]) - len(events)
        progress(f"  Removed {removed} duplicates")

        events.sort(key=lambda x: x["timestamp"] or "")

        output_dir = os.path.join(output_root, data["vendor"])
        os.makedirs(output_dir, exist_ok=True)
        file_path = save_output(data["device_id"], data["vendor"], events, output_root)
        progress(f"\nSaved output: {file_path}")

        written.append((data["device_id"], data["vendor"], file_path))

    return written


if __name__ == "__main__":
    run_parser()
