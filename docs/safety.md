# Safety Notes

- Commands are local files. Anything that can write to `inbox` can control
  REAPER.
- Keep the bridge folder local. Do not share it over the network.
- Mutating bridge commands are wrapped in REAPER undo blocks.
- Bad commands should produce failed result JSON instead of killing the bridge.
- The bridge never runs external processes. Drum generation happens in the
  agent's Python (`skills/drum-apparatus/`, driven by `reaperd.py`); the bridge
  only receives the finished MIDI file to insert.
- Commands may carry an optional shared-secret `token`. When `auth_token` is set
  in `bridge_config.json`, the bridge rejects any command without a matching
  token. See the SECURITY section in the README for the threat model.
- For plugin automation, parameter discovery must happen before setting values.

