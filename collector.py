#!/usr/bin/env python3
"""Small activity collector. No packet inspection, feature extraction, or ML."""
import argparse
import datetime as dt
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

CATEGORIES = ('web_browsing', 'video_streaming', 'video_conferencing')
STOP = False


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def public_url(url):
    """Do not persist URL queries, fragments, userinfo, or meeting paths."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.hostname or '', p.path, '', ''))


def positive(value):
    n = float(value)
    if not math.isfinite(n) or n <= 0:
        raise argparse.ArgumentTypeError('must be a finite positive number')
    return n


def load_plan(path):
    plan = json.loads(Path(path).read_text())
    if not isinstance(plan, list) or not plan:
        raise ValueError('Plan must be a nonempty JSON list')
    for item in plan:
        if item.get('activity') not in CATEGORIES:
            raise ValueError('Unsupported activity')
        if not re.fullmatch(r'[a-z0-9.-]+', item.get('domain', '')):
            raise ValueError('Invalid domain')
        if item['activity'] != 'video_conferencing':
            validate_url(item.get('url', ''))
        for link in item.get('links', []):
            validate_url(link)
            if urlsplit(link).hostname != urlsplit(item['url']).hostname:
                raise ValueError('Browsing links must remain on the initial hostname')
        for step in item.get('steps', []):
            if step.get('type') not in ('click', 'fill', 'wait') or not step.get('selector'):
                raise ValueError('Steps require click/fill/wait and a CSS selector')
            if step['type'] == 'fill' and not step.get('value_env'):
                raise ValueError('Fill values must come from an environment variable')
    return plan


def validate_url(url):
    p = urlsplit(url)
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('Expected an HTTP(S) URL without embedded credentials')


def conference_available(item):
    return bool(item.get('controlled_test') is True and item.get('participants_ready') is True
                and item.get('meeting_url_env') and os.environ.get(item['meeting_url_env'])
                and item.get('joined_selector'))


def pcap_packets(path):
    """Validate classic PCAP records, not packet contents. Reject truncated/empty files."""
    magics = {b'\xd4\xc3\xb2\xa1':'<', b'\xa1\xb2\xc3\xd4':'>',
              b'\x4d\x3c\xb2\xa1':'<', b'\xa1\xb2\x3c\x4d':'>'}
    with open(path, 'rb') as f:
        header = f.read(24)
        if len(header) != 24 or header[:4] not in magics:
            raise ValueError('Missing/unsupported PCAP header')
        endian = magics[header[:4]]
        snaplen = struct.unpack(endian+'I', header[16:20])[0]
        count = 0
        while True:
            record = f.read(16)
            if not record:
                return count
            if len(record) != 16:
                raise ValueError('Truncated PCAP record header')
            _, _, included, original = struct.unpack(endian+'IIII', record)
            if included > snaplen or included > original or included > 16*1024*1024:
                raise ValueError('Invalid PCAP lengths')
            if len(f.read(included)) != included:
                raise ValueError('Truncated PCAP packet')
            count += 1


# Browser evidence is distinct from packet contents. No decryption/DPI is used.
RTC_HOOK = r'''
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
'''
RTC_STATS = r'''
const done = arguments[arguments.length-1];
Promise.all((window.__collectorPeers || []).map(async pc => {
 const stats = await pc.getStats();
 let incoming=0, outgoing=0, frames=0;
 stats.forEach(s => {
   if (s.type==='inbound-rtp' && (s.kind==='video'||s.mediaType==='video')) {
     incoming += s.bytesReceived || 0; frames += s.framesDecoded || 0;
   }
   if (s.type==='outbound-rtp' && (s.kind==='video'||s.mediaType==='video'))
     outgoing += s.bytesSent || 0;
 });
 return {connected:pc.connectionState==='connected',incoming,outgoing,frames};
})).then(done).catch(() => done([]));
'''
NO_MEDIA = r'''
HTMLMediaElement.prototype.play = function() { return Promise.reject(new Error('Browsing-only session')); };
document.addEventListener('play', e => {if(e.target.pause) e.target.pause();}, true);
'''
VIDEO = r'''
const v=document.querySelector(arguments[0]);
if(!v) return null;
return {time:v.currentTime, frames:v.getVideoPlaybackQuality ? v.getVideoPlaybackQuality().totalVideoFrames : 0,
 paused:v.paused, ended:v.ended, ready:v.readyState, width:v.videoWidth,
 source:v.currentSrc, rate:v.playbackRate};
'''


def video_progress(a, b, elapsed):
    return bool(a and b and b['source'] and a['source'] == b['source']
                and not b['paused'] and not b['ended'] and b['ready'] >= 2 and b['width'] > 0
                and 0.1 < b['time'] - a['time'] <= elapsed * 2 + 1
                and b['frames'] > a['frames'])


def rtc_progress(a, b):
    return any(x['connected'] and y['connected'] and y['incoming'] > x['incoming']
               and y['outgoing'] > x['outgoing'] and y['frames'] > x['frames']
               for x, y in zip(a, b))


def browser_worker(pipe, item, settings, deadline, profile):
    """Separate process lets supervisor stop a hung WebDriver without losing PCAP cleanup."""
    os.setsid()
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    driver = None
    try:
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.chrome.service import Service
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        def emit(event, **data):
            pipe.send({'event':event, 'at':utc(), **data})
        def remaining():
            value = deadline - time.monotonic() - 1.0
            if value <= 0:
                raise TimeoutError('Session deadline')
            return value
        def wait_for(selector):
            return WebDriverWait(driver, min(settings['timeout'], remaining()), poll_frequency=.2).until(
                EC.visibility_of_element_located((By.CSS_SELECTOR, selector)))
        def navigate(url):
            driver.set_page_load_timeout(min(settings['timeout'], remaining()))
            driver.get(url)
            if not driver.current_url.startswith(('https://','http://')):
                raise RuntimeError('Browser navigation error')
            body = driver.find_element(By.TAG_NAME, 'body').text
            title = driver.title.lower()
            if (item['activity']=='web_browsing' and len(body.strip()) < 20) or any(x in title for x in (
                    'access denied', 'just a moment', 'captcha', 'privacy error', '403 forbidden')):
                raise RuntimeError('Empty, blocked, or challenge page')
            if item['activity'] != 'video_conferencing':
                emit('navigate', url=public_url(driver.current_url), title=driver.title[:160])
            else:
                emit('navigate', host=urlsplit(driver.current_url).hostname)
            # Chrome performance logs expose HTTP status for navigation, without reading packet data.
            for entry in driver.get_log('performance'):
                msg = json.loads(entry['message'])['message']
                if msg['method'] == 'Network.responseReceived' and msg['params'].get('type') == 'Document':
                    response = msg['params']['response']
                    if public_url(response['url']) == public_url(driver.current_url) and response['status'] >= 400:
                        raise RuntimeError('HTTP navigation failure')
        options = webdriver.ChromeOptions()
        for arg in ('--headless=new', '--disable-background-networking', '--disable-sync',
                    '--no-first-run', '--disable-default-apps', '--disable-extensions',
                    '--disable-component-update', '--window-size=1280,900',
                    '--autoplay-policy=no-user-gesture-required', f'--user-data-dir={profile}'):
            options.add_argument(arg)
        options.add_experimental_option('prefs', {'credentials_enable_service':False,
                'profile.password_manager_enabled':False, 'download_restrictions':3})
        if settings.get('chrome_binary'):
            options.binary_location = settings['chrome_binary']
        if item['activity'] == 'video_conferencing':
            options.add_argument('--use-fake-device-for-media-stream')
            options.add_argument('--use-fake-ui-for-media-stream')
        options.set_capability('goog:loggingPrefs', {'performance':'ALL'})
        service = Service(executable_path=settings['chromedriver']) if settings.get('chromedriver') else Service()
        driver = webdriver.Chrome(options=options, service=service)
        driver.set_script_timeout(min(5, settings['timeout']))
        driver.execute_cdp_cmd('Network.enable', {})
        driver.execute_cdp_cmd('Network.setCacheDisabled', {'cacheDisabled':True})
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source':RTC_HOOK if item['activity']=='video_conferencing' else NO_MEDIA if item['activity']=='web_browsing' else ''})
        emit('ready', browser_version=driver.capabilities.get('browserVersion'))
        if pipe.recv() != 'go':
            return
        url = os.environ[item['meeting_url_env']] if item['activity']=='video_conferencing' else item['url']
        validate_url(url)
        navigate(url)
        for step in item.get('steps', []):
            el = wait_for(step['selector'])
            if step['type']=='click':
                el.click()
            elif step['type']=='fill':
                el.clear(); el.send_keys(os.environ[step['value_env']])
            emit('step', action=step['type'])  # Never log filled values or secret-bearing selectors.
        if item.get('frame_selector'):
            driver.switch_to.frame(wait_for(item['frame_selector']))
        minimum = settings['min_verified_seconds']
        verified = 0.0
        if item['activity']=='web_browsing':
            if item.get('content_selector'):
                wait_for(item['content_selector'])
            next_link = 0
            last_link = time.monotonic()
            while deadline-time.monotonic() > 1.5:
                driver.execute_script('window.scrollBy(0, Math.round(window.innerHeight * 0.7))')
                emit('scroll')
                before = time.monotonic()
                time.sleep(min(2, max(0,deadline-time.monotonic()-1.5)))
                verified += time.monotonic()-before
                if next_link < len(item.get('links', [])) and time.monotonic()-last_link >= 8:
                    navigate(item['links'][next_link]); next_link += 1; last_link = time.monotonic()
            if verified < minimum:
                raise RuntimeError('Insufficient browsing observation time')
        else:
            selector = item.get('video_selector', 'video')
            if item['activity']=='video_streaming':
                wait_for(selector)
                driver.execute_script('const v=document.querySelector(arguments[0]); v.muted=true; v.play().catch(()=>{});', selector)
                emit('play_requested')
                previous = driver.execute_script(VIDEO, selector)
            else:
                wait_for(item['joined_selector'])
                emit('joined_indicator_visible')
                previous = driver.execute_async_script(RTC_STATS)
            sampled = time.monotonic()
            last_progress = sampled
            while deadline-time.monotonic() > 1.5:
                time.sleep(min(1,max(0,deadline-time.monotonic()-1.5)))
                now = time.monotonic()
                if item['activity']=='video_streaming':
                    current = driver.execute_script(VIDEO, selector)
                    progressed = video_progress(previous, current, now-sampled)
                    evidence = {'media_time':current.get('time') if current else None,
                                'frames':current.get('frames') if current else None}
                else:
                    current = driver.execute_async_script(RTC_STATS)
                    progressed = rtc_progress(previous, current)
                    evidence = {'peers':current}
                if progressed:
                    verified += now-sampled; last_progress = now
                emit('media_sample', progressing=progressed, **evidence)
                previous = current; sampled = now
                if now-last_progress > settings['stall_seconds']:
                    raise RuntimeError('Playback/call absent or stalled')
            if verified < minimum or time.monotonic()-last_progress > 3:
                raise RuntimeError('Insufficient sustained media evidence')
        emit('done', success=True, actual_activity=item['activity'], verified_seconds=round(verified,3))
        # Stop capture before browser teardown generates unrelated shutdown traffic.
        if pipe.poll(5):
            pipe.recv()
    except BaseException as error:
        try:
            # Exception text may contain meeting credentials or URLs; retain type only.
            pipe.send({'event':'done', 'at':utc(), 'success':False, 'error_type':type(error).__name__,
                       'reason':str(error) if type(error) in (RuntimeError,TimeoutError) else 'Browser action failed; inspect configuration/access'})
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
        pipe.close()


def stop_capture(proc):
    if not proc:
        return None
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait(timeout=1)
    return proc.returncode


def stop_worker(proc):
    if not proc:
        return
    proc.join(timeout=.5)
    # Kill only this worker's process group, including an orphaned ChromeDriver/Chrome.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        proc.join(timeout=.5)
    if proc.is_alive():  # Worker may have been interrupted before setsid.
        proc.kill(); proc.join(timeout=1)


def finish_record(meta, output, pending, capture_ok):
    good = meta.get('success',False) and capture_ok and meta.get('actual_activity') in CATEGORIES
    meta['success'] = bool(good)
    if not good:
        meta['actual_activity'] = None
    if pending and pending.exists():
        destination = output / (meta['actual_activity'] if good else 'failed') / pending.name
        pending.replace(destination)
        meta['pcap_path'] = str(destination.resolve())
    else:
        meta['pcap_path'] = None
    meta['ended_at'] = utc()
    record_path = output / 'metadata' / (meta['session_id']+'.json')
    temporary = record_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(meta,indent=2)+'\n')
    temporary.replace(record_path)
    with (output/'sessions.jsonl').open('a') as f:
        f.write(json.dumps(meta)+'\n')
    return meta


def run_session(item, args, run_deadline, output):
    started = time.monotonic()
    deadline = min(run_deadline, started+args.session_seconds)
    ident = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')+'_'+uuid.uuid4().hex[:8]
    name = f'{ident}_{item["domain"]}_{item["activity"]}.pcap'
    pending = output/'failed'/name  # Fail closed even if supervisor is killed abruptly.
    meta = {'session_id':ident, 'domain':item['domain'], 'requested_activity':item['activity'],
            'actual_activity':None, 'started_at':utc(), 'success':False, 'actions':[],
            'interface':args.interface, 'bpf_filter':args.bpf_filter, 'snaplen':args.snaplen,
            'session_budget_seconds':round(deadline-started,3), 'pcap_path':None}
    if item['activity']=='video_conferencing' and not conference_available(item):
        meta.update(status='unavailable', reason='Controlled meeting URL, joined selector, and participant setup not supplied',duration_seconds=0)
        return finish_record(meta,output,None,False)
    capture = worker = parent = child = log = None
    capture_ok = False
    profile = tempfile.mkdtemp(prefix='collector-chrome-')
    try:
        parent,child = mp.get_context('spawn').Pipe()
        worker = mp.get_context('spawn').Process(target=browser_worker,
            args=(child,item,vars(args),deadline,profile))
        worker.start(); child.close()
        ready = False
        while time.monotonic() < deadline and not STOP:
            if parent.poll(.1):
                event = parent.recv();meta['actions'].append(event)
                if event['event']=='ready':
                    ready = True; meta['browser_version']=event['browser_version']; break
                if event['event']=='done':
                    meta.update(event);break
            if not worker.is_alive():
                break
        if not ready:
            raise RuntimeError('Browser startup failed, interrupted, or exceeded session budget')
        log = (output/'metadata'/(ident+'.tcpdump.log')).open('wb')
        capture = subprocess.Popen([args.tcpdump,'-i',args.interface,'-n','-p','-U','-s',str(args.snaplen),
                                    '-w',str(pending),args.bpf_filter],stdout=subprocess.DEVNULL,stderr=log)
        # Wait for libpcap initialization/header; never navigate before capture is ready.
        while time.monotonic() < deadline and not STOP:
            if capture.poll() is not None:
                raise RuntimeError('tcpdump could not start; see metadata tcpdump log and BPF permissions')
            if pending.exists() and pending.stat().st_size>=24:
                break
            time.sleep(.05)
        else:
            raise RuntimeError('Capture initialization interrupted or timed out')
        meta['capture_started_at']=utc(); parent.send('go')
        while time.monotonic() < deadline and not STOP:
            if capture.poll() is not None:
                raise RuntimeError('tcpdump exited during session')
            if parent.poll(.1):
                event=parent.recv();meta['actions'].append(event)
                if event['event']=='done':
                    meta.update({k:v for k,v in event.items() if k not in ('event','at')});break
            if not worker.is_alive():
                raise RuntimeError('Browser worker exited without result')
        else:
            meta.update(success=False,reason='Interrupted' if STOP else 'Session deadline exceeded')
        rc=stop_capture(capture);meta['capture_ended_at']=utc();meta['tcpdump_exit_code']=rc
        count=pcap_packets(pending);meta['packet_count']=count
        capture_ok=rc==0 and count>0
        if not capture_ok:
            meta['reason']='Capture empty or tcpdump unsuccessful'
    except (Exception,KeyboardInterrupt) as error:
        meta.update(success=False,reason=str(error),error_type=type(error).__name__)
    finally:
        stop_capture(capture)
        if meta.get('capture_started_at'):
            meta.setdefault('capture_ended_at',utc())
            meta['capture_duration_seconds']=round((dt.datetime.fromisoformat(meta['capture_ended_at'])-dt.datetime.fromisoformat(meta['capture_started_at'])).total_seconds(),3)
        if parent:
            try:
                parent.send('stop')
            except (BrokenPipeError,EOFError,OSError):
                pass
        stop_worker(worker)
        if parent: parent.close()
        if child: child.close()
        if log: log.close()
        shutil.rmtree(profile,ignore_errors=True)
    meta['duration_seconds']=round(time.monotonic()-started,3)
    meta['status']='success' if meta.get('success') and capture_ok else 'failed'
    return finish_record(meta,output,pending,capture_ok)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',default=str(Path(__file__).with_name('sessions.json')))
    parser.add_argument('--output',default=str(Path(__file__).with_name('dataset')))
    parser.add_argument('--total-seconds',type=positive,default=720,help='Total experiment wall-clock budget, including setup')
    parser.add_argument('--session-seconds',type=positive,default=90,help='Per-session maximum, including setup/navigation')
    parser.add_argument('--timeout',type=positive,default=20,help='Navigation and element wait timeout')
    parser.add_argument('--min-verified-seconds',type=positive,default=5)
    parser.add_argument('--stall-seconds',type=positive,default=10)
    parser.add_argument('--interface',default='en0')
    parser.add_argument('--bpf-filter',default='ip or ip6',help='Keep TCP, UDP/QUIC, DNS and WebRTC; optionally filter by local host')
    parser.add_argument('--snaplen',type=int,default=128,help='128 saves packet prefixes and original lengths; 0 for full packets')
    parser.add_argument('--tcpdump',default=shutil.which('tcpdump') or '/usr/sbin/tcpdump')
    parser.add_argument('--chrome-binary')
    parser.add_argument('--chromedriver')
    parser.add_argument('--once',action='store_true',help='Visit plan once rather than repeat until total budget')
    parser.add_argument('--dry-run',action='store_true',help='Validate plan and display scheduling without traffic')
    args=parser.parse_args()
    if args.snaplen < 0 or args.snaplen>262144:
        parser.error('snaplen must be 0..262144')
    if args.session_seconds < args.min_verified_seconds+3:
        parser.error('Session must allow at least min-verified-seconds + 3 seconds')
    try:
        plan=load_plan(args.plan)
    except (ValueError,OSError) as e:
        parser.error(str(e))
    if args.dry_run:
        print(json.dumps({'total_budget_seconds':args.total_seconds,'per_session_max_seconds':args.session_seconds,
            'repeats':not args.once,'sessions':[{'domain':i['domain'],'activity':i['activity'],
            'available':i['activity']!='video_conferencing' or conference_available(i)} for i in plan]},indent=2))
        return 0
    if os.geteuid()==0:
        parser.error('Run as your normal user; grant BPF capture access separately. Do not run Chrome as root.')
    output=Path(args.output).resolve()
    for category in (*CATEGORIES,'failed','metadata'):
        (output/category).mkdir(parents=True,exist_ok=True)
    def interrupt(signum,frame):
        global STOP
        STOP=True
    signal.signal(signal.SIGINT,interrupt);signal.signal(signal.SIGTERM,interrupt)
    run_start=time.monotonic();run_deadline=run_start+args.total_seconds
    active=[];results=[]
    for item in plan:
        if item['activity']=='video_conferencing' and not conference_available(item):
            result=run_session(item,args,run_deadline,output);results.append(result)
            print(f'{item["domain"]}: unavailable (controlled meeting setup required)',flush=True)
        else:
            active.append(item)
    while active and not STOP and run_deadline-time.monotonic()>=args.min_verified_seconds+3:
        for item in active:
            if STOP or run_deadline-time.monotonic()<args.min_verified_seconds+3: break
            result=run_session(item,args,run_deadline,output);results.append(result)
            print(f'{item["domain"]} / {item["activity"]}: {result["status"]}',flush=True)
            # A capture permission/setup failure should not launch dozens of browser retries.
            if 'tcpdump could not start' in result.get('reason',''):
                active=[];break
        if args.once: break
    summary={'started_at':results[0]['started_at'] if results else utc(), 'ended_at':utc(),
             'elapsed_seconds':round(time.monotonic()-run_start,3),'total_budget_seconds':args.total_seconds,
             'successful_sessions':sum(r['success'] for r in results),'sessions':len(results),'interrupted':STOP,
             'note':'Bounded process cleanup may extend the wall-clock budget by several seconds; no new session starts after it.'}
    (output/('run_'+uuid.uuid4().hex[:8]+'.json')).write_text(json.dumps(summary,indent=2)+'\n')
    print(json.dumps(summary,indent=2))
    return 130 if STOP else 0 if summary['successful_sessions'] else 1


if __name__=='__main__':
    raise SystemExit(main())
