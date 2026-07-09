import json
import os
import csv
from datetime import datetime, timedelta
from collections import defaultdict

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

INPUT_ROOT          = "output"
SESSION_GAP_MINUTES = 5    # idle gap that splits sessions
QUALIFYING_WEIGHT   = 0.5   # minimum event weight to include in sessions
WINDOW_DAYS         = 5    # target window length for "busiest period" analysis
MIN_SCAN_EVENTS     = 1     # minimum scan events for a session to be valid


# ---------------------------------------------------------------------------
# Hard session boundary events
# System boots and shutdowns always split sessions regardless of gap size.
# ---------------------------------------------------------------------------

HARD_BOUNDARY_EVENTS = {"system_boot", "system_shutdown"}

# Events that count as "real scanning" for validity and classification
SCAN_EVENTS      = {"scan_started", "freeze_pressed", "image_adjustment"}
SAVE_EVENTS      = {"image_saved", "image_archived_local"}
LIFECYCLE_EVENTS = {"exam_started", "exam_completed", "exam_ended_by_user"}


# ---------------------------------------------------------------------------
# Rules loader
# ---------------------------------------------------------------------------

def load_rules(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def load_event_files():
    """Yield (vendor, file_name, events) for every _events.json under INPUT_ROOT."""
    for vendor in os.listdir(INPUT_ROOT):
        vendor_path = os.path.join(INPUT_ROOT, vendor)
        if not os.path.isdir(vendor_path):
            continue
        for file_name in os.listdir(vendor_path):
            if file_name.endswith("_events.json"):
                file_path = os.path.join(vendor_path, file_name)
                with open(file_path, "r", encoding="utf-8") as f:
                    events = json.load(f)
                yield vendor, file_name, events


# ---------------------------------------------------------------------------
# Session building
# ---------------------------------------------------------------------------

def build_sessions(events):
    """
    Group events into sessions using two rules:
      1. Hard boundaries: system_boot and system_shutdown always end/start a session.
      2. Time gap: any idle gap > SESSION_GAP_MINUTES splits the session.

    Events must already be timestamp-sorted.
    """
    if not events:
        return []

    sessions   = []
    current    = []
    prev_time  = None

    for e in events:
        event_time  = datetime.fromisoformat(e["timestamp"])
        event_type  = e.get("event_type", "")
        is_boundary = event_type in HARD_BOUNDARY_EVENTS

        if not current:
            current.append(e)
        else:
            gap_minutes = (event_time - prev_time).total_seconds() / 60

            if is_boundary or gap_minutes > SESSION_GAP_MINUTES:
                # Close current session, include the boundary event in the NEW one
                sessions.append(current)
                current = [e]
            else:
                current.append(e)

        prev_time = event_time

    if current:
        sessions.append(current)

    return sessions


# ---------------------------------------------------------------------------
# Session summarization
# ---------------------------------------------------------------------------

def classify_session(event_counts, category_counts):
    """
    Return a string classification based on what events were present.

    FULL_WORKFLOW   — scanning + at least one save + exam lifecycle
    SCAN_SAVED      — scanning + at least one save, but no lifecycle events
    GHOST_SCAN      — scanning but nothing saved
    LIFECYCLE_ONLY  — lifecycle/system events, no active scanning
    NOISE           — nothing meaningful
    """
    scan_count     = sum(event_counts.get(e, 0) for e in SCAN_EVENTS)
    save_count     = sum(event_counts.get(e, 0) for e in SAVE_EVENTS)
    lifecycle_count = sum(event_counts.get(e, 0) for e in LIFECYCLE_EVENTS)

    if scan_count < MIN_SCAN_EVENTS:
        if lifecycle_count > 0:
            return "LIFECYCLE_ONLY"
        return "NOISE"

    if save_count > 0 and lifecycle_count > 0:
        return "FULL_WORKFLOW"
    if save_count > 0:
        return "SCAN_SAVED"
    return "GHOST_SCAN"


def summarize_session(session, rules):
    start_dt = datetime.fromisoformat(session[0]["timestamp"])
    end_dt   = datetime.fromisoformat(session[-1]["timestamp"])
    duration = (end_dt - start_dt).total_seconds()

    event_counts    = defaultdict(int)
    category_counts = defaultdict(int)
    total_weight    = 0.0
    applications    = set()
    probes          = set()

    for e in session:
        etype = e.get("event_type", "unknown")
        event_counts[etype] += 1

        # Safe category lookup — skip events not in rules (e.g. _comment, unknowns)
        rule = rules.get(etype)
        if rule and not etype.startswith("_"):
            category_counts[rule["category"]] += 1

        total_weight += e.get("weight", 0.0)

        if e.get("application"):
            applications.add(e["application"])
        if e.get("probe"):
            probes.add(e["probe"])

    event_count = len(session)
    avg_weight  = total_weight / event_count if event_count > 0 else 0.0

    classification = classify_session(dict(event_counts), dict(category_counts))

    return {
        "start_time":       session[0]["timestamp"],
        "end_time":         session[-1]["timestamp"],
        "duration_sec":     duration,
        "classification":   classification,
        "event_count":      event_count,
        "event_counts":     dict(event_counts),
        "category_counts":  dict(category_counts),
        "total_weight":     round(total_weight, 3),
        "avg_weight":       round(avg_weight, 3),
        "scan_count":       sum(event_counts.get(e, 0) for e in SCAN_EVENTS),
        "save_count":       sum(event_counts.get(e, 0) for e in SAVE_EVENTS),
        "applications":     sorted(applications),
        "probes":           sorted(probes),
    }


def is_valid_session(summary):
    """
    A session is valid (represents a real scan encounter) if it has at least
    MIN_SCAN_EVENTS scan-type events. LIFECYCLE_ONLY and NOISE sessions are
    retained in output for completeness but flagged accordingly.
    """
    return summary["classification"] not in ("NOISE", "LIFECYCLE_ONLY")


# ---------------------------------------------------------------------------
# Time span analysis
# ---------------------------------------------------------------------------

def analyze_time_span(events):
    timestamps = sorted(e["timestamp"] for e in events if e.get("timestamp"))
    if not timestamps:
        return None
    start_dt = datetime.fromisoformat(timestamps[0])
    end_dt   = datetime.fromisoformat(timestamps[-1])
    days     = (end_dt - start_dt).days + 1
    return {"start_time": timestamps[0], "end_time": timestamps[-1], "total_days": days}


# ---------------------------------------------------------------------------
# Best-window selection
# ---------------------------------------------------------------------------

def select_best_window(events):
    """
    Slide a WINDOW_DAYS window across the event timeline and return the
    events from the densest period.

    If the log is shorter than WINDOW_DAYS, the whole log is returned and
    window_days reflects the actual log length (not WINDOW_DAYS).
    """
    if not events:
        return events, None

    events_by_day = defaultdict(int)
    for e in events:
        day = e["timestamp"][:10]
        events_by_day[day] += 1

    sorted_days = sorted(events_by_day.keys())
    log_span_days = (
        datetime.fromisoformat(sorted_days[-1]) -
        datetime.fromisoformat(sorted_days[0])
    ).days + 1

    effective_window = min(WINDOW_DAYS, log_span_days)

    best_window  = None
    max_events   = 0

    for i in range(len(sorted_days)):
        start_day = datetime.fromisoformat(sorted_days[i])
        end_day   = start_day + timedelta(days=effective_window)
        total     = sum(
            events_by_day[d]
            for d in sorted_days
            if start_day <= datetime.fromisoformat(d) < end_day
        )
        if total > max_events:
            max_events   = total
            best_window  = (start_day, end_day)

    print(f"  Window: {best_window[0].date()} → {best_window[1].date()} "
          f"({effective_window}d, events={max_events})")

    filtered = [
        e for e in events
        if best_window[0] <= datetime.fromisoformat(e["timestamp"]) < best_window[1]
    ]

    return filtered, best_window


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_sessions(vendor, file_name, all_summaries, valid_summaries, window_info=None):
    device_id  = file_name.replace("_events.json", "")
    output_dir = os.path.join("output_sessions", vendor)
    os.makedirs(output_dir, exist_ok=True)

    output = {
        "device_id":        device_id,
        "total_sessions":   len(all_summaries),
        "valid_sessions":   len(valid_summaries),
        "classification_counts": defaultdict(int),
        "sampled_window":   window_info,  # the "busiest week" window used for the rate/shape stats in the CSV - NOT a filter on the sessions below
        "sessions":         all_summaries,   # ALL sessions across the full log span; no truncation, no windowing
    }

    for s in all_summaries:
        output["classification_counts"][s["classification"]] += 1
    output["classification_counts"] = dict(output["classification_counts"])

    file_path = os.path.join(output_dir, f"{device_id}_sessions.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    print(f"  Saved sessions: {file_path}")


def save_summary_csv(summary_rows):
    if not summary_rows:
        return
    file_path = "device_summary.csv"
    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_rows[0].keys())
        writer.writeheader()
        writer.writerows(summary_rows)
    print(f"\nSaved summary CSV: {file_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    rules = load_rules("rules.json")
    all_device_summaries = []

    for vendor, file_name, events in load_event_files():
        print(f"\nProcessing {vendor} | {file_name}")

        device_id = file_name.replace("_events.json", "")

        # --- Filter to timestamped, qualifying events ---
        events = [e for e in events if e.get("timestamp") and e.get("weight", 0) >= QUALIFYING_WEIGHT]
        total_events_full = len(events)

        # --- Sort once; all downstream functions rely on this ---
        events.sort(key=lambda x: x["timestamp"])

        # --- FULL DATASET sessions ---
        sessions_full     = build_sessions(events)
        summaries_full    = [summarize_session(s, rules) for s in sessions_full]
        valid_full        = [s for s in summaries_full if is_valid_session(s)]

        time_info         = analyze_time_span(events)
        total_days_full   = time_info["total_days"] if time_info else 0
        sessions_per_day_full = (
            len(sessions_full) / total_days_full if total_days_full > 0 else 0
        )

        # --- WINDOW selection ---
        filtered_events, best_window = select_best_window(events)

        # --- WINDOW sessions ---
        sessions_window   = build_sessions(filtered_events)
        summaries_window  = [summarize_session(s, rules) for s in sessions_window]
        valid_window      = [s for s in summaries_window if is_valid_session(s)]

        events_window     = len(filtered_events)

        if best_window:
            window_start_dt, window_end_dt = best_window
            window_start  = window_start_dt.strftime("%Y-%m-%d")
            window_end    = window_end_dt.strftime("%Y-%m-%d")
            # Use actual data span, not WINDOW_DAYS, so per-day rates are honest
            window_days   = (window_end_dt - window_start_dt).days
            sessions_per_day_window = (
                len(sessions_window) / window_days if window_days > 0 else 0
            )
        else:
            window_start = window_end = ""
            window_days  = 0
            sessions_per_day_window = 0

        # --- Averages over WINDOW valid sessions ---
        if valid_window:
            avg_weight   = sum(s["total_weight"]  for s in valid_window) / len(valid_window)
            avg_events   = sum(s["event_count"]   for s in valid_window) / len(valid_window)
            avg_duration = sum(s["duration_sec"]  for s in valid_window) / len(valid_window)
            avg_saves    = sum(s["save_count"]     for s in valid_window) / len(valid_window)
            avg_scans    = sum(s["scan_count"]     for s in valid_window) / len(valid_window)
        else:
            avg_weight = avg_events = avg_duration = avg_saves = avg_scans = 0

        # --- Classification counts for FULL dataset (the true total) ---
        cls_counts_full = defaultdict(int)
        for s in summaries_full:
            cls_counts_full[s["classification"]] += 1

        # --- Classification counts for WINDOW (a representative-week sample) ---
        cls_counts = defaultdict(int)
        for s in summaries_window:
            cls_counts[s["classification"]] += 1

        # --- Debug summary ---
        print(f"  FULL:   events={total_events_full}, sessions={len(sessions_full)}, valid={len(valid_full)}")
        print(f"  FULL classification:   { {k: cls_counts_full[k] for k in sorted(cls_counts_full)} }")
        print(f"  WINDOW: events={events_window}, sessions={len(sessions_window)}, valid={len(valid_window)}")
        print(f"  WINDOW classification: { {k: cls_counts[k] for k in sorted(cls_counts)} }")

        # --- Save detailed session JSON (full dataset - all sessions, not just the window) ---
        window_info = {"start": window_start, "end": window_end, "days": window_days} if best_window else None
        save_sessions(vendor, file_name, summaries_full, valid_full, window_info)

        # --- CSV summary row ---
        summary_row = {
            "device_id":   device_id,
            "vendor":      vendor,

            # Full dataset
            "total_days_full":           total_days_full,
            "total_events_full":         total_events_full,
            "total_sessions_full":       len(sessions_full),
            "valid_sessions_full":       len(valid_full),
            "sessions_per_day_full":     round(sessions_per_day_full, 2),

            # Classification breakdown over the FULL dataset - this is the true total,
            # use this for exam-count estimates rather than the window figures below,
            # which can badly undercount devices with bursty/episodic usage patterns.
            "full_workflow_full":        cls_counts_full.get("FULL_WORKFLOW", 0),
            "scan_saved_full":           cls_counts_full.get("SCAN_SAVED", 0),
            "ghost_scans_full":          cls_counts_full.get("GHOST_SCAN", 0),
            "lifecycle_only_full":       cls_counts_full.get("LIFECYCLE_ONLY", 0),
            "noise_full":                cls_counts_full.get("NOISE", 0),

            # Window (a sampled "representative week" - useful for session-shape stats
            # like avg duration/events per session, NOT for total exam counts)
            "window_start":              window_start,
            "window_end":                window_end,
            "window_days":               window_days,
            "events_window":             events_window,
            "sessions_window":           len(sessions_window),
            "valid_sessions_window":     len(valid_window),
            "sessions_per_day_window":   round(sessions_per_day_window, 2),

            # Classification breakdown (window only - a sample, not the total)
            "full_workflow_window":      cls_counts.get("FULL_WORKFLOW", 0),
            "scan_saved_window":         cls_counts.get("SCAN_SAVED", 0),
            "ghost_scans_window":        cls_counts.get("GHOST_SCAN", 0),
            "lifecycle_only_window":     cls_counts.get("LIFECYCLE_ONLY", 0),
            "noise_window":              cls_counts.get("NOISE", 0),

            # Session characteristics (window valid sessions)
            "avg_events_per_session":    round(avg_events, 2),
            "avg_scans_per_session":     round(avg_scans, 2),
            "avg_saves_per_session":     round(avg_saves, 2),
            "avg_weight_per_session":    round(avg_weight, 2),
            "avg_session_duration_sec":  round(avg_duration, 2),
        }

        all_device_summaries.append(summary_row)

    save_summary_csv(all_device_summaries)
