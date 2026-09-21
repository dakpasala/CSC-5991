"""Merge analysis/top10k_login_requirements.csv's `category` column into plans/*.json.

Adds any domain not already present in that category's plan file, using a bare
homepage URL and category-appropriate defaults. Never overwrites or removes an
existing entry (hand-verified selectors like x.com/instagram.com's "article"
stay untouched). file_download and excluded_conferencing/unknown rows are
skipped -- file_download needs a specific download_url this CSV can't supply,
and the other two are deliberately not collection targets.
"""
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSV_PATH = ROOT / "analysis" / "top10k_login_requirements.csv"
SOURCE_SCOPE = "top10k_category_pass_2026-09-17"

CATEGORY_DEFAULTS = {
    "web_browsing": {},
    "social_media_browsing": {"content_selector": "article"},
    "video_streaming": {},
    "audio_streaming": {},
}


def load_csv_by_category():
    # `needs_more_verification` (added by a later categorization pass) marks rows
    # where the category was a no-evidence default rather than a real signal match;
    # those must not be treated as real categorizations here.
    by_category = {category: [] for category in CATEGORY_DEFAULTS}
    with CSV_PATH.open() as stream:
        for row in csv.DictReader(stream):
            if row.get("needs_more_verification") == "TRUE":
                continue
            if row["category"] in by_category:
                by_category[row["category"]].append((int(row["rank"]), row["domain"]))
    return by_category


def merge_plan(category, csv_entries):
    path = ROOT / "plans" / f"{category}.json"
    existing = json.loads(path.read_text()) if path.exists() else []
    existing_domains = {entry["domain"] for entry in existing}
    added = 0
    for rank, domain in sorted(csv_entries):
        if domain in existing_domains:
            continue
        entry = {
            "rank": rank,
            "domain": domain,
            "url": f"https://{domain}/",
            **CATEGORY_DEFAULTS[category],
            "source_scope": SOURCE_SCOPE,
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
