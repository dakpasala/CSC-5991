"""Shared sequential capture runner used by all six activity commands."""

import argparse
import datetime as dt
import hashlib
import json
import multiprocessing as mp
import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path

from browser_profiles import browser_profile, collection_lock
from browser_session import worker
from config import CATEGORIES, ROOT, available, load_plan, positive
from evidence import pcap_packets

STOP = False


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def stop_capture(process):
    if process is None:
        return None
    for sig, timeout in ((signal.SIGINT, 3), (signal.SIGTERM, 1), (signal.SIGKILL, 1)):
        if process.poll() is not None:
            break
        process.send_signal(sig)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            continue
    return process.returncode


def stop_browser(process):
    if process is None:
        return
    process.join(timeout=1)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            # Either the group is already gone, or the PID was recycled by an
            # unrelated process we have no business signaling -- both mean
            # there's nothing left here for us to clean up.
            break
        process.join(timeout=0.5)
    if process.is_alive():
        process.kill()
        process.join(timeout=1)


def save_record(record, output, pending=None, capture_ok=False):
    success = bool(record.get("browser_success") and capture_ok)
    record["success"] = success
    record["actual_activity"] = record["requested_activity"] if success else None
    record["parent_activity"] = (
        "web_browsing"
        if record["actual_activity"] == "social_media_browsing"
        else record["actual_activity"]
    )
    record["subtype"] = (
        "social_media" if record["actual_activity"] == "social_media_browsing" else None
    )
    record.setdefault("status", "success" if success else "failed")
    record["ended_at"] = utc()
    record["pcap_path"] = None
    if pending and pending.exists():
        folder = record["actual_activity"] if success else "failed"
        target = output / folder / pending.name
        pending.replace(target)
        record["pcap_path"] = str(target)
    target = output / "metadata" / (record["session_id"] + ".json")
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, indent=2) + "\n")
    temporary.replace(target)
    with (output / "sessions.jsonl").open("a") as stream:
        stream.write(json.dumps(record) + "\n")
    return record


def run_session(item, args, run_deadline, output):
    started = time.monotonic()
    deadline = min(run_deadline, started + args.session_seconds)
    ident = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    ident += "_" + uuid.uuid4().hex[:8]
    name = f"{ident}_{item['domain']}_{item['activity']}.pcap"
    pending = output / "failed" / name
    record = {
        "session_id": ident,
        "domain": item["domain"],
        "rank": item.get("rank"),
        "requested_activity": item["activity"],
        "started_at": utc(),
        "actions": [],
        "interface": args.interface,
        "bpf_filter": args.bpf_filter,
        "snaplen": args.snaplen,
        "session_budget_seconds": deadline - started,
        "source_scope": item.get("source_scope", "user_plan"),
        "label_scope": "session; interface packets are not process-attributed",
        "plan_sha256": getattr(args, "plan_sha256", None),
        "profile_mode": "persistent" if getattr(args, "use_login_profile", False) else "fresh",
    }
    if not available(item):
        record.update(
            status="unavailable",
            reason=item.get(
                "unavailable_reason", ("Session disabled or controlled meeting setup missing")
            ),
            duration_seconds=0,
        )
        return save_record(record, output)
    browser = capture = parent = child = log = None
    capture_ok = False
    with browser_profile(getattr(args, "use_login_profile", False)) as profile:
        try:
            parent, child = mp.get_context("spawn").Pipe()
            browser = mp.get_context("spawn").Process(
                target=worker, args=(child, item, vars(args), deadline, profile)
            )
            browser.start()
            child.close()
            ready = False
            while time.monotonic() < deadline and not STOP:
                if parent.poll(0.1):
                    event = parent.recv()
                    record["actions"].append(event)
                    if event["event"] == "ready":
                        record["browser_version"] = event["browser_version"]
                        ready = True
                        break
                    if event["event"] == "done":
                        record["reason"] = event.get("reason", "Browser startup failed")
                        break
                if not browser.is_alive():
                    break
            if not ready:
                raise RuntimeError(record.get("reason", "Browser startup failed or timed out"))
            log = (output / "metadata" / (ident + ".tcpdump.log")).open("wb")
            capture = subprocess.Popen(
                [
                    args.tcpdump,
                    "--immediate-mode",
                    "-i",
                    args.interface,
                    "-n",
                    "-p",
                    "-U",
                    "-s",
                    str(args.snaplen),
                    "-w",
                    str(pending),
                    args.bpf_filter,
                ],
                stdout=subprocess.DEVNULL,
                stderr=log,
            )
            while time.monotonic() < deadline and not STOP:
                if capture.poll() is not None:
                    raise RuntimeError("tcpdump could not start; check interface/BPF permissions")
                if pending.exists() and pending.stat().st_size >= 24:
                    break
                time.sleep(0.05)
            else:
                raise RuntimeError("Capture initialization interrupted or timed out")
            record["capture_started_at"] = utc()
            parent.send("go")
            print(f"Recording {item['domain']} -> {pending.name}", flush=True)
            while time.monotonic() < deadline and not STOP:
                if capture.poll() is not None:
                    raise RuntimeError("tcpdump exited during session")
                if parent.poll(0.1):
                    event = parent.recv()
                    record["actions"].append(event)
                    if event["event"] == "done":
                        record["browser_success"] = event.get("success", False)
                        for field in (
                            "reason",
                            "verified_seconds",
                            "downloaded_bytes",
                            "error_type",
                        ):
                            if field in event:
                                record[field] = event[field]
                        break
                if not browser.is_alive():
                    raise RuntimeError("Browser exited without a result")
            else:
                raise RuntimeError("Interrupted" if STOP else "Session deadline exceeded")
            code = stop_capture(capture)
            record["capture_ended_at"] = utc()
            record["tcpdump_exit_code"] = code
            record["packet_count"] = pcap_packets(pending)
            capture_ok = code == 0 and record["packet_count"] > 0
            if not capture_ok:
                record["reason"] = "Capture empty or unsuccessful"
        except (Exception, KeyboardInterrupt) as error:
            record.update(browser_success=False, reason=str(error), error_type=type(error).__name__)
        finally:
            code = stop_capture(capture)
            if capture is not None:
                record.setdefault("tcpdump_exit_code", code)
            if pending.exists() and "packet_count" not in record:
                try:
                    record["packet_count"] = pcap_packets(pending)
                except (OSError, ValueError) as error:
                    record["capture_validation_error"] = str(error)
            if record.get("capture_started_at"):
                record.setdefault("capture_ended_at", utc())
                end = dt.datetime.fromisoformat(record["capture_ended_at"])
                start = dt.datetime.fromisoformat(record["capture_started_at"])
                record["capture_duration_seconds"] = (end - start).total_seconds()
            if parent:
                try:
                    parent.send("stop")
                except (BrokenPipeError, EOFError, OSError):
                    pass
            stop_browser(browser)
            if parent:
                parent.close()
            if child:
                child.close()
            if log:
                log.close()
    record["duration_seconds"] = round(time.monotonic() - started, 3)
    return save_record(record, output, pending, capture_ok)


def main(category):
    global STOP
    STOP = False
    parser = argparse.ArgumentParser(description=f"Collect verified {category} sessions")
    parser.add_argument("--plan", type=Path, default=ROOT / "plans" / f"{category}.json")
    parser.add_argument("--output", type=Path, default=ROOT / "dataset")
    parser.add_argument("--total-seconds", type=positive, default=600)
    parser.add_argument("--session-seconds", type=positive, default=30)
    parser.add_argument("--timeout", type=positive, default=20)
    parser.add_argument("--min-verified-seconds", type=positive, default=5)
    parser.add_argument("--stall-seconds", type=positive, default=15)
    parser.add_argument("--cooldown-seconds", type=positive, default=2)
    parser.add_argument("--max-download-bytes", type=int, default=10 * 1024 * 1024)
    parser.add_argument("--min-download-bytes", type=int, default=1024)
    parser.add_argument("--interface", default="en0")
    parser.add_argument("--bpf-filter", default="ip or ip6")
    parser.add_argument("--snaplen", type=int, default=128)
    parser.add_argument("--tcpdump", default=shutil.which("tcpdump") or "/usr/sbin/tcpdump")
    alternate = "/Applications/Google Chrome 2.app/Contents/MacOS/Google Chrome"
    parser.add_argument("--chrome-binary", default=alternate if Path(alternate).is_file() else None)
    parser.add_argument("--chromedriver")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--use-login-profile",
        action="store_true",
        help=(
            "Reuse the dedicated local collection profile and its saved session "
            "cookies; run login_setup.py first. Runs headless by default like any "
            "other session -- pass --headed too if you want to watch it."
        ),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.session_seconds < args.min_verified_seconds + 3:
        parser.error("Session budget must exceed minimum verification time by 3 seconds")
    if not 0 <= args.snaplen <= 262144:
        parser.error("snaplen must be 0..262144")
    if not 0 < args.min_download_bytes <= args.max_download_bytes <= 100 * 1024 * 1024:
        parser.error("Download byte limits must be ordered and at most 100 MiB")
    try:
        plan = load_plan(args.plan, category)
        args.plan_sha256 = hashlib.sha256(args.plan.read_bytes()).hexdigest()
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if args.dry_run:
        print(
            json.dumps(
                {
                    "category": category,
                    "total_seconds": args.total_seconds,
                    "session_seconds": args.session_seconds,
                    "sessions": [{"domain": i["domain"], "available": available(i)} for i in plan],
                },
                indent=2,
            )
        )
        return 0
    if os.geteuid() == 0:
        parser.error("Run as your normal user with BPF access, not root")
    try:
        with collection_lock():
            return run_plan(plan, args)
    except RuntimeError as error:
        parser.error(str(error))


def run_plan(plan, args):
    def interrupt(signum, frame):
        global STOP
        STOP = True

    old_int = signal.signal(signal.SIGINT, interrupt)
    old_term = signal.signal(signal.SIGTERM, interrupt)
    output = args.output.resolve()
    for folder in (*CATEGORIES, "failed", "metadata"):
        (output / folder).mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    deadline = started + args.total_seconds
    counts = {"success": 0, "failed": 0, "unavailable": 0}
    try:
        active = []
        for item in plan:
            if available(item):
                active.append(item)
            else:
                result = run_session(item, args, deadline, output)
                counts["unavailable"] += 1
                print(f"{item['domain']}: unavailable — {result['reason']}", flush=True)
        while active and not STOP:
            for item in active:
                if STOP or deadline - time.monotonic() < args.min_verified_seconds + 3:
                    active = []
                    break
                result = run_session(item, args, deadline, output)
                counts[result["status"]] += 1
                print(f"{item['domain']}: {result['status']}", flush=True)
                if "tcpdump could not start" in result.get("reason", ""):
                    active = []
                    break
                resume_at = min(deadline, time.monotonic() + args.cooldown_seconds)
                while not STOP and time.monotonic() < resume_at:
                    time.sleep(min(0.1, max(0, resume_at - time.monotonic())))
            if args.once:
                break
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    summary = {
        "ended_at": utc(),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "total_budget_seconds": args.total_seconds,
        "counts": counts,
        "interrupted": STOP,
        "note": "Bounded cleanup may extend the deadline by several seconds",
    }
    (output / f"run_{uuid.uuid4().hex[:8]}.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))
    return 130 if STOP else 0 if counts["success"] else 1
