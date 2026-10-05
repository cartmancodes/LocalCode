# Octet documentation

## User guides

- [Using the terminal UI](tui.md): install, keys, commands, permission
  modes, switching models and providers, journals and limits.
- [Use Octet from your phone](remote-control.md): tmux, Tailscale and mosh
  setup step by step, and the `/remote-control` reference.
- [Persistent goals](rust/goals.md): how `/goal` keeps working across turns
  and how it decides it is done.

## Reference

- [Features](rust/tui-features.md): what works today, feature by feature,
  and the preview's boundaries.
- [Parity matrix](rust/parity-matrix.md): the release requirements still
  open, measured against the earlier Python harness.
- [Mascot artwork](design/octet-agent-modes/): the pixel-art reference for
  Octet's nine poses.

## Development history

These are dated working notes kept for their reasoning. They describe the
code as it was when written, so the guides above win where they disagree.

- [Design specs](superpowers/specs/) and
  [implementation plans](superpowers/plans/), named by date.
- [Code reviews](reviews/), named by date.
- [Phone access evaluation](rust/remote-control-ssh.md), the analysis behind
  the tmux, Tailscale and mosh approach, and a
  [proposal for a browser-based remote client](rust/remote-control-plan.md)
  that has not been built.
- An earlier Python harness and web UI were removed on 2026-10-04. They
  remain in git history; the parity matrix names the last commit that has
  them.
