"""Load and filter the browser-level request log (requests_history.json)
down to the requests that actually belong to the streaming service under
test, discarding unrelated background traffic (ads, analytics, unrelated
page scripts) that the same browser session also generated.

Each entry has OnBeforeRequestOptions (always present) and onCompleted
(present once the request finished) sub-records, captured via what looks
like Chrome's webRequest API. Timestamps are epoch milliseconds, converted
to seconds here to match the pcap's time scale. content-length is read from
the real response headers, not inferred.
"""

import json
from dataclasses import dataclass
from urllib.parse import urlparse

SERVICE_DOMAIN_KEYWORDS = {
    "twitch": ("twitch", "ttvnw"),
    "youtube": ("youtube", "googlevideo", "ytimg"),
    "netflix": ("netflix", "nflxvideo", "nflximg"),
    "amazon": ("amazon", "primevideo", "media-amazon"),
}


@dataclass(frozen=True)
class RequestRecord:
    start_s: float
    end_s: float | None
    content_length: int | None

    @property
    def duration_ms(self):
        return (self.end_s - self.start_s) * 1000 if self.end_s is not None else None


def _content_length(response_headers):
    if not response_headers:
        return None
    for header in response_headers:
        if header.get("name", "").lower() == "content-length":
            try:
                return int(header["value"])
            except (TypeError, ValueError):
                return None
    return None


def _service_key(label):
    return label.split("_")[0]  # "youtube_tcp" / "youtube_quic" -> "youtube"


def load_relevant_requests(path, label):
    keywords = SERVICE_DOMAIN_KEYWORDS.get(_service_key(label), ())
    with open(path) as handle:
        data = json.load(handle)

    records = []
    for entry in data.values():
        before = entry.get("OnBeforeRequestOptions")
        if not before:
            continue
        domain = urlparse(before.get("url", "")).netloc
        if not any(keyword in domain for keyword in keywords):
            continue

        completed = entry.get("onCompleted")
        records.append(
            RequestRecord(
                start_s=before["timeStamp"] / 1000,
                end_s=(completed["timeStamp"] / 1000) if completed else None,
                content_length=_content_length(completed.get("responseHeaders")) if completed else None,
            )
        )
    return records
