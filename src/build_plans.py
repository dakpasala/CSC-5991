"""Merge category CSVs' `category` column into plans/*.json.

Adds any domain not already present in a plan file; never overwrites or
removes an existing entry. file_download and excluded_conferencing/unknown
rows are skipped -- file_download needs a download_url these CSVs can't
supply, and conferencing is out of scope for collection.
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "analysis" / "current_origins_verified.csv"
SOURCE_SCOPE = "current_origins_verified_2026-09-22"

CATEGORY_DEFAULTS = {
    "web_browsing": {},
    "social_media_browsing": {"content_selector": "article"},
    "video_streaming": {},
    "audio_streaming": {},
}


def load_csv_by_category():
    by_category = {category: [] for category in CATEGORY_DEFAULTS}
    with SOURCE.open() as stream:
        for row in csv.DictReader(stream):
            if row["category"] in by_category:
                by_category[row["category"]].append(
                    (int(row["rank"]), row["domain"], SOURCE_SCOPE)
                )
    return by_category


def merge_plan(category, csv_entries):
    path = ROOT / "plans" / f"{category}.json"
    existing = json.loads(path.read_text()) if path.exists() else []
    existing_domains = {entry["domain"] for entry in existing}
    added = 0
    for rank, domain, source_scope in sorted(csv_entries):
        if domain in existing_domains:
            continue
        entry = {
            "rank": rank,
            "domain": domain,
            "url": f"https://{domain}/",
            **CATEGORY_DEFAULTS[category],
            "source_scope": source_scope,
            "activity": category,
        }
        existing.append(entry)
        existing_domains.add(domain)
        added += 1
    path.write_text(json.dumps(existing, indent=2) + "\n")
    return added, len(existing)


def main():
    by_category = load_csv_by_category()
    for category, entries in by_category.items():
        added, total = merge_plan(category, entries)
        print(f"{category}: +{added} new from CSV, {total} total in plan")


if __name__ == "__main__":
    main()
