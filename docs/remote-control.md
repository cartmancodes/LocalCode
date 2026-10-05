# Use Octet from your phone

Run Octet on your Mac and use it from your phone. Your phone uses a terminal
app (Blink Shell or Termius) and reaches the Mac over Tailscale, your own
private network. mosh keeps the connection through Wi-Fi and cellular
changes, and tmux keeps Octet running while your phone is away.

You need:

- a Mac that stays on while you're away, with Octet built
  (`make rust-build` puts it in `target/release/octet`);
- an iPhone or Android phone with Blink Shell (iOS) or Termius (iOS and
  Android);
- a Tailscale account (the free plan is enough).

Setup takes about 20 minutes. Inside Octet, `/remote-control` checks your
progress at any point: it changes nothing, and lists what is ready (`[ok]`),
what is missing (`[!!]`) and the command to fix it. Everything it checks,
and what it can't, is in [The `/remote-control` check](#the-remote-control-check);
if the phone won't connect, see [Troubleshooting](#troubleshooting). Why this
design, and its limits: [the evaluation](rust/remote-control-ssh.md).

## 1. Install tmux and mosh on the Mac

```sh
brew install tmux mosh
mosh-server --version
```

The version must be 1.4.0 or newer; older mosh shows Octet in 256 colours.

On an Apple silicon Mac, Homebrew installs into `/opt/homebrew/bin`, which a
login over SSH doesn't search: SSH runs commands through a shell that reads
only `~/.zshenv`, so the phone gets `mosh-server: command not found`. Add
Homebrew to that file once:

```sh
echo 'export PATH="/opt/homebrew/bin:$PATH"' >> ~/.zshenv
```

## 2. Configure tmux

Create `~/.tmux.conf`, or add these lines to it:

```
# Octet from a phone over mosh: 24-bit colour, and flag bells (approvals)
# from windows you aren't looking at.
set -g default-terminal "tmux-256color"
set -as terminal-features ",xterm-256color:RGB"
set -g monitor-bell on
set -g bell-action any
# Let /copy reach the phone's clipboard.
set -g set-clipboard on
```

If tmux is already running, reload it with `tmux source-file ~/.tmux.conf`.

## 3. Put the Mac on Tailscale

Install the Tailscale app from the Mac App Store (or from
[tailscale.com/download](https://tailscale.com/download)), open it and sign in.
In the app, note the Mac's **machine name**, for example
`my-mac.tail1234.ts.net`. The phone connects to that name.

## 4. Let the phone log in over SSH

Pick one option.

### Option A: Remote Login (works with the App Store Tailscale app)

1. Open **System Settings → General → Sharing** and turn on
   **Remote Login**. Click its info button and set **Allow access for** to
   **Only these users**, with just your account.
2. Turn off password login, so only your phone's key can log in. Run these
   commands, entering your Mac password when asked:

   ```sh
   printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' \
     | sudo tee /etc/ssh/sshd_config.d/010-octet.conf
   sudo sshd -t && echo "SSH settings OK"
   ```

   The file name starts with `010` so it is read before macOS's own
   settings. Turn Remote Login off and on again to apply it.
3. Do step 6 (create a key on the phone) and add the phone's public key
   before you leave the Mac, or you'll be locked out:

   ```sh
   mkdir -p ~/.ssh && chmod 700 ~/.ssh
   echo 'PASTE-THE-PHONE-PUBLIC-KEY-HERE' >> ~/.ssh/authorized_keys
   chmod 600 ~/.ssh/authorized_keys
   ```

Remote Login answers on every network the Mac joins, not only Tailscale.
Key-only login (step 2) is what keeps that safe.

### Option B: Tailscale SSH (only Tailscale can reach the Mac)

This needs Tailscale's open-source build instead of the App Store app; the
App Store app can't accept Tailscale SSH connections.

```sh
brew install tailscale
sudo tailscaled install-system-daemon
sudo tailscale up
sudo tailscale set --ssh
```

The daemon runs as root, so these commands need `sudo` unless you first make
yourself its operator with `sudo tailscale set --operator=$USER`.

Tailscale then handles login with your Tailscale account; no SSH keys are
needed. In the
[Tailscale admin console](https://login.tailscale.com/admin/acls), make sure
the `ssh` rules allow only your own devices to reach this Mac as your user.
`"action": "check"` asks you to sign in again every 12 hours.

## 5. Check from Octet

Start Octet inside a tmux session named `octet`:

```sh
tmux new -A -s octet "octet --engine codex --cwd ~/Projects/my-app --approval-timeout 600"
```

- `--approval-timeout 600` gives you ten minutes to answer an approval from
  the phone before it is denied (the default is 120 seconds).
- `-A` reattaches to the session if it already exists.

Type `/remote-control` on the Mac. It checks the Mac's side only (the phone's
Tailscale and app settings are yours to check), so run it before you leave
the desk or when the phone can't connect. When the Mac is ready, every line
shows `[ok]`, and the report ends with the exact commands for your phone.
With Option A the SSH line reads `[ok] SSH answers on the tailnet address`
instead:

```
Remote control setup (read-only check)
[ok] Running in tmux session "octet"
[ok] Tailscale connected: my-mac.tail1234.ts.net (100.101.102.103)
[ok] Tailscale SSH is on
[ok] mosh-server 1.4.0
Phone (Blink):   mosh you@my-mac.tail1234.ts.net -- tmux new -A -s octet
Phone (Termius): host my-mac.tail1234.ts.net, user you, Mosh on, startup: tmux new -A -s octet
Setup guide: docs/remote-control.md
```

Each `[!!]` line names its fix; the full list is in
[Report lines and fixes](#report-lines-and-fixes). The status line under the
prompt sums it up, for example `Remote control: ready (report above)` or
`Remote control: 2 problems (report above)`.

## 6. Set up the phone

1. Install the **Tailscale** app and sign in with the same account. Leave it
   connected.
2. Set up your terminal app.

**Blink Shell (iOS)**

1. Type `config`, then **Keys & Certificates → +**, and create a key.
   For Option A, copy its public key into `~/.ssh/authorized_keys` on the
   Mac (step 4).
2. Back in **config**, open **Hosts → +**. Set an alias such as `mac`,
   the Mac's machine name as the host name, your Mac user name, and the
   key.
3. Connect with:

   ```
   mosh mac -- tmux new -A -s octet
   ```

**Termius (iOS and Android)**

1. In **Keychain**, generate a key. For Option A, copy its public key into
   `~/.ssh/authorized_keys` on the Mac (step 4).
2. Create a **New Host** with the Mac's machine name as the address, your
   Mac user name, and the key (Option B needs no key). Turn on **Mosh**. If
   Termius can't resolve the name, use the Mac's `100.x.y.z` address from
   the `/remote-control` report instead.
3. Add a startup snippet that runs `tmux new -A -s octet`. If your version
   doesn't run snippets over mosh, type that line after connecting.

You now see the same Octet screen as on the Mac.

## 7. Keep the Mac awake

Octet stops when the Mac sleeps. While you're away, either set
**System Settings → Battery → Options → Prevent automatic sleeping on power
adapter when the display is off** (on a desktop Mac, **System Settings →
Energy**), or run this once:

```sh
tmux new-window -d -t octet caffeinate -dims
```

## The `/remote-control` check

`/remote-control` answers one question: is the Mac ready for the phone? It
is a pre-flight and troubleshooting check, not part of using Octet from the
phone. Termius or Blink does the connecting, and nothing in Octet needs to
run for that.

### Where and when to run it

Run it **on the Mac**, in the Octet session you'll use from the phone
(started inside tmux, step 5):

- before you leave the desk, to be sure the phone will get in;
- after changing the setup, such as a Tailscale update, a new
  `~/.tmux.conf` or a reinstall;
- when the phone can't connect, to rule the Mac in or out.

Typed from the phone it prints the same report, because Octet runs on the
Mac and every check looks at the Mac. By then the phone is already
connected, so the check adds little there.

### Usage

```
/remote-control
/remote-control status
```

Both run the same checks. Any other argument prints
`Use /remote-control or /remote-control status`. The report appears as a
note in the conversation; nothing is sent to the agent and the session is
unchanged.

### What it checks, and how

| Check | How it finds out | Passes when |
| --- | --- | --- |
| tmux session | `$TMUX` is set in Octet's environment, then `tmux display-message -p '#S'` gives the session name. | The session name comes back. |
| tmux colour | `tmux show-options -s terminal-features` and `terminal-overrides`. Shown only when it fails. | An `RGB` feature, or a `Tc`/`RGB` override on older tmux, for a pattern that matches `xterm-256color` (the name the phone arrives under over mosh), such as `xterm-256color`, `xterm*` or `*256col*`. A setting for another terminal, such as `alacritty`, doesn't count. If tmux lists neither option, or Octet isn't in tmux, there is no line. |
| Tailscale | `tailscale status --json`. If `tailscale` isn't on the PATH, the App Store app's own CLI at `/Applications/Tailscale.app/Contents/MacOS/Tailscale`. | `BackendState` is `Running`. The report shows the machine name and first tailnet address. |
| SSH login | Only checked once Tailscale is connected. Two checks at once: `tailscale debug prefs` reports `RunSSH: true` (Option B, skipped for the App Store app), or a TCP connection to port 22 on the Mac's own tailnet address succeeds (Option A). | Either one passes. |
| mosh | `mosh-server --version`. | Version 1.4.0 or newer. |
| Phone commands | Built from `$USER`, the machine name and the tmux session name (`octet` if unknown). | Printed when Tailscale is connected and SSH passes. |

Why two SSH checks: Tailscale SSH serves only connections that arrive
through the tunnel from another device. The Mac connecting to its own
tailnet address is refused even when Tailscale SSH is on, so Octet asks
Tailscale for its setting instead.

### Timing

All the commands run at once and share one 2-second deadline. A command
that hasn't answered by then is stopped and counts as missing (or, for
tmux, as "didn't answer"). The SSH checks then get half a second. So the
report arrives within about 2.5 seconds, even if tmux or Tailscale hangs.
The checks run in the background: while they do, the status line reads
`Checking phone access…`, and the screen keeps drawing, taking keys and
showing the agent's output.

### Report lines and fixes

| Line | Meaning and fix |
| --- | --- |
| `[ok] Running in tmux session "NAME"` | The phone commands use `NAME`. |
| `[!!] Not inside tmux. Quit and restart with: …` | Octet will stop when this terminal closes. Quit it (Ctrl+C twice) and start it with the `tmux new -A -s octet …` line in step 5. |
| `[!!] Inside tmux, but tmux didn't answer; …` | `$TMUX` is set but tmux didn't reply within 2 seconds. Run `tmux ls` in another terminal and restart tmux if it hangs. The phone commands assume the session is named `octet`. |
| `[!!] tmux reduces colours. Add to ~/.tmux.conf: …` | Add step 2's colour lines, then `tmux source-file ~/.tmux.conf`. |
| `[ok] Tailscale connected: NAME (100.x.y.z)` | The phone connects to `NAME`, or to the address if it can't resolve the name. |
| `[!!] Tailscale isn't connected. …` | Tailscale is missing, signed out or stopped. Open the app and sign in, or run `tailscale up` (step 3). There's no SSH line until this passes. |
| `[ok] Tailscale SSH is on` | Option B is ready. |
| `[ok] SSH answers on the tailnet address` | Option A (Remote Login) is ready. |
| `[!!] SSH doesn't answer on the tailnet. Turn on Remote Login …` | Do step 4. The App Store app gets the Remote Login advice only; other installs also get `tailscale set --ssh`. |
| `[ok] mosh-server 1.4.0` | mosh is ready. |
| `[!!] mosh-server X.Y.Z is too old for 24-bit colour; …` | `brew upgrade mosh`. |
| `[!!] mosh-server isn't installed: brew install mosh` | Do step 1. The same line appears if `mosh-server` isn't on Octet's PATH. |
| `Phone (Blink): …` and `Phone (Termius): …` | Copy these into the phone app (step 6). When Octet isn't in tmux they follow `After restarting Octet in tmux:`, because until then they would open a new, empty session. |
| `Setup guide: docs/remote-control.md` | Always the last line. |

### What it can't check

It sees only the Mac. If every line is `[ok]` and the phone still fails, the
cause is one of these (see [Troubleshooting](#troubleshooting)):

- Tailscale on the phone: connected, and signed in to the same account;
- whether the phone can resolve the Mac's machine name (MagicDNS);
- the phone app's host, user name, key and Mosh setting;
- whether `mosh-server` and `tmux` are on the PATH of an SSH login. The
  check uses Octet's own PATH, which comes from your interactive shell;
- the `ssh` rules in the Tailscale admin console;
- whether the Mac will stay awake (step 7);
- whether the phone app shows the bell while it's in the background.

### What it never does

It only runs the commands in the table above. It changes no system, tmux or
Tailscale settings, starts no listener, and holds no keys or passwords. Its
one connection goes to the Mac's own address and closes at once.

## Troubleshooting

Start with `/remote-control` on the Mac. Then find the symptom here.

| Symptom on the phone | Cause and fix |
| --- | --- |
| Termius: address resolution failed | The phone isn't using Tailscale's DNS. Use the Mac's `100.x.y.z` address from the report (or `tailscale ip -4` on the Mac) as the host. Or, in the phone's Tailscale app, turn on **Use Tailscale DNS** and check that MagicDNS is on in the admin console's DNS page. |
| Connection refused | Nothing serves SSH on the Mac. Option B: run `sudo tailscale set --ssh`. Option A: turn on Remote Login. `/remote-control` should then show an `[ok]` SSH line. |
| Connection times out | Tailscale is off on the phone, or signed in to a different account. Both devices should appear in `tailscale status` on the Mac. |
| Permission denied (publickey) | Option A: the phone's public key isn't in `~/.ssh/authorized_keys` on the Mac, or the host uses a different key or user name. |
| Tailscale asks you to sign in, or denies access | Option B: the admin console's `ssh` rules don't allow this device and user. With `"action": "check"`, sign in again every 12 hours. |
| `mosh-server: command not found`, or Mosh fails while plain SSH works | Homebrew isn't on the PATH for SSH logins. Add the `~/.zshenv` line from step 1. |
| `tmux: command not found` from the startup snippet | Same cause and fix as above. |
| A plain shell instead of Octet | The startup snippet didn't run. Type `tmux new -A -s octet`. If that opens an empty session, Octet is in a session with another name: use the name from `/remote-control`, or list them with `tmux ls`. |
| 256 colours instead of full colour | Check the report for `tmux reduces colours` or an old mosh-server. After changing `~/.tmux.conf`, reload it and reattach. |
| Octet is gone | The Mac slept (step 7), or Octet was quit. Start it again with step 5's line. |
| `/remote-control` shows `[!!] SSH doesn't answer` but the phone connects with Tailscale SSH | An older Octet build only tested port 22, which Tailscale SSH refuses from the Mac itself. Rebuild (below). |

After pulling Octet changes, run `make rust-build`, then quit Octet (Ctrl+C
twice) and start it again with step 5's line; the running copy keeps the old
code until then.

## Everyday use

| To | Do |
| --- | --- |
| Send a newline | **Ctrl+J** (phone keyboards rarely send Alt+Enter) |
| Cancel a turn | **Esc** from the app's key bar |
| Answer an approval | **A** to allow once, **D** to deny |
| Change the permission mode | `/mode auto` (Shift+Tab if your app sends it) |
| Help | `/help` (F1 isn't on phone keyboards) |
| Scroll | PgUp/PgDn from the key bar |
| Leave Octet running | Close the app, or press **Ctrl+B** then **D** to detach |
| Quit Octet | **Ctrl+C** twice; this ends the agent's session |

When an approval starts waiting, Octet rings the terminal bell; tmux flags
it, and the bell reaches your phone app over mosh. Whether the phone shows it
while the app is in the background depends on the app. The desk and the
phone can be attached to the same session at once. By default (tmux 3.1 and
later) the screen takes the size of whichever of them was used last; add
`set -g window-size smallest` to `~/.tmux.conf` to fit both instead.

## Turn it off

- Quit Octet with Ctrl+C twice, or `tmux kill-session -t octet`.
- Turn off **Remote Login** in System Settings, or run `sudo tailscale set --ssh=false`.
- Disconnect the Mac in the Tailscale app.

## Security notes

- Anyone who can log in over SSH can run anything as your user, including
  switching Octet to `full-access`. Keep login to your own key (Option A) or
  your own devices (Option B).
- Never forward port 22 on your router; Tailscale makes that unnecessary.
- The approval window still denies when it runs out. Prefer `ask` mode and
  check in, or `auto` to let the vendor's reviewer decide.
