"""Extract per-window traffic statistics from one or all session directories
and append them to a shared, accumulating CSV per window size.

A session directory must contain dump.pcap and exp.txt (requests_history.json
is optional). Running this against multiple session directories with the
same --window-seconds value builds up one combined dataset; a different
--window-seconds writes to a differently-named file, so window sizes never
mix together.

"all" for session_directory discovers every session directory in the
PARENT of the current working directory (the real session directories sit
as siblings of this repo, not inside it) -- run this from inside the repo,
as usual. Output CSVs still land in the current working directory.

Usage:
    python src/build_session_csv.py <session_directory> [--window-seconds 10] [--output path.csv]
    python src/build_session_csv.py all                      # every session directory found in ../, 10s windows
    python src/build_session_csv.py all --window-seconds all # every directory x every window size (10/8/4/2/1)
"""

import argparse
import os
import traceback

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


def discover_session_directories(root=".."):
    found = []
    for entry in sorted(os.listdir(root)):
        path = os.path.join(root, entry)
        if not os.path.isdir(path):
            continue
        if all(os.path.isfile(os.path.join(path, f)) for f in REQUIRED_FILES):
            found.append(path)
    return found


def _window_seconds_label(window_seconds):
    return int(window_seconds) if window_seconds == int(window_seconds) else window_seconds


def process_session(session_dir, window_seconds, output=None):
    session_dir = session_dir.rstrip("/")
    session_id = os.path.basename(session_dir)

    label = parse_exp_label(os.path.join(session_dir, "exp.txt"))

    requests_path = os.path.join(session_dir, "requests_history.json")
    if not os.path.isfile(requests_path):
        requests_path = None

    output_path = output or f"session_statistics_{_window_seconds_label(window_seconds)}_second_window.csv"

    session_df = build_session_dataframe(
        session_dir, window_seconds, label, session_id, requests_path
    )

    file_exists = os.path.isfile(output_path)
    session_df.to_csv(output_path, mode="a", header=not file_exists, index=False)

    final_label = session_df["label"].iloc[0]
    print(f"{len(session_df)} windows for session {session_id} ({final_label}) -> {output_path}")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_directory", help='a session directory path, or "all"')
    parser.add_argument("--window-seconds", default="10", help='a number, or "all"')
    parser.add_argument("--output", default=None, help="only valid with a single directory and window size")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.session_directory.lower() == "all":
        directories = discover_session_directories()
        print(f"found {len(directories)} session directories")
    else:
        directories = [args.session_directory]

    if args.window_seconds.lower() == "all":
        window_sizes = list(ALL_WINDOW_SIZES)
    else:
        window_sizes = [float(args.window_seconds)]

    if args.output and (len(directories) > 1 or len(window_sizes) > 1):
        raise SystemExit("--output can only be used with one directory and one window size")

    failures = []
    for session_dir in directories:
        for window_seconds in window_sizes:
            try:
                process_session(session_dir, window_seconds, args.output)
            except Exception:
                failures.append((session_dir, window_seconds))
                print(f"FAILED: {session_dir} at {window_seconds}s")
                traceback.print_exc()

    if len(directories) > 1 or len(window_sizes) > 1:
        total = len(directories) * len(window_sizes)
        print(f"\n{total - len(failures)}/{total} session x window-size runs succeeded")
        if failures:
            print("failed:", failures)


if __name__ == "__main__":
    main()
