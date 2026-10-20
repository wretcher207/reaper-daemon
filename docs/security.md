# Security

The bridge is a local file control channel. Any process that can write to
`inbox/` can drive REAPER: load FX, change tracks, render. That is fine for a
single-user dev box, which is what it is built for.

## Two rules

- **Keep the bridge folder local.** Not on a network share, not on a synced
  drive, not anywhere another machine or user can write. That is the real
  boundary.
- **Everything is undoable.** Mutating commands run inside REAPER undo
  blocks, so anything an agent does reverts with Ctrl/Cmd+Z.

## Scripts

`run_script` executes Lua the agent supplies, inside REAPER, with the whole
ReaScript API. That is the one command undo does not bound: a script can write
files, change preferences, or call anything the API offers. It is off by
default and opens only when `allow_script_execution` is `true` in
`bridge/bridge_config.json` (`python3 setup/install.py --allow-script-execution`).
Unlike the disk-write gates, nothing else opens it: `allow_risk_level_3` does
not, and `--allow-disk-writes` leaves it closed. Turn it on for an agent you
would let run ReaScripts by hand.

## Optional token

On a shared or less-trusted box you can require a shared secret. Set
`auth_token` to any string in `bridge/bridge_config.json`. The bridge then
rejects any command without a matching `token` field with `AUTH_FAILED`.
The CLI reads the same config and attaches the token automatically.

The limit: the token lives in a locally readable config. It stops accidental
writes and other apps, not a local attacker who can read that file. Keeping
the folder local is still the actual protection.
