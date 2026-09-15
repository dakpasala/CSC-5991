"""Resume bounded DNS/HTTP assessment of Tranco domains; never invent activity labels."""

import argparse
import concurrent.futures
import csv
import hashlib
import heapq
import json
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from config import ROOT, positive


def top_domains(path, limit):
    def rows():
        with Path(path).open(newline="", encoding="utf-8-sig") as stream:
            for line, row in enumerate(csv.reader(stream), 1):
                if line == 1 and row and row[0].lower() == "rank":
                    continue
                if len(row) != 2:
                    raise ValueError(f"Invalid CSV row {line}")
                rank, domain = int(row[0]), row[1].strip().lower()
                if rank < 1:
                    raise ValueError(f"Invalid rank/domain at row {line}")
                yield rank, domain
    selected = heapq.nsmallest(limit, rows())
    if len({d for _, d in selected}) != len(selected):
        raise ValueError("Duplicate domain in selected ranks")
    if len({r for r, _ in selected}) != len(selected):
        raise ValueError("Duplicate rank in selected ranks")
    return selected


def source_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def valid_domain(domain):
    try:
        ascii_name = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    labels = ascii_name.split(".")
    return (
        len(ascii_name) <= 253
        and len(labels) >= 2
        and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
    )


def probe(domain, timeout):
    if not valid_domain(domain):
        return {"dns": "not_attempted", "reason": "invalid_domain", "browser_usability": "not_tested"}
    result = {"dns": "failed", "http": [], "browser_usability": "not_tested"}
    try:
        addresses = socket.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
        result["addresses"] = sorted({item[4][0] for item in addresses})
        result["dns"] = "resolved"
    except OSError:
        result["reason"] = "DNS resolution failed"
        return result
    for scheme in ("https", "http"):
        url = f"{scheme}://{domain}/"
        entry = {"scheme": scheme}
        request = urllib.request.Request(url, headers={"User-Agent": "CSC5991-research/0.1"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                parsed = urlsplit(response.url)
                entry.update(
                    status=response.status,
                    final_url=urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
                    content_type=response.headers.get("Content-Type", ""),
                    sampled_bytes=len(response.read(65536)),
                )
        except urllib.error.HTTPError as error:
            entry.update(status=error.code, reason="HTTP error response")
        except (OSError, urllib.error.URLError, ValueError) as error:
            entry.update(reason=type(error).__name__)
        result["http"].append(entry)
        if entry.get("status") and entry["status"] < 400:
            break
    result["reachable"] = any("status" in entry for entry in result["http"])
    result["http_success"] = any(200 <= e.get("status", 0) < 300 for e in result["http"])
    return result


def isolated_probe(domain, timeout):
    """A child-process timeout also bounds DNS, TLS, redirects, and slow body reads."""
    try:
        completed = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--probe", domain,
             "--timeout", str(timeout)],
            capture_output=True, text=True, timeout=timeout,
        )
        if completed.returncode:
            return {"dns": "unknown", "reason": "probe_error", "browser_usability": "not_tested"}
        return json.loads(completed.stdout)
    except subprocess.TimeoutExpired:
        return {"dns": "unknown", "reason": "overall_timeout", "browser_usability": "not_tested"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=ROOT / "tranco_GQNVK.csv")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--database", type=Path, default=ROOT / "assessment" / "domains.sqlite3")
    parser.add_argument("--timeout", type=positive, default=15)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--interval", type=positive, default=0.5,
        help="Minimum seconds between launches",
    )
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(probe(args.probe, args.timeout)))
        return 0
    if not 1 <= args.workers <= 16 or not 1 <= args.limit <= 100000:
        parser.error("workers must be 1..16; limit must be 1..100000")
    selected = top_domains(args.csv, args.limit)
    if args.dry_run:
        print(json.dumps({"selected": len(selected), "first": selected[:3], "last": selected[-3:]}))
        return 0
    args.database.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.database)
    db.execute("CREATE TABLE IF NOT EXISTS source (hash TEXT PRIMARY KEY)")
    digest = source_hash(args.csv)
    previous = db.execute("SELECT hash FROM source").fetchone()
    if previous and previous[0] != digest:
        parser.error("Database belongs to a different Tranco file; use a new database")
    db.execute("INSERT OR IGNORE INTO source VALUES (?)", (digest,))
    db.execute("""CREATE TABLE IF NOT EXISTS domains (
        domain TEXT PRIMARY KEY, rank INTEGER, checked_at REAL,
        http_success INTEGER, result TEXT
    )""")
    db.commit()
    reviewed = {}
    research = ROOT / "analysis" / "top100_assessment.csv"
    if research.exists():
        with research.open() as stream:
            reviewed = {row["domain"]: row for row in csv.DictReader(stream)}
    done = dict(db.execute("SELECT domain, http_success FROM domains"))
    pending = [
        (rank, domain) for rank, domain in selected
        if domain not in done or (args.retry_failed and not done[domain])
    ]
    count = 0
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
    futures = {}
    iterator = iter(pending)
    exhausted = False
    next_launch = 0.0
    try:
        while futures or not exhausted:
            while len(futures) < args.workers and not exhausted:
                try:
                    rank, domain = next(iterator)
                except StopIteration:
                    exhausted = True
                    break
                time.sleep(max(0, next_launch - time.monotonic()))
                future = pool.submit(isolated_probe, domain, args.timeout)
                futures[future] = (rank, domain)
                next_launch = time.monotonic() + args.interval
            if not futures:
                break
            completed, _ = concurrent.futures.wait(
                futures, timeout=0.25, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in completed:
                rank, domain = futures.pop(future)
                result = future.result()
                note = reviewed.get(domain, {})
                result.update(
                    proposed_categories=note.get("proposed_categories", "unknown"),
                    research_source=note.get("source_url"),
                    category_status="proposal_only" if note else "unclassified",
                    actual_activity=None,
                )
                db.execute("INSERT OR REPLACE INTO domains VALUES (?, ?, ?, ?, ?)", (
                    domain, rank, time.time(), int(result.get("http_success", False)),
                    json.dumps(result),
                ))
                db.commit()
                count += 1
                if count % 10 == 0 or count == len(pending):
                    print(f"Assessed {count}/{len(pending)} pending domains", flush=True)
    except KeyboardInterrupt:
        print("Stopped; completed results are saved. Rerun to resume.")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
