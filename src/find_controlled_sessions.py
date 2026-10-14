"""Scan every session directory's exp.txt and flag which ones are
controlled/throttled lab experiments -- condition text (e.g. "rate+3000kbps
+loss+1%+25%") sitting between the service name and ".run" -- as opposed to
organic real-world sessions, where the service name is immediately followed
by ".run" with nothing in between (e.g. "lab10_youtube.run30-1549387740").

Step 1 of 2: only detects and reports counts, and saves the controlled
session IDs to a text file. Doesn't touch any CSV -- that's step 2, once
you've seen how many of these there actually are.

Usage:
    python src/find_controlled_sessions.py [source_dir]
    (defaults to /mnt/NAS/salmo-trutta/traces)
"""

import os
import sys

SOURCE_DIR_DEFAULT = "/mnt/NAS/salmo-trutta/traces"
SERVICE_NAMES = ("youtube", "netflix", "twitch", "amazon")
OUTPUT_PATH = "controlled_session_ids.txt"


def is_controlled_experiment(exp_content):
    """True if there's real condition text between the service name and
    ".run" (a controlled/throttled run), False for a plain
    "<prefix>_<service>.<suffix>" organic session, None if no known
    service name is found at all."""
    content = exp_content.strip().lower()
    for service in SERVICE_NAMES:
        idx = content.find(service)
        if idx == -1:
            continue
        service_end = idx + len(service)
        return content[service_end : service_end + 4] != ".run"
    return None


def main():
    source_dir = sys.argv[1] if len(sys.argv) > 1 else SOURCE_DIR_DEFAULT

    controlled = []
    organic = []
    unclassifiable = []

    entries = sorted(entry for entry in os.listdir(source_dir) if not entry.startswith("."))
    for entry in entries:
        exp_path = os.path.join(source_dir, entry, "exp.txt")
        if not os.path.isfile(exp_path):
            continue
        content = open(exp_path).read()
        result = is_controlled_experiment(content)
        if result is True:
            controlled.append(entry)
        elif result is False:
            organic.append(entry)
        else:
            unclassifiable.append(entry)

    total = len(controlled) + len(organic) + len(unclassifiable)
    print(f"scanned {total} directories with an exp.txt")
    print(f"organic (real-world) sessions: {len(organic)}")
    print(f"controlled/lab-condition sessions: {len(controlled)}")
    if unclassifiable:
        print(f"unclassifiable (no known service keyword found): {len(unclassifiable)}")
        print("  " + ", ".join(unclassifiable[:10]) + (" ..." if len(unclassifiable) > 10 else ""))

    with open(OUTPUT_PATH, "w") as handle:
        handle.write("\n".join(controlled) + "\n")
    print(f"\nsaved {len(controlled)} controlled session IDs to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
