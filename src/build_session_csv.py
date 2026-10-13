"""Extract per-window traffic statistics from one or all session directories
and append them to a shared, accumulating CSV per window size.

A session directory must contain dump.pcap and exp.txt (requests_history.json
is optional). Running this against multiple session directories with the
same --window-seconds value builds up one combined dataset; a different
--window-seconds writes to a differently-named file, so window sizes never
mix together.

"all" for session_directory discovers every session directory under
--source-dir (default: the parent of the current working directory, since
the real session directories normally sit as siblings of this repo). Pass
--source-dir to scan somewhere else directly -- e.g. a NAS mount -- without
copying anything locally first. --batch-index/--batch-size slice that
(sorted) directory listing into fixed-size chunks, so a huge source
directory can be worked through a batch at a time across multiple runs:
batch 1 is entries [0:batch_size), batch 2 is [batch_size:2*batch_size), etc.
Output CSVs always land in the current working directory, regardless of
--source-dir.

"all" also parallelizes across (directory, window-size) combinations using
a process pool, since each one is fully independent -- packet parsing and
per-flow state tracking are CPU-bound, branchy Python work with no benefit
from threads (the GIL would just serialize them) or a GPU (no bulk uniform
numeric computation to vectorize; the real cost is per-packet sequential
logic, not matrix math). Only the actual CSV-writing is kept serialized,
done in the main process as results come back, so concurrent workers never
write to the same output file at once. Defaults to one worker per CPU core;
override with --workers.

Usage:
    python src/build_session_csv.py <session_directory> [--window-seconds 10] [--output path.csv]
    python src/build_session_csv.py all                      # every session directory found in ../, 10s windows
    python src/build_session_csv.py all --window-seconds all # every directory x every window size (10/8/4/2/1)
    python src/build_session_csv.py all --source-dir /mnt/NAS/salmo-trutta/traces --batch-index 2 --batch-size 500
    python src/build_session_csv.py all --window-seconds all --workers 16
"""

import argparse
import os
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed

from window_features import build_session_dataframe

ALL_WINDOW_SIZES = (10, 8, 4, 2, 1)
REQUIRED_FILES = ("dump.pcap", "exp.txt")
SERVICE_NAMES = ("youtube", "netflix", "twitch", "amazon")


def parse_exp_label(exp_path):
    """Extract the service name from exp.txt.

    Two shapes have been seen:
      - "<prefix>_<service>.<suffix>": "test1-05_twitch.run276-...",
        "princ-paul_netflix.run2061527974066", "lab10_youtube.run30-1549387740"
        -- service sits right after the underscore, before the first dot.
      - controlled-experiment runs where the service leads and everything
        between it and ".run" is experiment config, not part of the label:
        "youtube-0_rate+3000kbps+loss+1%+25%.run2".
    Rather than parse position (which these two shapes disagree on), just
    search for whichever known service name appears in the string -- it's
    present verbatim in every shape seen so far, so this sidesteps having to
    special-case each new condition-string format.
    """
    content = open(exp_path).read().strip().lower()
    for service in SERVICE_NAMES:
        if service in content:
            return service
    raise ValueError(f"no known service name found in {exp_path}: {content!r}")


def discover_session_directories(root="..", batch_index=None, batch_size=500):
    entries = sorted(entry for entry in os.listdir(root) if not entry.startswith("."))

    if batch_index is not None:
        start = (batch_index - 1) * batch_size
        entries = entries[start:start + batch_size]

    found = []
    for entry in entries:
        path = os.path.join(root, entry)
        if not os.path.isdir(path):
            continue
        if all(os.path.isfile(os.path.join(path, f)) for f in REQUIRED_FILES):
            found.append(path)
    return found


def _window_seconds_label(window_seconds):
    return int(window_seconds) if window_seconds == int(window_seconds) else window_seconds


def _output_path_for(window_seconds, output=None):
    return output or f"session_statistics_{_window_seconds_label(window_seconds)}_second_window.csv"


def compute_session_dataframe(session_dir, window_seconds):
    """The CPU-bound part only -- no file writes. Safe to run in a worker
    process; the caller is responsible for writing the result."""
    session_dir = session_dir.rstrip("/")
    session_id = os.path.basename(session_dir)

    label = parse_exp_label(os.path.join(session_dir, "exp.txt"))

    requests_path = os.path.join(session_dir, "requests_history.json")
    if not os.path.isfile(requests_path):
        requests_path = None

    session_df = build_session_dataframe(
        session_dir, window_seconds, label, session_id, requests_path
    )
    return session_id, session_df


def _write_session_df(output_path, session_id, session_df):
    file_exists = os.path.isfile(output_path)
    session_df.to_csv(output_path, mode="a", header=not file_exists, index=False)

    final_label = session_df["label"].iloc[0]
    print(f"{len(session_df)} windows for session {session_id} ({final_label}) -> {output_path}")


def process_session(session_dir, window_seconds, output=None):
    session_id, session_df = compute_session_dataframe(session_dir, window_seconds)
    _write_session_df(_output_path_for(window_seconds, output), session_id, session_df)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_directory", help='a session directory path, or "all"')
    parser.add_argument("--window-seconds", default="10", help='a number, or "all"')
    parser.add_argument("--output", default=None, help="only valid with a single directory and window size")
    parser.add_argument("--source-dir", default="..", help='where "all" scans for session directories')
    parser.add_argument("--batch-index", type=int, default=None, help="1-based batch number (with --batch-size)")
    parser.add_argument("--batch-size", type=int, default=500, help="directories per batch (default 500)")
    parser.add_argument(
        "--workers", type=int, default=None, help="parallel worker processes for 'all' (default: all CPU cores)"
    )
    return parser.parse_args()


def main():
    args = parse_args()
    is_all_directories = args.session_directory.lower() == "all"

    if is_all_directories:
        directories = discover_session_directories(
            root=args.source_dir, batch_index=args.batch_index, batch_size=args.batch_size
        )
        print(f"found {len(directories)} session directories")
    else:
        directories = [args.session_directory]

    if args.window_seconds.lower() == "all":
        window_sizes = list(ALL_WINDOW_SIZES)
    else:
        window_sizes = [float(args.window_seconds)]

    if args.output and (len(directories) > 1 or len(window_sizes) > 1):
        raise SystemExit("--output can only be used with one directory and one window size")

    tasks = [(session_dir, window_seconds) for session_dir in directories for window_seconds in window_sizes]
    failures = []

    if is_all_directories and len(tasks) > 1:
        workers = args.workers or os.cpu_count()
        print(f"processing {len(tasks)} (directory, window-size) combinations with {workers} workers...")
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(compute_session_dataframe, session_dir, window_seconds): (session_dir, window_seconds)
                for session_dir, window_seconds in tasks
            }
            for future in as_completed(futures):
                session_dir, window_seconds = futures[future]
                try:
                    session_id, session_df = future.result()
                    _write_session_df(_output_path_for(window_seconds, args.output), session_id, session_df)
                except Exception:
                    failures.append((session_dir, window_seconds))
                    print(f"FAILED: {session_dir} at {window_seconds}s")
                    traceback.print_exc()
    else:
        for session_dir, window_seconds in tasks:
            try:
                process_session(session_dir, window_seconds, args.output)
            except Exception:
                failures.append((session_dir, window_seconds))
                print(f"FAILED: {session_dir} at {window_seconds}s")
                traceback.print_exc()

    if len(tasks) > 1:
        print(f"\n{len(tasks) - len(failures)}/{len(tasks)} session x window-size runs succeeded")
        if failures:
            print("failed:", failures)


if __name__ == "__main__":
    main()
