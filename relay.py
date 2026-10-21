#!/usr/bin/env python3
"""Relay for the Santa Barbara Coastal Almanac and The Channel on TRMNL.

TRMNL's servers cannot reach two of the sources these screens need. CDIP's
server answers them with 403 Forbidden, and NOAA's ERDDAP server often takes
longer than the 10 seconds TRMNL allows. This script runs on a schedule in
GitHub Actions, fetches both with time to spare, checks what came back, and
writes the files GitHub Pages then serves to TRMNL.

A source that fails keeps its last good file, so the screens see an older
reading with its own timestamp and never a broken one. The screens judge
age by those timestamps: the buoy readings go to dashes after three hours,
and The Channel stops shading the water when CDIP's hour is six hours old.

  python3 relay.py --site site [--previous https://OWNER.github.io/REPO/]

Writes into the site folder:
  almanac-buoys.json   NDBC 46053 and 46054, last 6 hours (ERDDAP's JSON table)
  channel-buoys.json   NDBC 46053 46054 46218 46069 46025, last 3 hours, with positions
  cdip-hs.txt          CDIP MOP CA_0.01 nowcast wave height, every third point (OPeNDAP text)
  status.json          when each source was last fetched and what happened
Only the Python standard library is used.
"""
import argparse, datetime, json, os, re, sys, time, urllib.error, urllib.request

ERDDAP = 'https://coastwatch.pfeg.noaa.gov/erddap/tabledap/cwwcNDBCMet.json'
SOURCES = {
    'almanac-buoys.json': {
        'kind': 'erddap',
        'url': ERDDAP + '?station,time,WD,WSPD,GST,WVHT,DPD,MWD,WTMP&station=~%2246053%7C46054%22'
                        '&time%3E=now-6hours&orderByDescending(%22time%22)',
        'columns': ['station', 'time', 'WD', 'WSPD', 'GST', 'WVHT', 'DPD', 'MWD', 'WTMP'],
    },
    'channel-buoys.json': {
        'kind': 'erddap',
        'url': ERDDAP + '?station,time,longitude,latitude,WD,WSPD,GST,WVHT,DPD,MWD,WTMP'
                        '&station=~%2246053%7C46054%7C46218%7C46069%7C46025%22'
                        '&time%3E=now-3hours&orderByDescending(%22time%22)',
        'columns': ['station', 'time', 'longitude', 'latitude', 'WD', 'WSPD', 'GST', 'WVHT', 'DPD', 'MWD', 'WTMP'],
    },
    'cdip-hs.txt': {
        'kind': 'cdip',
        'url': 'https://thredds.cdip.ucsd.edu/thredds/dodsC/cdip/model/MOP_grids/CA_0.01_nowcast.nc.ascii'
               '?waveHs%5B5%5D%5B162:3:255%5D%5B413:3:599%5D',
    },
}
TRIES, TIMEOUT, PAUSE = 3, 60, 10


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def fetch(url, agent):
    req = urllib.request.Request(url, headers={'User-Agent': agent, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, r.read()


def check_erddap(body, columns):
    """ERDDAP's table, intact, with the columns the screens read and at least one dated row"""
    d = json.loads(body.decode('utf-8'))
    t = d['table']
    missing = [c for c in columns if c not in t['columnNames']]
    if missing:
        raise ValueError('columns missing: %s' % missing)
    rows = t['rows']
    if not isinstance(rows, list) or not rows:
        raise ValueError('no rows')
    it = t['columnNames'].index('time')
    times = sorted(r[it] for r in rows)
    for s in (times[0], times[-1]):
        datetime.datetime.strptime(s, '%Y-%m-%dT%H:%M:%SZ')
    return json.dumps(d, separators=(',', ':')).encode('utf-8'), times[-1]


def check_cdip(body):
    """CDIP's text with the full 32 by 63 grid and the nowcast hour it belongs to"""
    t = body.decode('latin-1')
    if 'waveHs.waveHs[1][32][63]' not in t:
        raise ValueError('not the expected grid: %r' % t[:120])
    if len(re.findall(r'^\[0\]\[\d+\],', t, re.M)) != 32:
        raise ValueError('grid rows missing')
    m = re.search(r'waveHs\.waveTime\[1\]\s*\n\s*(\d+)', t)
    if not m:
        raise ValueError('no nowcast time')
    when = datetime.datetime.fromtimestamp(int(m.group(1)), datetime.timezone.utc)
    return body, when.strftime('%Y-%m-%dT%H:%M:%SZ')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--site', default='site')
    ap.add_argument('--previous', default='', help='the published site, to keep last good files')
    a = ap.parse_args()
    os.makedirs(a.site, exist_ok=True)
    repo = os.environ.get('GITHUB_REPOSITORY', 'local')
    agent = 'almanac-relay/1 (+https://github.com/%s)' % repo

    # the last published copies, so a source that fails now keeps its last good file
    old_status = {}
    if a.previous:
        for name in list(SOURCES) + ['status.json']:
            try:
                code, body = fetch(a.previous.rstrip('/') + '/' + name, agent)
                if code == 200:
                    if name == 'status.json':
                        old_status = json.loads(body.decode('utf-8')).get('sources', {})
                    else:
                        open(os.path.join(a.site, name), 'wb').write(body)
            except Exception as e:
                print('no previous %s: %s' % (name, e))

    status = {'generated_at': now_iso(), 'sources': {}}
    for name, s in SOURCES.items():
        st = dict(old_status.get(name, {}))
        st['url'] = s['url']
        st['tried_at'] = now_iso()
        err = None
        for k in range(TRIES):
            t0 = time.time()
            try:
                code, body = fetch(s['url'], agent)
                if s['kind'] == 'erddap':
                    out, newest = check_erddap(body, s['columns'])
                else:
                    out, newest = check_cdip(body)
                open(os.path.join(a.site, name), 'wb').write(out)
                st.update({'ok': True, 'fetched_at': now_iso(), 'newest_reading': newest,
                           'bytes': len(out), 'seconds': round(time.time() - t0, 1), 'error': None})
                err = None
                break
            except urllib.error.HTTPError as e:
                err = 'HTTP %d %s' % (e.code, (e.read() or b'')[:160].decode('latin-1', 'replace').strip())
            except Exception as e:
                err = '%s: %s' % (type(e).__name__, e)
            print('%s try %d failed after %.1f s: %s' % (name, k + 1, time.time() - t0, err))
            if k + 1 < TRIES:
                time.sleep(PAUSE)
        if err:
            st.update({'ok': False, 'error': err})
            print('%s kept its last good file (%s)' % (name, st.get('fetched_at', 'none yet')))
        else:
            print('%s ok: %d bytes, newest reading %s, %.1f s' % (name, st['bytes'], st['newest_reading'], st['seconds']))
        status['sources'][name] = st

    open(os.path.join(a.site, 'status.json'), 'w').write(json.dumps(status, indent=1))
    open(os.path.join(a.site, '.nojekyll'), 'w').write('')
    open(os.path.join(a.site, 'index.html'), 'w').write(status_page(status))
    # never fail the job for a source: the rest still publishes
    return 0


def status_page(status):
    """A plain page a person can open on a phone to see whether the relay is healthy"""
    try:
        from zoneinfo import ZoneInfo
        la = ZoneInfo('America/Los_Angeles')
    except Exception:
        la = datetime.timezone.utc

    def local(iso):
        if not iso:
            return 'never'
        t = datetime.datetime.strptime(iso, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
        return t.astimezone(la).strftime('%a %b %-d, %-I:%M %p')

    names = {'almanac-buoys.json': 'Buoys for the Almanac (46053, 46054)',
             'channel-buoys.json': 'Buoys for The Channel (five stations)',
             'cdip-hs.txt': 'CDIP swell model for The Channel'}
    rows = []
    for name, s in status['sources'].items():
        ok = s.get('ok')
        state = 'OK' if ok else 'Kept last good copy' if s.get('fetched_at') else 'Never fetched'
        note = '' if ok else (s.get('error') or '')[:140]
        rows.append('<tr><td>%s<br><a href="%s">%s</a></td><td class="%s">%s</td><td>%s</td><td>%s</td></tr>'
                    % (names.get(name, name), name, name, 'ok' if ok else 'bad', state,
                       local(s.get('newest_reading')), local(s.get('fetched_at')) + ('<br><small>%s</small>' % note if note else '')))
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Almanac relay</title><style>'
            'body{font:16px/1.45 system-ui,-apple-system,sans-serif;margin:24px auto;max-width:760px;padding:0 16px;color:#111}'
            'h1{font-size:22px;margin:0 0 4px}p{color:#444;margin:4px 0 16px}table{border-collapse:collapse;width:100%%}'
            'td,th{border-bottom:1px solid #ddd;padding:8px 6px;text-align:left;vertical-align:top;font-size:15px}'
            'th{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:#555}.ok{color:#126b2a;font-weight:600}'
            '.bad{color:#9a1c1c;font-weight:600}small{color:#666}a{color:#0b4f8a}</style>'
            '<h1>Almanac relay</h1><p>Fetches CDIP and NOAA buoy data for the TRMNL screens every 15 minutes. '
            'Updated %s, Santa Barbara time.</p>'
            '<table><tr><th>Source</th><th>State</th><th>Newest reading</th><th>Fetched</th></tr>%s</table>'
            '<p><small>Sources: NOAA National Data Buoy Center through CoastWatch ERDDAP, and the Coastal Data '
            'Information Program at Scripps. Times are when each reading was taken and when the relay last '
            'got it.</small></p></html>' % (local(status['generated_at']), ''.join(rows)))


if __name__ == '__main__':
    sys.exit(main())
