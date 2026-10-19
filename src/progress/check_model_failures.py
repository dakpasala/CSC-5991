"""Scan an AutoGluon training log for models that failed to fit (usually a
missing dependency, e.g. "import lightgbm failed"), and print a clean,
deduplicated list of (model, exception type, any install tip AutoGluon
printed) -- so you don't have to scroll through the whole log to find them.

Usage:
    python src/check_model_failures.py train10.log
"""

import re
import sys

FAILURE_RE = re.compile(r"Exception caused (\S+) to fail during training \((\w+)\)")
TIP_RE = re.compile(r"(pip install \S+)")

path = sys.argv[1] if len(sys.argv) > 1 else "train.log"

seen = {}  # (base_model_name, exception_type) -> tip or None
with open(path) as handle:
    lines = handle.readlines()

for i, line in enumerate(lines):
    match = FAILURE_RE.search(line)
    if not match:
        continue
    model, exc_type = match.groups()
    base_model = re.sub(r"_BAG_L\d+$", "", model)  # fold-level names collapse to one entry

    tip = None
    for lookahead in lines[i : i + 4]:
        tip_match = TIP_RE.search(lookahead)
        if tip_match:
            tip = tip_match.group(1).rstrip("`.,;: ")
            break

    key = (base_model, exc_type)
    if key not in seen or (tip and not seen[key]):
        seen[key] = tip

if not seen:
    print(f"no model failures found in {path}")
else:
    print(f"{len(seen)} distinct model failure(s) in {path}:\n")
    for (model, exc_type), tip in sorted(seen.items()):
        print(f"- {model}: {exc_type}")
        if tip:
            print(f"    try: {tip}")
