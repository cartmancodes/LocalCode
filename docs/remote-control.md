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
what is missing (`[!!]`) and the command to fix it. Why this design, and its
limits: [the evaluation](rust/remote-control-ssh.md).

## 1. Install tmux and mosh on the Mac

```sh
brew install tmux mosh
mosh-server --version
```

The version must be 1.4.0 or newer; older mosh shows Octet in 256 colours.

## 2. Configure tmux

Create `~/.tmux.conf`, or add these lines to it:

```
# Octet from a phone over mosh: 24-bit colour, and flag bells (approvals)
# from windows you aren't looking at.
set -g default-terminal "tmux-256color"
set -as terminal-features ",xterm-256color:RGB"
set -g monitor-bell on
set -g bell-action any
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
tailscale up
tailscale set --ssh
```

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

Type `/remote-control`. When the Mac is ready, every line shows `[ok]`, and
the report ends with the exact commands for your phone:

```
Remote control setup (read-only check)
[ok] Running in tmux session "octet"
[ok] Tailscale connected: my-mac.tail1234.ts.net (100.101.102.103)
[ok] SSH answers on the tailnet address
[ok] mosh-server 1.4.0
Phone (Blink):   mosh you@my-mac.tail1234.ts.net -- tmux new -A -s octet
Phone (Termius): host my-mac.tail1234.ts.net, user you, Mosh on, startup: tmux new -A -s octet
Setup guide: docs/remote-control.md
```

| Line shows | Fix |
| --- | --- |
| `[!!] Not inside tmux` | Quit Octet (Ctrl+C twice) and start it with the `tmux new -A -s octet …` line above. |
| `[!!] Inside tmux, but tmux didn't answer` | Run `tmux ls` in another terminal; restart tmux if it hangs. |
| `[!!] tmux reduces colours` | Add step 2's colour lines and reload tmux. |
| `[!!] Tailscale isn't connected` | Open the Tailscale app and sign in (step 3). |
| `[!!] SSH doesn't answer on the tailnet` | Do step 4. |
| `[!!] mosh-server isn't installed` or `is too old` | Do step 1. |

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
   Mac user name, and the key. Turn on **Mosh**.
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
phone can be attached to the same session at once; tmux sizes the screen to
the smaller of the two.

## Turn it off

- Quit Octet with Ctrl+C twice, or `tmux kill-session -t octet`.
- Turn off **Remote Login** in System Settings, or run `tailscale set --ssh=false`.
- Disconnect the Mac in the Tailscale app.

## Security notes

- Anyone who can log in over SSH can run anything as your user, including
  switching Octet to `full-access`. Keep login to your own key (Option A) or
  your own devices (Option B).
- Never forward port 22 on your router; Tailscale makes that unnecessary.
- The approval window still denies when it runs out. Prefer `ask` mode and
  check in, or `auto` to let the vendor's reviewer decide.
