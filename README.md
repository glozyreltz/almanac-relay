# almanac relay

A small relay for two TRMNL screens on a TRMNL X: the Santa Barbara Coastal Almanac and The Channel.

## Why it exists

TRMNL's servers cannot reach two of the sources the screens need.

- CDIP's server answers TRMNL with 403 Forbidden. It answers other servers normally.
- NOAA's ERDDAP server often takes longer than the 10 seconds TRMNL allows.

GitHub's servers reach both. Every 15 minutes this repository fetches them, checks what came back, and publishes the files on GitHub Pages, which TRMNL reaches without trouble.

## What it publishes

| File | Source | Used by |
|---|---|---|
| `almanac-buoys.json` | NDBC buoys 46053 and 46054, the last 6 hours, through ERDDAP | The Almanac, seas and water |
| `channel-buoys.json` | NDBC buoys 46053, 46054, 46218, 46069 and 46025, the last 3 hours | The Channel, buoy boxes |
| `cdip-hs.txt` | CDIP MOP nowcast wave height, every third point of the 0.01 degree grid | The Channel, water shading |
| `status.json` | When each source was last fetched and what happened | You, when something looks wrong |
| `index.html` | The same status as a page you can open on a phone | You |

The status page is at https://glozyreltz.github.io/almanac-relay/ once GitHub Pages is on.

The files are the sources' own data, unchanged apart from the JSON being written compactly.

## When a source fails

The file keeps its last good copy, and `status.json` records the error. The screens judge age by each reading's own timestamp. The buoy readings go to dashes after three hours, and The Channel stops shading the water when CDIP's hour is six hours old. Nothing old is shown as current.

## Running it

- It runs by itself on a schedule. GitHub may start a run late when it is busy.
- To run it now, open the Actions tab, choose relay, and press Run workflow.
- To stop it, open the Actions tab, choose relay, and disable the workflow. The screens will then show dashes for the buoys and an unavailable note for the shading within a few hours.
- GitHub switches off schedules in public repositories after 60 days without activity. The workflow writes `heartbeat.txt` once a month and re-enables itself to prevent that.

## Locally

```
python3 relay.py --site site
```

Only the Python standard library is used.
