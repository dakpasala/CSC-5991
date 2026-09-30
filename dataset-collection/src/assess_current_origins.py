"""Evidence-only origin inventory using assess_domains' isolated single-fetch probes.

These are activity-plan proposals, never verified packet/capture labels.
"""

import argparse
from collections import Counter
import concurrent.futures
import csv
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import re
import signal
import sqlite3
import time
from urllib.parse import urlsplit

from assess_domains import isolated_probe, source_hash, valid_domain, SIGNAL_VERSION
from config import ROOT, positive

FIELDS = [
    "rank",
    "origin",
    "domain",
    "login_required",
    "basis",
    "note",
    "category",
    "category_basis",
]
ACTIVE = {
    "web_browsing",
    "social_media_browsing",
    "video_streaming",
    "audio_streaming",
    "file_download",
}
RULE_VERSION = 3


def read_origins(path):
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["origin", "rank"]:
            raise ValueError("Expected origin,rank header")
        rows = []
        for position, row in enumerate(reader, 1):
            if set(row) != {"origin", "rank"} or None in row.values():
                raise ValueError(f"Malformed input row {position}")
            rank = int(row["rank"])
            if rank < 1:
                raise ValueError(f"Invalid rank at input row {position}")
            # Extract the literal host, rather than urlsplit.hostname's lowercasing.
            parsed = urlsplit(row["origin"])
            domain = parsed.netloc.split(":", 1)[0]
            try:
                port_valid = parsed.port is None or 1 <= parsed.port <= 65535
            except ValueError:
                port_valid = False
            valid = (
                parsed.scheme in {"http", "https"}
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and parsed.path in {"", "/"}
                and port_valid
                and parsed.hostname == domain
                and valid_domain(domain)
                and re.fullmatch(r"[a-z0-9.-]+", domain)
            )
            rows.append(
                dict(
                    position=position,
                    rank=row["rank"],
                    origin=row["origin"],
                    domain=domain,
                    valid=bool(valid),
                )
            )
    return rows


def matches(pattern, text):
    return re.search(pattern, text, re.I) is not None


def assess(result):
    entries = result.get("http", [])
    if len(entries) != 1 or not 200 <= entries[0].get("status", 0) < 300:
        return "unassessable", {
            "reason": result.get("reason", "unsuccessful or missing HTTP response")
        }
    s = entries[0].get("signals", {})
    title = s.get("title", "")
    description = s.get("meta_description", "") or s.get("og_description", "")
    metadata = title + " " + description
    all_metadata = metadata + " " + s.get("og_description", "")
    # Domain strings never supply adult, category, or login evidence.
    explicit = r"\b(?:porn(?:ography|ographic|hub|o|ô)?|hentai|bokep|xxx (?:videos?|movies?)|sex (?:videos?|stories|movies?|games)|adult (?:videos?|movies?|entertainment)|nude (?:girls?|women|photos?|videos?)|live sex|sex cams?|erotic (?:videos?|movies?|stories)|uncensored (?:yaoi|hentai|porn|sex))\b"
    if matches(explicit, all_metadata):
        # Avoid confusing reporting, safety resources, or health information with porn.
        if matches(
            r"\b(?:news|report|study|research|health|education|prevention|blocking|blocker|filter|addiction|recovery|laws?|legal)\b",
            all_metadata,
        ):
            return "unassessable", {
                "reason": "explicit terms with informational context; ambiguous"
            }
        return "explicit", {"reason": "explicit-content wording in fetched title/description"}
    blocked = r"captcha|access denied|just a moment|verify (?:you are|that you)|checking your browser|enable javascript|domain.{0,20}for sale|parked domain|403 forbidden|404 not found|request rejected|security check|robot check|site cannot be reached"
    if not title or not description or matches(blocked, metadata):
        return "unassessable", {"reason": "missing metadata, challenge, error, or parked page"}
    public = s.get("public_text", "")
    if matches(blocked, public[:1800]):
        return "unassessable", {"reason": "challenge or error wording in sampled body text"}
    # Each family needs positive site-purpose evidence. Multiple families fail closed.
    editorial = matches(r"\b(?:news|reviews?|articles?|tutorials?|research|reporting)\b", metadata)
    calling = (
        matches(
            r"\b(?:(?:video conferencing|video meetings|team messaging|instant messaging) (?:software|platform|app|solutions?|service|tool)|messaging app|messaging platform|secure messenger)\b",
            metadata,
        )
        or ("video chat" in title.lower() and "video chat" in description.lower())
        or "отправлять любые виды сообщений и звонить" in description.lower()
    ) and not editorial
    video = matches(
        r"\b(?:watch (?:live |free |online )?(?:videos|movies|tv|shows)|video streaming)\b",
        metadata,
    )
    audio = matches(
        r"\b(?:listen to (?:music|podcasts|radio)|stream (?:music|audio)|music streaming|internet radio)\b",
        metadata,
    )
    social = (
        matches(
            r"\b(?:social network|social media platform|online community|discussion forum)\b",
            metadata,
        )
        and matches(r"\b(?:posts|feed|discussions|share|connect)\b", description)
        and not editorial
    )
    download = (
        matches(
            r"\b(?:download (?:free )?(?:software|files|apps|games)|software downloads|download center|download centre)\b",
            metadata,
        )
        and not editorial
    )
    candidates = []
    if calling:
        candidates.append(("excluded_conferencing", "explicit calling/messaging product wording"))
    if video:
        candidates.append(
            ("video_streaming", "explicit viewing purpose plus sampled video element")
        )
    if audio:
        candidates.append(
            ("audio_streaming", "explicit listening purpose plus sampled audio element")
        )
    if social:
        candidates.append(
            ("social_media_browsing", "social/discussion purpose and post/feed wording")
        )
    if download:
        candidates.append(("file_download", "download purpose and sampled file/download link"))
    if len(candidates) > 1:
        return "unassessable", {"reason": "conflicting activity purposes"}
    if candidates:
        category, category_reason = candidates[0]
        if category == "video_streaming" and (not s.get("video_tag") or s.get("audio_tag")):
            return "unassessable", {
                "reason": "video purpose without unambiguous sampled video element"
            }
        if category == "audio_streaming" and (not s.get("audio_tag") or s.get("video_tag")):
            return "unassessable", {
                "reason": "audio purpose without unambiguous sampled audio element"
            }
        if category == "file_download" and not s.get("download_link"):
            return "unassessable", {"reason": "download purpose without sampled file/download link"}
    else:
        # Positive content-purpose terms only. Recognizing these equivalent news
        # terms prevents language alone from deciding whether a site is assessed.
        reading = (
            r"\b(?:news|articles?|blog|read|documentation|tutorials?|encyclopedia|recipes?|guides?|"
            r"noticias|notícias|notizie|actualités|nachrichten|vijesti|wiadomości|nieuws|nyheter|"
            r"новости|новини|ειδήσεις|أخبار|الأخبار)\b|ニュース|新闻|新聞|뉴스"
        )
        corroborated = (matches(reading, title) and matches(reading, description)) or (
            s.get("og_type") == "article" and matches(reading, description)
        )
        ambiguous = matches(
            r"\b(?:listen|stream|streaming|radio|podcasts?|trailers?|watch|memes?|gifs?|feeds?|platform|marketing|hosting)\b",
            metadata,
        )
        commerce = (
            matches(r"\b(?:online shopping|online store|shop|catalogue|catalog)\b", title)
            and matches(r"\b(?:shop|shopping|buy|products|catalogue|catalog)\b", description)
            and matches(r"\b(?:products|price|add to cart|shop now)\b", public)
            and not matches(r"\b(?:build|create|software|platform|marketing|hosting)\b", metadata)
        )
        weather = (
            matches(r"\b(?:weather|forecast)\b", title)
            and matches(r"\b(?:weather|forecast)\b", description)
            and matches(r"\b(?:forecast|temperature|humidity)\b|°[CF]", public)
        )
        if (
            not (corroborated or commerce or weather)
            or ambiguous
            or s.get("video_tag")
            or s.get("audio_tag")
        ):
            return "unassessable", {
                "reason": "insufficient or ambiguous activity-specific evidence"
            }
        category = "web_browsing"
        if corroborated:
            category_reason = "corroborated reading-related title/description or article metadata"
        elif commerce:
            category_reason = (
                "shopping/catalog purpose in title and description plus public product wording"
            )
        else:
            category_reason = (
                "weather/forecast purpose in title and description plus public forecast wording"
            )
    text = metadata + " " + public
    gate_text = re.sub(
        r"\b(?:no (?:sign[ -]?up|registration|login|account) (?:is )?(?:required|needed)|without (?:an account|registration|logging in|signing in))\b",
        "",
        text,
        flags=re.I,
    )
    mandatory = matches(
        r"\b(?:sign in|log in|login|register|create an account|registration) (?:is required|to (?:continue|access|watch|listen|download|view|read|use|join))\b|\b(?:account|login|registration) required\b",
        gate_text,
    )
    no_account = matches(
        r"\b(?:no (?:sign[ -]?up|registration|login|account) (?:is )?(?:required|needed)|without (?:an account|registration|logging in|signing in))\b",
        text,
    )
    public_reading = len(public) >= 500 and len(public.split()) >= 80
    login_control = s.get("login_control") or s.get("password_input")
    if mandatory and no_account:
        return "unassessable", {"reason": "conflicting login evidence"}
    if mandatory:
        login = "partial" if public_reading and not s.get("password_input") else "hard"
        login_reason = "explicit account-gate wording" + (
            " with public text also accessible" if login == "partial" else ""
        )
    elif no_account:
        login, login_reason = "no", "explicit no-account-required wording"
    elif category == "web_browsing" and public_reading and not s.get("password_input"):
        login = "partial" if login_control else "no"
        login_reason = "substantial public reading text in unauthenticated response" + (
            " plus login entry point"
            if login_control
            else "; no account gate observed for this reading surface"
        )
    elif (
        category == "excluded_conferencing"
        and public_reading
        and login_control
        and not s.get("password_input")
    ):
        login, login_reason = (
            "partial",
            "public product information plus account login entry point; calling itself untested",
        )
    else:
        return "unassessable", {
            "reason": "category supported but login requirement not established"
        }
    note = json.dumps(
        dict(
            title=title,
            description=description,
            og_type=s.get("og_type", ""),
            video_tag=bool(s.get("video_tag")),
            audio_tag=bool(s.get("audio_tag")),
            download_link=bool(s.get("download_link")),
            public_text_chars=len(public),
            public_text_words=len(public.split()),
            login_entry_point=bool(login_control),
            password_input=bool(s.get("password_input")),
            final_url=entries[0].get("final_url"),
            login_evidence=login_reason,
            category_evidence=category_reason,
            status="planning_proposal_only",
        ),
        ensure_ascii=False,
    )
    return "retained", dict(
        login_required=login,
        basis="fetched_public_text_and_account_signals",
        note=note,
        category=category,
        category_basis="fetched_title_and_description_and_corroborating_signals",
    )


def open_database(path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
    expected = {"source_sha256": source_hash(source), "signal_version": str(SIGNAL_VERSION)}
    previous = dict(db.execute("SELECT key, value FROM metadata"))
    if previous and previous != expected:
        db.close()
        raise ValueError("Checkpoint source or signal version differs; use a new database")
    db.executemany("INSERT OR IGNORE INTO metadata VALUES (?, ?)", expected.items())
    db.execute(
        "CREATE TABLE IF NOT EXISTS origins (position INTEGER PRIMARY KEY, origin TEXT, rank TEXT, checked_at REAL, result TEXT)"
    )
    db.commit()
    return db


def export(rows, db, output, target):
    counts = Counter(
        rows_walked=0, explicit_drops=0, unassessable_skips=0, qualifying=0, excluded_conferencing=0
    )
    retained = []
    reasons = Counter()
    for row in rows:
        record = db.execute(
            "SELECT origin, rank, result FROM origins WHERE position=?", (row["position"],)
        ).fetchone()
        if record is None:
            break  # Ordered committed prefix only, even when probes finish out of order.
        if record[:2] != (row["origin"], row["rank"]):
            raise ValueError("Checkpoint row identity mismatch")
        outcome, assessment = assess(json.loads(record[2]))
        counts["rows_walked"] += 1
        if outcome != "retained":
            counts["explicit_drops" if outcome == "explicit" else "unassessable_skips"] += 1
            reasons[assessment["reason"]] += 1
        else:
            retained.append(
                {**{key: row[key] for key in ("rank", "origin", "domain")}, **assessment}
            )
            if assessment["category"] in ACTIVE:
                counts["qualifying"] += 1
            else:
                counts["excluded_conferencing"] += 1
        if counts["qualifying"] >= target:
            break
    temporary = output.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(retained)
    temporary.replace(output)
    return dict(counts), retained, dict(reasons)


def run(args):
    rows = read_origins(args.csv)
    if args.dry_run:
        print(
            json.dumps(
                dict(
                    input_rows=len(rows),
                    invalid_origins=sum(not r["valid"] for r in rows),
                    first=rows[:3],
                )
            )
        )
        return
    if args.output.resolve() in {
        args.csv.resolve(),
        (ROOT / "analysis/top10k_login_requirements.csv").resolve(),
    }:
        raise ValueError("Output cannot overwrite source or reference inventory")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    db = open_database(args.database, args.csv)
    counts, retained, reasons = export(rows, db, args.output, args.target)
    done = {r[0] for r in db.execute("SELECT position FROM origins")}
    selected = rows[: args.max_rows] if args.max_rows else rows
    pending = iter(r for r in selected if r["position"] not in done)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
    futures = {}
    exhausted = args.offline
    next_launch = 0.0
    last_export = time.monotonic()
    interrupted = False
    frontier = counts["rows_walked"]
    qualifying = counts["qualifying"]
    try:
        while (futures or not exhausted) and qualifying < args.target:
            while len(futures) < args.workers and not exhausted:
                row = next(pending, None)
                if row is None:
                    exhausted = True
                    break
                if not row["valid"]:
                    db.execute(
                        "INSERT INTO origins VALUES (?, ?, ?, ?, ?)",
                        (
                            row["position"],
                            row["origin"],
                            row["rank"],
                            time.time(),
                            json.dumps({"reason": "invalid_origin"}),
                        ),
                    )
                    db.commit()
                    continue
                time.sleep(max(0, next_launch - time.monotonic()))
                future = pool.submit(isolated_probe, row["domain"], args.timeout, row["origin"])
                futures[future] = row
                next_launch = time.monotonic() + args.interval
            if futures:
                completed, _ = concurrent.futures.wait(
                    futures, timeout=0.25, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in completed:
                    row = futures.pop(future)
                    result = future.result()
                    db.execute(
                        "INSERT INTO origins VALUES (?, ?, ?, ?, ?)",
                        (
                            row["position"],
                            row["origin"],
                            row["rank"],
                            time.time(),
                            json.dumps(result, ensure_ascii=False),
                        ),
                    )
                    db.commit()
            # Advance only through the completed input prefix. Stop new launches as
            # soon as the target is reached; at most one worker window is in flight.
            while frontier < len(rows) and qualifying < args.target:
                saved = db.execute(
                    "SELECT result FROM origins WHERE position=?", (frontier + 1,)
                ).fetchone()
                if saved is None:
                    break
                outcome, assessment = assess(json.loads(saved[0]))
                frontier += 1
                if outcome == "retained" and assessment["category"] in ACTIVE:
                    qualifying += 1
            if time.monotonic() - last_export >= 30 or (exhausted and not futures):
                counts, retained, reasons = export(rows, db, args.output, args.target)
                print(json.dumps(counts), flush=True)
                last_export = time.monotonic()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        # Preserve completed in-flight probes, including a bounded speculative tail.
        pool.shutdown(wait=True, cancel_futures=True)
        for future, row in futures.items():
            if not future.cancelled():
                db.execute(
                    "INSERT OR IGNORE INTO origins VALUES (?, ?, ?, ?, ?)",
                    (
                        row["position"],
                        row["origin"],
                        row["rank"],
                        time.time(),
                        json.dumps(future.result(), ensure_ascii=False),
                    ),
                )
        db.commit()
        counts, retained, reasons = export(rows, db, args.output, args.target)
        saved_rows = db.execute("SELECT COUNT(*) FROM origins").fetchone()[0]
        db.close()
    report = dict(
        counts,
        input_rows=len(rows),
        input_exhausted=counts["rows_walked"] == len(rows),
        completed=counts["qualifying"] >= args.target or counts["rows_walked"] == len(rows),
        interrupted=interrupted,
        categories=dict(Counter(r["category"] for r in retained)),
        skip_reasons=reasons,
        source_sha256=source_hash(args.csv),
        output_sha256=source_hash(args.output),
        signal_version=SIGNAL_VERSION,
        rule_version=RULE_VERSION,
        saved_rows=saved_rows,
        speculative_tail=saved_rows - counts["rows_walked"],
        settings=dict(
            workers=args.workers, interval=args.interval, timeout=args.timeout, target=args.target
        ),
        updated_at=datetime.now(timezone.utc).isoformat(),
    )
    args.output.with_suffix(".summary.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=ROOT / "analysis/current_top20k.csv")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "analysis/current_origins_verified.csv"
    )
    parser.add_argument(
        "--database", type=Path, default=ROOT / "assessment/current_origins.sqlite3"
    )
    parser.add_argument("--target", type=int, default=10000)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--interval", type=positive, default=0.1)
    parser.add_argument("--timeout", type=positive, default=8)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--max-rows",
        type=int,
        help="Stop after this input prefix; resume against the same complete source",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Reassess saved evidence without any network requests",
    )
    args = parser.parse_args()
    if not 1 <= args.workers <= 16 or not 1 <= args.target <= 10000:
        parser.error("workers must be 1..16 and target must be 1..10000")
    if args.max_rows is not None and args.max_rows < 1:
        parser.error("max-rows must be positive")
    if args.dry_run:
        run(args)
        return
    args.database.parent.mkdir(parents=True, exist_ok=True)
    with args.database.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another assessment is using this checkpoint")

        def stop(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stop)
        run(args)


if __name__ == "__main__":
    main()
