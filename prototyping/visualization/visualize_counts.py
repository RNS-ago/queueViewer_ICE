#!/usr/bin/env python3
"""
Interactive visualization of the TF-Luna people-counter logs.
====================================================================
Reads the records produced by the ESP32 logger (see esp32/src/logger.py)
and renders an interactive HTML dashboard with Plotly.

Accepts either format the device writes:
  * counts.csv   -> "ts,device_id,boot_id,event,in,out,occupancy"
  * buffer.jsonl -> one JSON object per line ({"device_id":..,"ts":..,..})

Each record carries an ``event``:
  in / out   - a person crossed the lane in that direction
  snapshot   - periodic heartbeat (the current totals)
  boot       - one-shot marker written at start-up, delimits runs

Timestamps are ISO-8601 UTC ("2026-06-27T12:34:56Z") once NTP has synced,
or an uptime marker ("uptime+12345ms") before that. Both are handled; when
a boot has no wall-clock time at all we fall back to plotting against the
record index so the run is still visible.

Usage
-----
    python visualize_counts.py counts.csv
    python visualize_counts.py buffer.jsonl -o dashboard.html
    python visualize_counts.py counts.csv --device entrance-01 --no-open

Requires: plotly, pandas  (see requirements.txt)
"""

import argparse
import json
import os
import sys
import webbrowser
from datetime import datetime, timedelta, timezone

try:
    import pandas as pd
except ImportError:
    sys.exit("This script needs pandas and plotly. Install them with:\n"
             "    pip install -r requirements.txt")

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except ImportError:
    sys.exit("This script needs plotly. Install it with:\n"
             "    pip install -r requirements.txt")


COLUMNS = ["ts", "device_id", "boot_id", "event", "in", "out", "occupancy"]


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------
def _load_jsonl(path):
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                print("skipping malformed line:", line[:60], file=sys.stderr)
    return pd.DataFrame(rows)


def load_records(path):
    """Read a counts.csv or buffer.jsonl into a tidy DataFrame."""
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jsonl", ".json", ".ndjson"):
        df = _load_jsonl(path)
    else:
        df = pd.read_csv(path)

    if df.empty:
        sys.exit("No records found in " + path)

    # The logger uses bare "in"/"out" keys; normalise and fill any gaps.
    for col in COLUMNS:
        if col not in df.columns:
            df[col] = None
    for col in ("in", "out", "occupancy"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    # occupancy is derivable when the column is absent/empty.
    df["occupancy"] = df["occupancy"].fillna(df["in"] - df["out"])
    return df


# --------------------------------------------------------------------------
# Timestamp handling
# --------------------------------------------------------------------------
def parse_timestamps(df):
    """Add a ``time`` column (datetime) and a ``has_walltime`` flag.

    ISO-8601 stamps map straight to wall-clock time. "uptime+<ms>" markers
    have no absolute reference, so we anchor each boot's uptime stream to the
    first wall-clock time we *do* see in that boot (or, failing that, leave
    them as offsets from an arbitrary epoch so ordering is preserved).
    """
    ts = df["ts"].astype(str)
    is_uptime = ts.str.startswith("uptime+")

    wall = pd.to_datetime(ts.where(~is_uptime), utc=True, errors="coerce")

    uptime_ms = (ts.where(is_uptime)
                   .str.replace("uptime+", "", regex=False)
                   .str.replace("ms", "", regex=False))
    uptime_ms = pd.to_numeric(uptime_ms, errors="coerce")

    time = wall.copy()
    df["_uptime_ms"] = uptime_ms

    # Anchor uptime markers per boot to that boot's first real timestamp.
    for boot_id, grp in df.groupby("boot_id"):
        idx = grp.index
        grp_wall = wall.loc[idx]
        grp_up = uptime_ms.loc[idx]
        need = grp_up.notna() & grp_wall.isna()
        if not need.any():
            continue
        first_wall = grp_wall.dropna().min()
        if pd.notna(first_wall):
            # No row carries both an uptime and a wall-clock stamp, so the true
            # offset is unrecoverable. Uptime markers are written before NTP
            # sync — i.e. before this run's first real timestamp — so lay them
            # out in uptime order, ending just before that first wall-clock row.
            u = grp_up.loc[idx[need]]
            time.loc[idx[need]] = (first_wall
                                   - pd.to_timedelta(u.max() - u, unit="ms")
                                   - timedelta(seconds=1))
        else:
            # Whole run never synced: lay it on the epoch so the shape is still
            # visible (this run's axis will read 1970, by design).
            anchor = datetime(1970, 1, 1, tzinfo=timezone.utc)
            time.loc[idx[need]] = anchor + pd.to_timedelta(grp_up.loc[idx[need]], unit="ms")

    df["time"] = time
    df["has_walltime"] = wall.notna()
    return df


# --------------------------------------------------------------------------
# Plotting
# --------------------------------------------------------------------------
def build_figure(df, title):
    boots = df[df["event"] == "boot"]
    series = df[df["event"].isin(["snapshot", "boot"])]

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.09,
        row_heights=[0.5, 0.5],
        subplot_titles=("Occupancy (people inside)",
                        "Cumulative in / out per run"))

    # 1) Occupancy over time -------------------------------------------------
    fig.add_trace(go.Scatter(
        x=series["time"], y=series["occupancy"], mode="lines+markers",
        line=dict(color="#4FC3F7", width=2, shape="hv"),
        marker=dict(size=6, color="#4FC3F7"), name="occupancy",
        fill="tozeroy", fillcolor="rgba(79,195,247,0.12)",
        hovertemplate="%{x}<br>occupancy: %{y}<extra></extra>"),
        row=1, col=1)

    # 2) Cumulative totals (reset to 0 at each boot) -------------------------
    fig.add_trace(go.Scatter(
        x=series["time"], y=series["in"], mode="lines+markers",
        line=dict(color="#66BB6A", width=2), marker=dict(size=6, color="#66BB6A"),
        name="in (total)",
        hovertemplate="%{x}<br>in: %{y}<extra></extra>"),
        row=2, col=1)
    fig.add_trace(go.Scatter(
        x=series["time"], y=series["out"], mode="lines+markers",
        line=dict(color="#FF7043", width=2), marker=dict(size=6, color="#FF7043"),
        name="out (total)",
        hovertemplate="%{x}<br>out: %{y}<extra></extra>"),
        row=2, col=1)

    # Boot markers: a labelled vertical divider across both panels so each new
    # run (counts reset to 0) is obvious at a glance.
    n = 0
    for _, b in boots.iterrows():
        if pd.isna(b["time"]):
            continue
        n += 1
        fig.add_vline(x=b["time"], row="all", col=1,
                      line=dict(color="#FFB74D", width=2, dash="dash"))
        fig.add_annotation(
            x=b["time"], y=1.0, xref="x", yref="paper",
            text="⏻ boot {}".format(n), showarrow=False,
            font=dict(color="#FFB74D", size=11),
            bgcolor="rgba(17,17,17,0.75)", borderpad=2,
            xanchor="left", yanchor="bottom")

    # Whole-number count axes — fractional people make no sense.
    cmax = float(pd.concat([series["in"], series["out"], series["occupancy"]]).max() or 0)
    dtick = 1 if cmax <= 12 else None
    fig.update_yaxes(title_text="people", row=1, col=1, tickformat="d",
                     rangemode="tozero", dtick=dtick)
    fig.update_yaxes(title_text="count", row=2, col=1, tickformat="d",
                     rangemode="tozero", dtick=dtick)
    fig.update_xaxes(title_text="time", row=2, col=1)

    fig.update_layout(
        title=title, hovermode="x unified", template="plotly_dark",
        height=720, legend=dict(orientation="h", yanchor="bottom", y=1.02,
                                xanchor="right", x=1),
        margin=dict(t=90))
    return fig


def summarize(df):
    n_snap = int((df["event"] == "snapshot").sum())
    devices = ", ".join(sorted(str(d) for d in df["device_id"].dropna().unique()))
    boots = df["boot_id"].nunique()
    span = ""
    walled = df[df["has_walltime"]]
    if not walled.empty:
        span = " | {} -> {}".format(walled["time"].min(), walled["time"].max())
    print("Loaded {} records | devices: {} | boots: {} | snapshots: {}{}".format(
              len(df), devices or "?", boots, n_snap, span))
    # Report the last snapshot of each run (in/out reset to 0 at every boot).
    for boot_id, grp in df.groupby("boot_id", sort=False):
        last = grp.iloc[-1]
        print("  run {}: in={} out={} occupancy={}".format(
            boot_id, int(last["in"]), int(last["out"]), int(last["occupancy"])))


def write_dark_html(fig, out):
    """Write a full HTML page whose body matches the dark plot theme.

    Plotly's own write_html only themes the plot area, leaving the surrounding
    page white; we wrap the figure div in a dark-background document instead.
    """
    inner = fig.to_html(full_html=False, include_plotlyjs="cdn")
    page = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Queue counter dashboard</title>
<style>
  html, body {{ margin: 0; padding: 0; background: #111111; color: #e0e0e0;
                font-family: -apple-system, "Segoe UI", Roboto, sans-serif; }}
  .plot-wrap {{ max-width: 1100px; margin: 0 auto; padding: 12px; }}
</style>
</head>
<body>
  <div class="plot-wrap">{inner}</div>
</body>
</html>""".format(inner=inner)
    with open(out, "w") as f:
        f.write(page)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logfile", help="counts.csv or buffer.jsonl from the device")
    ap.add_argument("-o", "--output", default=None,
                    help="HTML output path (default: alongside the input)")
    ap.add_argument("--device", default=None,
                    help="only plot this device_id")
    ap.add_argument("--no-open", action="store_true",
                    help="write the HTML but don't open a browser")
    args = ap.parse_args()

    if not os.path.exists(args.logfile):
        sys.exit("No such file: " + args.logfile)

    df = load_records(args.logfile)
    if args.device:
        df = df[df["device_id"] == args.device]
        if df.empty:
            sys.exit("No records for device " + args.device)

    df = parse_timestamps(df)
    # Plot in chronological order; index ordering is the tiebreaker.
    df = df.sort_values("time", kind="stable").reset_index(drop=True)

    summarize(df)

    title = "Queue counter — {}".format(
        args.device or ", ".join(sorted(str(d) for d in df["device_id"].dropna().unique())) or "all devices")
    fig = build_figure(df, title)

    out = args.output or os.path.splitext(args.logfile)[0] + "_dashboard.html"
    write_dark_html(fig, out)
    print("Wrote", out)

    if not args.no_open:
        webbrowser.open("file://" + os.path.abspath(out))


if __name__ == "__main__":
    main()
