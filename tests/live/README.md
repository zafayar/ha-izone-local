# Live tests

These drive a real bridge: they change the AC and zones, then put everything
back as it was. Run them only against a system you can disturb, from the
repository root:

```bash
python tests/live/zone_power.py 192.168.1.50
```

- `zone_power.py`: zones turn the AC on and off; timing and status per step.
- `favourite.py`: runs the favourite named "Get Ready" and follows it
  through (edit the name in the script for yours).
- `airflow.py`: sets and restores a zone's minimum and maximum airflow.

`bridge_state.py` takes a snapshot of the AC and zones and restores it,
including the setpoints of closed zones.
