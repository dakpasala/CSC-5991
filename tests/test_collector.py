import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import collector as c


class LabelTests(unittest.TestCase):
    def test_paused_seek_or_missing_video_never_passes(self):
        a=dict(time=1,frames=10,paused=False,ended=False,ready=4,width=640,source='movie',rate=1)
        b={**a,'time':2,'frames':40}
        self.assertTrue(c.video_progress(a,b,1))
        for bad in ({**b,'paused':True},{**b,'time':90},{**b,'frames':10},{**b,'source':'other'},None):
            self.assertFalse(c.video_progress(a,bad,1))

    def test_call_requires_bidirectional_video_not_lobby_or_self_preview(self):
        a=[dict(connected=True,incoming=10,outgoing=10,frames=2)]
        b=[dict(connected=True,incoming=30,outgoing=30,frames=5)]
        self.assertTrue(c.rtc_progress(a,b))
        for bad in ([],[{**b[0],'incoming':10}],[{**b[0],'outgoing':10}],[{**b[0],'connected':False}]):
            self.assertFalse(c.rtc_progress(a,bad))

    def test_meeting_requires_explicit_setup(self):
        item=dict(meeting_url_env='TEST_MEETING',controlled_test=True,participants_ready=True,joined_selector='#joined')
        with patch.dict(os.environ,{},clear=True):
            self.assertFalse(c.conference_available(item))
        with patch.dict(os.environ,{'TEST_MEETING':'https://example.com/room'}):
            self.assertTrue(c.conference_available(item))
            self.assertFalse(c.conference_available({**item,'participants_ready':False}))

    def test_capture_failure_cannot_promote_activity(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)
            for sub in ('metadata','failed','video_streaming'): (p/sub).mkdir()
            pending=p/'failed'/'sample.pcap';pending.write_bytes(b'incomplete')
            result=c.finish_record(dict(session_id='s1',success=True,actual_activity='video_streaming'),p,pending,False)
            self.assertFalse(result['success']);self.assertIsNone(result['actual_activity'])
            self.assertEqual(Path(result['pcap_path']).parent.name,'failed')
            self.assertEqual(list((p/'video_streaming').iterdir()),[])

    def test_pcap_empty_valid_and_truncated(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'a.pcap'
            header=b'\xd4\xc3\xb2\xa1'+struct.pack('<HHIIII',2,4,0,0,128,1)
            p.write_bytes(header);self.assertEqual(c.pcap_packets(p),0)
            p.write_bytes(header+struct.pack('<IIII',1,0,4,4)+b'abcd')
            self.assertEqual(c.pcap_packets(p),1)
            p.write_bytes(p.read_bytes()[:-1])
            with self.assertRaises(ValueError):c.pcap_packets(p)

    def test_analysis_covers_exact_top100(self):
        import csv
        with (Path(c.__file__).parent/'analysis/top100_assessment.csv').open() as f:
            rows=list(csv.DictReader(f))
        self.assertEqual([int(r['rank']) for r in rows],list(range(1,101)))
        self.assertEqual(len({r['domain'] for r in rows}),100)
        self.assertTrue(all(r['source_url'] and r['planned_browser_action'] and r['uncertainty'] for r in rows))


if __name__=='__main__':unittest.main()
