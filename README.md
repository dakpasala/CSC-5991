# Labeled traffic collector (macOS proof of concept)

Collect separate PCAPs for observed browsing, video playback, and controlled video calls. No ML training or packet-content inspection. Read `analysis/top100_assessment.csv` first: all 100 Tranco ranks are assessed, including excluded infrastructure, uncertain domains, access barriers, actions, and sources.

## Setup

Use Python 3.10+ and Google Chrome. In this project directory:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
/usr/sbin/tcpdump -D
```

[Selenium Manager](https://www.selenium.dev/documentation/selenium_manager/) obtains a matching driver on first launch (Internet access required). Warm it up **before** a timed experiment so download time does not consume the experiment budget:

```sh
python -c 'from selenium import webdriver; o=webdriver.ChromeOptions(); o.add_argument("--headless=new"); d=webdriver.Chrome(options=o); print(d.capabilities["browserVersion"]); d.quit()'
```

If Chrome has a nonstandard name, add `--chrome-binary '/Applications/Google Chrome 2.app/Contents/MacOS/Google Chrome'`. If automatic driver management is unavailable, provide `--chromedriver /absolute/path/to/chromedriver`. The browser and driver must match. The same binary path must be set on `o.binary_location` for the warmup command.

### Packet-capture permission

macOS includes `/usr/sbin/tcpdump`, but your normal user needs BPF-device access. A normal-user probe is:

```sh
/usr/sbin/tcpdump -i lo0 -n -p -c 1 'tcp port 18765'
# Ctrl+C if no fixture traffic is running.
```

If it reports permission denied, use the **ChmodBPF** component provided with [Wireshark for macOS](https://www.wireshark.org/download.html), then log out/in if group membership changed. For a temporary, administrator-approved setup on a personal test Mac, grant your current user access to existing capture devices:

```sh
sudo chown "$USER" /dev/bpf*
```

This changes ownership of existing BPF devices until reset/reboot; it is broader than one interface. Prefer the managed ChmodBPF setup on shared machines. Do not run the Python collector or Chrome as root. This collector never invokes sudo, changes device permissions, or silently bypasses capture errors. In the validation environment, the user already had capture access; the Codex execution sandbox required a separate approval.

## Run a short experiment

```sh
python collector.py --dry-run
python collector.py --total-seconds 720 --session-seconds 90 --interface en0
# 12 minutes TOTAL, at most 90 seconds per session (including startup/navigation).
# Use 600 for 10 minutes or 900 for 15 minutes TOTAL.
```

The four available sessions repeat in plan order until the total deadline. The missing meeting is logged once as unavailable. `--once` visits each plan entry only once. A small first run:

```sh
python collector.py --total-seconds 45 --session-seconds 30 --once --interface en0
python -m unittest discover -s tests -v
```

The total deadline includes Chrome startup, navigation, actions, and capture. Capture stops at the deadline; bounded cleanup may add several seconds. The final session may be shorter; a session without enough evidence fails. Unvisited entries after budget exhaustion are not counted as attempted captures. Exit code 0 means at least one successful session, 1 means none, 130 means interrupted; inspect per-session metadata for partial failures.

Choose the correct interface using `tcpdump -D`; `en0` is usually Wi-Fi, not a universal guarantee. VPN traffic may require an appropriate `utun` interface or a dedicated test environment. Do not change networking during a run. Optionally restrict to this machine's IPv4 **and IPv6** addresses, for example with `--bpf-filter 'host 192.0.2.10 or host 2001:db8::10'` after substituting real addresses. A host filter still includes other apps on that host. The default `ip or ip6` preserves DNS, UDP/QUIC, TCP, STUN/TURN, and WebRTC. Filtering only by the site's resolved IPs would lose CDN/media traffic.

## Configuration and results

See [USAGE.md](USAGE.md) for browser actions, playback/call verification, controlled meeting setup, output metadata, and capture limitations. See [VALIDATION.md](VALIDATION.md) for the actual smoke-test results.

Successful captures go under the matching `dataset/` category. Failed/unverified captures stay in `dataset/failed/`, which is not a class. Session evidence is in `dataset/metadata/` and `dataset/sessions.jsonl`. A homepage is browsing; streaming requires observed playback, and conferencing requires a controlled call with bidirectional video evidence. No meeting is configured by default.

Close other network-heavy apps during collection: interface capture also records unrelated host traffic. This initial dataset is not isolated research ground truth. No ML is implemented.
