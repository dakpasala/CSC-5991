"""Browser evidence and PCAP structure validation; no packet payload inspection."""

import struct

MEDIA_STATE = r"""
const media = document.querySelector(arguments[0]);
if (!media) return null;
return {
  time: media.currentTime, duration: Number.isFinite(media.duration) ? media.duration : null,
  loop: media.loop, paused: media.paused, ended: media.ended,
  ready: media.readyState, source: media.currentSrc,
  width: media.videoWidth || 0,
  frames: media.getVideoPlaybackQuality ? media.getVideoPlaybackQuality().totalVideoFrames : 0
};
"""

BROWSING_STATE = r"""
const visible = e => {
 const r = e.getBoundingClientRect(), s = getComputedStyle(e);
 if (!(r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' &&
       r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth)) return false;
 const x = (Math.max(0, r.left) + Math.min(innerWidth, r.right)) / 2;
 const y = (Math.max(0, r.top) + Math.min(innerHeight, r.bottom)) / 2;
 const top = document.elementFromPoint(x, y);
 return top === e || e.contains(top);
};
const nodes = [...document.querySelectorAll(arguments[0])].filter(visible);
return {
 // visible() on the node already rules out a page-level overlay; also requiring
 // its <img> to win the topmost-pixel test breaks on normal hover/click layers.
 content: nodes.some(e => (e.innerText || '').trim().length >= 20 ||
   [...e.querySelectorAll('img')].some(i => i.complete && i.naturalWidth > 0)),
 matching_elements: nodes.length,
 playing_media: [...document.querySelectorAll('audio,video')].some(m => !m.paused && !m.ended)
};
"""

NO_MEDIA = r"""
HTMLMediaElement.prototype.play = function() {
  return Promise.reject(new Error('Browsing-only session'));
};
document.addEventListener('play', event => {
  if (event.target.pause) event.target.pause();
}, true);
"""

RTC_HOOK = r"""
(() => {
 const Original = window.RTCPeerConnection;
 window.__collectorPeers = [];
 if (!Original) return;
 class TrackedPeer extends Original {
   constructor(...args) { super(...args); window.__collectorPeers.push(this); }
 }
 window.RTCPeerConnection = TrackedPeer;
 window.webkitRTCPeerConnection = TrackedPeer;
})();
"""

RTC_STATS = r"""
const done = arguments[arguments.length - 1];
Promise.all((window.__collectorPeers || []).map(async pc => {
 const stats = await pc.getStats();
 let incoming = 0, outgoing = 0, frames = 0;
 stats.forEach(s => {
   if (s.type === 'inbound-rtp' && (s.kind === 'video' || s.mediaType === 'video')) {
     incoming += s.bytesReceived || 0;
     frames += s.framesDecoded || 0;
   }
   if (s.type === 'outbound-rtp' && (s.kind === 'video' || s.mediaType === 'video'))
     outgoing += s.bytesSent || 0;
 });
 return {connected: pc.connectionState === 'connected', incoming, outgoing, frames};
})).then(done).catch(() => done([]));
"""


def media_progress(before, after, elapsed, video=True):
    if not before or not after or not 0 < elapsed <= 3:
        return False
    if (
        not after["source"]
        or before["source"] != after["source"]
        or after["paused"]
        or after["ended"]
        or after["ready"] < 2
    ):
        return False
    advance = after["time"] - before["time"]
    if advance < 0 and after.get("loop") and after.get("duration"):
        advance += after["duration"]
    if not 0.05 < advance <= elapsed * 2 + 1:
        return False
    return not video or (
        after["width"] > 0 and after["frames"] > before["frames"]
    )


def rtc_progress(before, after):
    return any(
        a["connected"]
        and b["connected"]
        and b["incoming"] > a["incoming"]
        and b["outgoing"] > a["outgoing"]
        and b["frames"] > a["frames"]
        for a, b in zip(before, after)
    )


def pcap_packets(path):
    magics = {
        b"\xd4\xc3\xb2\xa1": "<",
        b"\xa1\xb2\xc3\xd4": ">",
        b"\x4d\x3c\xb2\xa1": "<",
        b"\xa1\xb2\x3c\x4d": ">",
    }
    with open(path, "rb") as stream:
        header = stream.read(24)
        if len(header) != 24 or header[:4] not in magics:
            raise ValueError("Missing or unsupported PCAP header")
        endian = magics[header[:4]]
        snaplen = struct.unpack(endian + "I", header[16:20])[0]
        count = 0
        while True:
            record = stream.read(16)
            if not record:
                return count
            if len(record) != 16:
                raise ValueError("Truncated PCAP record")
            _, _, included, original = struct.unpack(endian + "IIII", record)
            if included > min(snaplen, original, 16 * 1024 * 1024):
                raise ValueError("Invalid PCAP lengths")
            if len(stream.read(included)) != included:
                raise ValueError("Truncated PCAP packet")
            count += 1
