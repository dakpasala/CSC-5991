"""Extract per-window traffic statistics from one session directory and
append them to a shared, accumulating CSV.

A session directory must contain dump.pcap, exp.txt, and (optionally)
requests_history.json. Running this against multiple session directories
with the same --window-seconds value builds up one combined dataset; a
different --window-seconds writes to a differently-named file, so window
sizes never get mixed together.

Usage:
    python src/build_session_csv.py <session_directory> [--window-seconds 10] [--output path.csv]
"""

import argparse
import os

from window_features import build_session_dataframe


def parse_exp_label(exp_path):
    """Extract the service name from exp.txt.

    Verified format (one real example so far): "test1-05_twitch.run276-...",
    i.e. "<run prefix>_<service>.<run suffix>". Only "twitch" has actually
    been observed -- this needs re-checking against youtube/netflix/amazon
    examples once those session directories are available.
    """
    content = open(exp_path).read().strip()
    after_prefix = content.split("_", 1)[1]
    return after_prefix.split(".", 1)[0]


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_directory")
    parser.add_argument("--window-seconds", type=float, default=10)
    parser.add_argument("--output", default=None)
    return parser.parse_args()


def main():
    args = parse_args()
    session_dir = args.session_directory.rstrip("/")
    session_id = os.path.basename(session_dir)

    exp_path = os.path.join(session_dir, "exp.txt")
    label = parse_exp_label(exp_path)

    requests_path = os.path.join(session_dir, "requests_history.json")
    if not os.path.isfile(requests_path):
        requests_path = None

    window_seconds_label = (
        int(args.window_seconds) if args.window_seconds == int(args.window_seconds)
        else args.window_seconds
    )
    output_path = args.output or f"session_statistics_{window_seconds_label}_second_window.csv"

    session_df = build_session_dataframe(
        session_dir, args.window_seconds, label, session_id, requests_path
    )

    file_exists = os.path.isfile(output_path)
    session_df.to_csv(output_path, mode="a", header=not file_exists, index=False)

    print(f"{len(session_df)} windows for session {session_id} ({label}) -> {output_path}")


if __name__ == "__main__":
    main()
