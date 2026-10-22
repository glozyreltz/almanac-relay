#!/usr/bin/env python3
"""Relay for the Santa Barbara Coastal Almanac and The Channel on TRMNL.

TRMNL's servers cannot reach two of the sources these screens need. CDIP's
server answers them with 403 Forbidden. The buoys come as NDBC's plain-text
realtime files, which the screens cannot read, and NOAA's ERDDAP copy of them
was slow for TRMNL and, on 22 Oct 2026, missing every wave reading after
08:50 UTC while the buoys kept reporting them. This script runs on a schedule
in GitHub Actions, fetches the sources with time to spare, checks what came
back, and writes the files GitHub Pages then serves to TRMNL.

A source that fails keeps its last good file, so the screens see an older
reading with its own timestamp and never a broken one. CDIP has refused
GitHub's servers too, for a few minutes at a time, and the next run got
through. The screens judge age by the readings' own timestamps: after three
hours the buoys show as silent, and The Channel stops shading the water when
CDIP's hour is six hours old.

  python3 relay.py --site site [--previous https://OWNER.github.io/REPO/]

Writes into the site folder:
  almanac-buoys.json   NDBC 46053 and 46054, last 6 hours
  channel-buoys.json   NDBC 46053 46054 46218 46069 46025, last 3 hours, with positions
                       Both from NDBC's own realtime files, written in the shape of
                       ERDDAP's JSON table and in its units, which the screens read.
  cdip-hs.txt          CDIP MOP CA_0.01 nowcast wave height, every third point (OPeNDAP text)
  status.json          when each source was last fetched and what happened
  index.html           the same status as a page a person can read on a phone
Only the Python standard library is used.
"""
import argparse, datetime, html, json, os, re, sys, time, urllib.error, urllib.request

NDBC = 'https://www.ndbc.noaa.gov/data/realtime2/%s.txt'
# where each buoy sits, as NOAA's ERDDAP reported it on 21 Oct 2026 (NDBC's text files carry no position)
POSITION = {'46053': (34.248, -119.841), '46054': (34.274, -120.459), '46218': (34.451, -120.769),
            '46069': (33.67, -120.2), '46025': (33.749, -119.053)}
SOURCES = {
    'almanac-buoys.json': {
        'kind': 'ndbc', 'stations': ['46053', '46054'], 'hours': 6,
        'url': NDBC % '46053' + ' and 46054',
        'columns': ['station', 'time', 'WD', 'WSPD', 'GST', 'WVHT', 'DPD', 'MWD', 'WTMP'],
    },
    'channel-buoys.json': {
        'kind': 'ndbc', 'stations': ['46053', '46054', '46218', '46069', '46025'], 'hours': 3,
        'url': NDBC % '46053' + ' and 46054, 46218, 46069, 46025',
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


def fetch(url, agent, head=None):
    h = {'User-Agent': agent, 'Accept': '*/*'}
    if head:
        h['Range'] = 'bytes=0-%d' % (head - 1)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, r.read()


def http_error(e):
    """an HTTP refusal as one short line of plain text: the code, the reason, and
    whatever the server's page says beyond that, with its markup removed"""
    err = ('HTTP %d %s' % (e.code, e.reason or '')).strip()
    try:
        body = (e.read() or b'')[:600].decode('latin-1', 'replace')
    except Exception:
        body = ''
    text = re.sub(r'\s+', ' ', re.sub(r'<[^>]*>', ' ', body)).strip()
    if text and not set(text.lower().split()) <= set(err.lower().split()):
        err += ': ' + text[:120]
    return err


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


UNITS = {'WD': 'degrees_true', 'WSPD': 'm s-1', 'GST': 'm s-1', 'WVHT': 'm', 'DPD': 's', 'MWD': 'degrees_true',
         'WTMP': 'degree_C', 'longitude': 'degrees_east', 'latitude': 'degrees_north', 'time': 'UTC'}


def ndbc_rows(text, stn, columns, since):
    """the rows of one NDBC realtime file newer than since, newest first, as ERDDAP would give them:
    MM becomes null, whole numbers stay whole, units unchanged (NDBC and ERDDAP both use m/s, m, s, degC)"""
    lines = [l for l in text.splitlines() if l.strip()]
    head = lines[0].lstrip('#').split()
    if head[:5] != ['YY', 'MM', 'DD', 'hh', 'mm']:
        raise ValueError('%s: not an NDBC realtime file: %r' % (stn, lines[0][:60]))
    idx = {c: head.index(c) for c in ('WDIR', 'WSPD', 'GST', 'WVHT', 'DPD', 'MWD', 'WTMP')}
    name = {'WD': 'WDIR'}
    rows = []
    for l in lines[2:]:
        f = l.split()
        if len(f) < len(head):
            continue  # the last line of a partial download
        t = datetime.datetime(int(f[0]), int(f[1]), int(f[2]), int(f[3]), int(f[4]), tzinfo=datetime.timezone.utc)
        if t < since:
            break
        row = []
        for c in columns:
            if c == 'station':
                row.append(stn)
            elif c == 'time':
                row.append(t.strftime('%Y-%m-%dT%H:%M:%SZ'))
            elif c == 'latitude':
                row.append(POSITION[stn][0])
            elif c == 'longitude':
                row.append(POSITION[stn][1])
            else:
                v = f[idx[name.get(c, c)]]
                row.append(None if v == 'MM' else (int(float(v)) if float(v).is_integer() else float(v)))
        rows.append(row)
    return rows


def fetch_ndbc(s, agent):
    """every station's recent rows in one ERDDAP-shaped table; a station that fails is left out,
    as ERDDAP leaves out a silent buoy, and the file fails only when every station does"""
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=s['hours'])
    rows, errs = [], []
    for stn in s['stations']:
        try:
            code, body = fetch(NDBC % stn, agent, head=40000)
            rows += ndbc_rows(body.decode('latin-1'), stn, s['columns'], since)
        except urllib.error.HTTPError as e:
            errs.append('%s %s' % (stn, http_error(e)))
        except Exception as e:
            errs.append('%s %s: %s' % (stn, type(e).__name__, e))
    if len(errs) == len(s['stations']):
        raise ValueError('; '.join(errs))
    if not rows:
        raise ValueError('no rows in the last %d hours' % s['hours'])
    rows.sort(key=lambda r: r[1], reverse=True)
    d = {'table': {'columnNames': s['columns'], 'columnUnits': [UNITS.get(c) for c in s['columns']], 'rows': rows}}
    out, newest = check_erddap(json.dumps(d).encode('utf-8'), s['columns'])
    return out, newest, errs


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
                partial = []
                if s['kind'] == 'ndbc':
                    out, newest, partial = fetch_ndbc(s, agent)
                else:
                    code, body = fetch(s['url'], agent)
                    out, newest = check_cdip(body)
                open(os.path.join(a.site, name), 'wb').write(out)
                st.update({'ok': True, 'fetched_at': now_iso(), 'newest_reading': newest,
                           'bytes': len(out), 'seconds': round(time.time() - t0, 1),
                           'error': ('missing ' + '; '.join(partial)) if partial else None})
                err = None
                break
            except urllib.error.HTTPError as e:
                err = http_error(e)
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
    # every string from a source or a server is escaped: an error page is data, not markup
    e = html.escape
    rows = []
    for name, s in status['sources'].items():
        ok = s.get('ok')
        state = 'OK' if ok else 'Kept last good copy' if s.get('fetched_at') else 'Never fetched'
        note = (s.get('error') or '')[:140]
        rows.append('<tr><td><b>%s</b><br><a href="%s">%s</a></td><td class="%s" data-l="State">%s</td>'
                    '<td data-l="Newest reading">%s</td><td data-l="Fetched">%s%s</td></tr>'
                    % (e(names.get(name, name)), e(name), e(name), 'ok' if ok else 'bad', e(state),
                       e(local(s.get('newest_reading'))), e(local(s.get('fetched_at'))),
                       '<br><small>%s</small>' % e(note) if note else ''))
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Almanac relay</title><style>'
            'body{font:16px/1.45 system-ui,-apple-system,sans-serif;margin:24px auto;max-width:760px;padding:0 16px;color:#111;background:#fff}'
            'h1{font-size:22px;margin:0 0 4px}p{color:#444;margin:4px 0 16px}table{border-collapse:collapse;width:100%%}'
            'td,th{border-bottom:1px solid #ddd;padding:8px 6px;text-align:left;vertical-align:top;font-size:15px}'
            'th{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:#555}td b{font-weight:600}'
            '.ok{color:#126b2a;font-weight:600}.bad{color:#9a1c1c;font-weight:600}small{color:#666;overflow-wrap:anywhere}a{color:#0b4f8a}'
            '@media (max-width:560px){table,tbody,tr,td{display:block;width:auto}thead{display:none}'
            'tr{border-bottom:1px solid #ddd;padding:10px 0}td{border:0;padding:2px 0}'
            'td[data-l]:before{content:attr(data-l);display:inline-block;min-width:11em;margin-right:.4em;color:#555;font-size:13px;'
            'font-weight:400;text-transform:uppercase;letter-spacing:.05em}}</style>'
            '<h1>Almanac relay</h1><p>Fetches CDIP and NOAA buoy data for the TRMNL screens every 15 minutes. '
            'Updated %s, Santa Barbara time.</p>'
            '<table><thead><tr><th>Source</th><th>State</th><th>Newest reading</th><th>Fetched</th></tr></thead>'
            '<tbody>%s</tbody></table>'
            '<p><small>Sources: NOAA National Data Buoy Center realtime files, and the Coastal Data '
            'Information Program at Scripps. Times are when each reading was taken and when the relay last '
            'got it.</small></p></html>' % (local(status['generated_at']), ''.join(rows)))


if __name__ == '__main__':
    sys.exit(main())
