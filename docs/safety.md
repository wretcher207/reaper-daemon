# Safety Notes

- Commands are local files. Anything that can write to `inbox` can control
  REAPER.
- Keep the bridge folder local. Do not share it over the network.
- Mutating bridge commands are wrapped in REAPER undo blocks.
- Bad commands should produce failed result JSON instead of killing the bridge.
- The bridge never runs external processes. Drum generation is handled by the
  separate PowerShell worker or by the agent itself.
- For plugin automation, parameter discovery must happen before setting values.

