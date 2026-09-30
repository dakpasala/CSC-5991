"""Resume bounded DNS/HTTP assessment of Tranco domains; never invent activity labels."""

import argparse
import concurrent.futures
import csv
import hashlib
import heapq
import http.client
from html.parser import HTMLParser
import ssl
import json
import re
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from config import ROOT, CATEGORIES, positive


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


SIGNAL_VERSION = 2
BODY_LIMIT = 65536


class PageSignals(HTMLParser):
    """Inspect the bounded response in memory; never persist raw HTML or cookies."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.in_title = False
        self.title_seen = False
        self.ignored = 0
        self.description = ""
        self.og_type = ""
        self.video = False
        self.audio = False
        self.og_description = ""
        self.body_parts = []
        self.body_chars = 0
        self.in_body = False
        self.login_control = False
        self.password_input = False
        self.download_link = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "template", "svg", "noscript"}:
            self.ignored += 1
        if self.ignored:
            return
        attrs = dict(attrs)
        if tag == "body":
            self.in_body = True
        if tag == "input" and attrs.get("type", "").lower() == "password":
            self.password_input = True
        if tag == "a":
            href = attrs.get("href") or ""
            if re.search(r"(?:^|[/?._-])(?:login|signin|sign-in|log-in)(?:$|[/?.#_-])", href, re.I):
                self.login_control = True
            if "download" in attrs or re.search(
                r"\.(?:zip|tar|gz|exe|msi|dmg|iso|apk)(?:$|[?#])", href, re.I
            ):
                self.download_link = True
        if tag == "title" and not self.title_seen:
            self.in_title = True
            self.title_seen = True
        elif tag == "meta":
            key = (attrs.get("name") or attrs.get("property") or "").lower()
            value = attrs.get("content") or ""
            if key == "description" and not self.description:
                self.description = value
            elif key == "og:type" and not self.og_type:
                self.og_type = value
            elif key == "og:description" and not self.og_description:
                self.og_description = value
        elif tag == "video":
            self.video = True
        elif tag == "audio":
            self.audio = True

    def handle_endtag(self, tag):
        if tag in {"script", "style", "template", "svg", "noscript"}:
            self.ignored = max(0, self.ignored - 1)
        if tag == "title":
            self.in_title = False
        if tag == "body":
            self.in_body = False

    def handle_data(self, data):
        if self.in_title and not self.ignored:
            self.title_parts.append(data)
        if self.in_body and not self.ignored and self.body_chars < 12000:
            text = " ".join(data.split())[: 12000 - self.body_chars]
            if text:
                self.body_parts.append(text)
                self.body_chars += len(text)

    def signals(self):
        clean = lambda value: " ".join(value.split())[:1000]
        return dict(
            title=clean("".join(self.title_parts)),
            meta_description=clean(self.description),
            og_type=clean(self.og_type).lower(),
            video_tag=self.video,
            audio_tag=self.audio,
            og_description=clean(self.og_description),
            public_text=" ".join(self.body_parts)[:12000],
            login_control=self.login_control,
            password_input=self.password_input,
            download_link=self.download_link,
        )


def propose_category(signals):
    """Planning proposals only: no HTTP response verifies a capture activity."""
    title = signals.get("title", "")
    description = signals.get("meta_description", "")
    text = (title + " " + description).lower()
    if re.search(
        r"captcha|access denied|just a moment|verify you are human|checking your browser|domain.{0,15}for sale|parked domain|sign in|log in|login|403 forbidden|404 not found",
        text,
    ):
        return "unknown", "challenge, error, parked, or login page"
    if not title or not description:
        return "unknown", "missing title or meta description"
    calling_product = re.search(
        r"\b(?:(?:video conferencing|video meetings|team messaging|instant messaging) (?:software|platform|app|solutions?|service|tool)|messaging app|messaging platform|secure messenger)\b",
        text,
    )
    video_chat = "video chat" in title.lower() and "video chat" in description.lower()
    messaging_calls = "отправлять любые виды сообщений и звонить" in description.lower()
    if (calling_product or video_chat or messaging_calls) and not re.search(
        r"\b(?:news|reviews?|articles?|tutorials?)\b", text
    ):
        return (
            "excluded_conferencing",
            "title/meta description explicitly describes calling or messaging product",
        )
    video = bool(
        re.search(r"watch (?:live |free |online )?(?:videos|movies|tv|shows)|video streaming", text)
    )
    audio = bool(
        re.search(
            r"listen to (?:music|podcasts|radio)|stream (?:music|audio)|music streaming", text
        )
    )
    if video and audio:
        return "unknown", "mixed media purpose"
    if video and signals.get("video_tag") and not signals.get("audio_tag"):
        return (
            "video_streaming",
            "explicit video viewing description and raw HTML video element; playback unverified",
        )
    if audio and signals.get("audio_tag") and not signals.get("video_tag"):
        return (
            "audio_streaming",
            "explicit audio listening description and raw HTML audio element; playback unverified",
        )
    reading = r"\b(?:news|articles?|blog|read|documentation|tutorials?)\b"
    article = signals.get("og_type") == "article" and re.search(reading, text)
    reading_site = (
        re.search(r"\b(?:news|documentation|encyclopedia|tutorials)\b", title.lower())
        and re.search(
            r"\b(?:news|reporting|documentation|encyclopedia|tutorials)\b", description.lower()
        )
        and not re.search(
            r"\b(?:platform|software|marketing|create|build|hosting)\b", description.lower()
        )
    )
    media_intent = re.search(
        r"\b(?:listen|stream|streaming|radio|podcasts?|trailers?|watch|memes?|gifs?|feeds?)\b", text
    )
    if (
        (article or reading_site)
        and not media_intent
        and not any(signals.get(k) for k in ("video_tag", "audio_tag"))
        and not video
        and not audio
    ):
        return "web_browsing", "corroborated reading-related metadata; browser action unverified"
    return "unknown", "insufficient or ambiguous activity-specific evidence"


def probe(domain, timeout, origin=None):
    result = {
        "dns": "not_attempted",
        "http": [],
        "browser_usability": "not_tested",
        "signal_version": SIGNAL_VERSION,
        "proposed_categories": "unknown",
        "category_status": "unclassified",
        "actual_activity": None,
    }
    if not valid_domain(domain):
        result["reason"] = "invalid_domain"
        return result
    url = origin or f"https://{domain}/"
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname != domain
        or parsed.username
        or parsed.password
    ):
        result["reason"] = "invalid_origin"
        return result
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(domain, port, type=socket.SOCK_STREAM)
        result["addresses"] = sorted({item[4][0] for item in addresses})
        result["dns"] = "resolved"
    except OSError:
        result.update(dns="failed", reason="DNS resolution failed")
        return result
    # Connect directly to the first resolved address: no hidden second DNS lookup,
    # redirects, retries, proxy lookups, subresources, or HTTP fallback.
    family, socktype, proto, _, address = addresses[0]
    connection_class = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_class(domain, port=port, timeout=timeout)
    raw = None
    entry = {"scheme": parsed.scheme}
    try:
        raw = socket.socket(family, socktype, proto)
        raw.settimeout(timeout)
        raw.connect(address)
        connection.sock = (
            ssl.create_default_context().wrap_socket(raw, server_hostname=domain)
            if parsed.scheme == "https"
            else raw
        )
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        connection.request(
            "GET",
            target,
            headers={"User-Agent": "CSC5991-research/0.2", "Accept-Encoding": "identity"},
        )
        response = connection.getresponse()
        body = response.read(BODY_LIMIT)
        content_type = response.getheader("Content-Type", "")
        entry.update(
            status=response.status,
            final_url=url,
            content_type=content_type,
            sampled_bytes=len(body),
            body_limit_reached=len(body) == BODY_LIMIT,
        )
        if (
            200 <= response.status < 300
            and content_type.split(";")[0].strip().lower() in {"text/html", "application/xhtml+xml"}
            and response.getheader("Content-Encoding", "identity").lower() == "identity"
        ):
            match = re.search(r"charset=[\"']?([\w-]+)", content_type, re.I)
            encoding = match.group(1) if match else "utf-8"
            try:
                html = body.decode(encoding, errors="replace")
            except LookupError:
                html = body.decode("utf-8", errors="replace")
            parser = PageSignals()
            parser.feed(html)
            entry["signals"] = parser.signals()
            category, reason = propose_category(entry["signals"])
            result.update(
                proposed_categories=category,
                category_reason=reason,
                category_status="proposal_only" if category != "unknown" else "unclassified",
            )
        else:
            result["category_reason"] = "non-success, redirect, non-HTML, or encoded response"
    except (OSError, http.client.HTTPException, ValueError) as error:
        entry["reason"] = type(error).__name__
    finally:
        connection.close()
        if raw is not None:
            raw.close()
    result["http"].append(entry)
    result["reachable"] = "status" in entry
    result["http_success"] = 200 <= entry.get("status", 0) < 300
    return result


def apply_categories(path, db):
    """Change only unknown categories backed by this version's fetched evidence."""
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        rows = list(reader)
    changed = {}
    allowed = set(CATEGORIES) - {"video_conferencing"} | {"excluded_conferencing"}
    for row in rows:
        if row["category"] != "unknown":
            continue
        record = db.execute(
            "SELECT rank, result FROM domains WHERE domain=?", (row["domain"],)
        ).fetchone()
        if not record or record[0] != int(row["rank"]):
            continue
        result = json.loads(record[1])
        if result.get("signal_version") != SIGNAL_VERSION:
            continue
        entries = result.get("http", [])
        if len(entries) != 1 or not 200 <= entries[0].get("status", 0) < 300:
            continue
        category, _ = propose_category(entries[0].get("signals", {}))
        if category in allowed:
            row["category"] = category
            changed[category] = changed.get(category, 0) + 1
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)
    print(
        json.dumps(
            {"changed": changed, "stayed_unknown": sum(r["category"] == "unknown" for r in rows)}
        )
    )


def isolated_probe(domain, timeout, origin=None):
    """A child-process timeout also bounds DNS, TLS, redirects, and slow body reads."""
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--probe",
                domain,
                "--timeout",
                str(timeout),
            ]
            + (["--origin", origin] if origin else []),
            capture_output=True,
            text=True,
            timeout=timeout,
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
        "--interval",
        type=positive,
        default=0.5,
        help="Minimum seconds between launches",
    )
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--category-csv", type=Path, help="Probe only unknown rows in this inventory"
    )
    parser.add_argument(
        "--apply-categories",
        action="store_true",
        help="Apply saved proposals offline; requires --category-csv",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--probe", help=argparse.SUPPRESS)
    parser.add_argument("--origin", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(probe(args.probe, args.timeout, args.origin)))
        return 0
    if not 1 <= args.workers <= 16 or not 1 <= args.limit <= 100000:
        parser.error("workers must be 1..16; limit must be 1..100000")
    if args.apply_categories and not args.category_csv:
        parser.error("--apply-categories requires --category-csv")
    selected = top_domains(args.csv, args.limit)
    if args.category_csv:
        with args.category_csv.open(newline="", encoding="utf-8") as stream:
            inventory = list(csv.DictReader(stream))
        unknown = {(int(r["rank"]), r["domain"]) for r in inventory if r["category"] == "unknown"}
        selected = [item for item in selected if item in unknown]
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
    if args.apply_categories:
        apply_categories(args.category_csv, db)
        db.close()
        return 0
    reviewed = {}
    research = ROOT / "analysis" / "top100_assessment.csv"
    if research.exists():
        with research.open() as stream:
            reviewed = {row["domain"]: row for row in csv.DictReader(stream)}
    done = dict(db.execute("SELECT domain, http_success FROM domains"))
    pending = [
        (rank, domain)
        for rank, domain in selected
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
                    research_proposed_categories=note.get("proposed_categories", "unknown"),
                    research_source=note.get("source_url"),
                    actual_activity=None,
                )
                db.execute(
                    "INSERT OR REPLACE INTO domains VALUES (?, ?, ?, ?, ?)",
                    (
                        domain,
                        rank,
                        time.time(),
                        int(result.get("http_success", False)),
                        json.dumps(result),
                    ),
                )
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
