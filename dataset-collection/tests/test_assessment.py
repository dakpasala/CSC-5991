import csv
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import assess_domains as a


class AssessmentTests(unittest.TestCase):
    def test_raw_signals_ignore_script_comment_template(self):
        parser = a.PageSignals()
        parser.feed('<title>A &amp; B</title><meta name="description" content="Read this"><meta property="og:type" content="article"><script>"<video>"</script><!-- <audio> --><template><video></video></template><svg><title>Logo</title></svg>')
        s = parser.signals()
        self.assertEqual(s['title'], 'A & B')
        self.assertFalse(s['video_tag'])
        self.assertFalse(s['audio_tag'])
        self.assertEqual(a.propose_category(s)[0], 'web_browsing')
        s['meta_description'] = 'The app for independent voices'
        self.assertEqual(a.propose_category(s)[0], 'unknown')

    def test_fail_closed_and_media_requires_element(self):
        s = dict(title='Movies', meta_description='Watch movies online', og_type='video.movie')
        self.assertEqual(a.propose_category(s)[0], 'unknown')
        self.assertEqual(a.propose_category(dict(s, video_tag=True))[0], 'video_streaming')
        self.assertEqual(a.propose_category(dict(s, video_tag=True, audio_tag=True))[0], 'unknown')
        self.assertEqual(a.propose_category(dict(s, title='Access denied', video_tag=True))[0], 'unknown')
        self.assertEqual(a.propose_category(dict(title='Talk', meta_description='Secure messaging app'))[0], 'excluded_conferencing')
        self.assertEqual(a.propose_category(dict(title='Daily News', meta_description='Breaking news and reporting'))[0], 'web_browsing')
        self.assertEqual(a.propose_category(dict(title='Memes and News', meta_description='Funny memes and news stories'))[0], 'unknown')
        self.assertEqual(a.propose_category(dict(title='Radio News', meta_description='Listen to free internet radio news'))[0], 'unknown')
        self.assertEqual(a.propose_category(dict(title='News platform', meta_description='Build your news platform'))[0], 'unknown')
        self.assertEqual(a.propose_category(dict(title='Files', meta_description='Download files'))[0], 'unknown')

    def test_one_dns_one_get_no_redirect_follow(self):
        response = MagicMock()
        response.status = 302
        response.read.return_value = b'<title>Redirect</title>'
        response.getheader.return_value = 'text/html'
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(a.socket, 'getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 443))]) as dns, patch.object(a.socket, 'socket'), patch.object(a.ssl, 'create_default_context'), patch.object(a.http.client, 'HTTPSConnection', return_value=connection):
            result = a.probe('example.com', 1)
        dns.assert_called_once()
        connection.request.assert_called_once()
        response.read.assert_called_once_with(a.BODY_LIMIT)
        self.assertEqual(result['proposed_categories'], 'unknown')
        self.assertEqual(len(result['http']), 1)

    def test_preserve_columns_and_preexisting_categories(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'inventory.csv'
            fields = ['rank', 'domain', 'login_required', 'basis', 'note', 'category']
            rows = [dict(zip(fields, [str(i), f'd{i}.org', 'partial', 'original', 'unchanged, note', category])) for i, category in enumerate(['unknown', 'audio_streaming', 'excluded_conferencing', 'unknown'], 1)]
            with path.open('w', newline='') as f:
                w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(rows)
            db = sqlite3.connect(':memory:')
            db.execute('CREATE TABLE domains (domain TEXT, rank INTEGER, result TEXT)')
            for row in rows:
                result = dict(signal_version=a.SIGNAL_VERSION, http=[dict(status=200, signals=dict(title='Story', meta_description='Article text', og_type='article'))])
                if row['rank'] == '4':
                    result['signal_version'] = 0
                db.execute('INSERT INTO domains VALUES (?, ?, ?)', (row['domain'], int(row['rank']), json.dumps(result)))
            a.apply_categories(path, db)
            with path.open() as f:
                after = list(csv.DictReader(f))
            self.assertEqual(after[0]['category'], 'web_browsing')
            for before, updated in zip(rows, after):
                for field in fields[:-1]:
                    self.assertEqual(before[field], updated[field])
            self.assertEqual(after[1:], rows[1:])
            db.close()


if __name__ == '__main__':
    unittest.main()
