# Workspace persistence probe

`scripts/workspace_probe.py` answers one question before the cloud-clone evidence design is finalised: does a
scheduled cloud run keep its `cron_tracking` files from one fire to the next?

Each run writes one small marker file under `<root>/workspace_probe/` and lists the markers earlier runs left
behind. It reads no broker, places no order, touches no network and never reads the account number.

```bash
python3 -m scripts.workspace_probe [--root DIR] [--now YYYY-MM-DDTHH:MM:SSZ]
```

`--root` defaults to `$DCA_CRON_TRACKING_ROOT` (see `dca_paths.cron_tracking_root`). Exit 0 prints one JSON
object; exit 2 means the root was refused (it is a file, or the filesystem root).

```json
{"root": "/home/user/workspace/cron_tracking",
 "marker": "workspace_probe/20260930T134000Z.json",
 "written_at_utc": "2026-09-30T13:40:00Z",
 "prior_count": 1,
 "prior_markers": [{"name": "20260930T093500Z.json", "written_at_utc": "2026-09-30T09:35:00Z",
                    "age_seconds": 15900, "readable": true}]}
```

## How to use the result

Run it as a scheduled task twice (for example 09:40 and 10:40 ET) from the same environment the live slots use.

- The second run lists the first run's marker: the workspace persists between fires, so same-day evidence
  written by one slot is already visible to the next. A private evidence store then becomes a backup copy.
- The second run reports `prior_count: 0`: every fire starts empty, so the evidence store and slot claim in
  `docs/cloud_clone_runs.md` are essential.

Age is measured from the time recorded inside each marker, never from file modification time, which a copy or
restore would reset. An unreadable marker is reported with `readable: false` and does not stop the probe.
