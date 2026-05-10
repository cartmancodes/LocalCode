# VS Code Integration

LocalCode ships a thin VS Code extension that runs the chat UI inside a webview, so you can drive the agents next to your code instead of context-switching to a browser tab. The extension itself is ~150 lines of plain JS — it doesn't run the backend, mint events, or talk to providers; it's a viewer that wraps the existing vite dev server in an iframe.

The extension lives in [vscode-extension/](../vscode-extension/) at the repo root. The on-disk install location is `~/.vscode/extensions/localcode-local.localcode-0.1.0/` — copy the source files there to install, and `Developer: Reload Window` to pick up changes.

---

## Surfaces

The extension exposes the LocalCode UI in two places. Pick whichever matches your screen layout.

| Surface | Width | When to use |
|---------|-------|-------------|
| **Activity bar → LocalCode** (sidebar webview) | narrow (~300–500 px) | Always-visible chat that lives next to the file explorer. Good for occasional prompts while editing. |
| **Editor panel** (`LocalCode: Open Panel`) | wide (split editor group) | Drive the agents seriously. Code on one side, chat on the other. |

Both share state — they hit the same backend session over the same WS, and the runner architecture means the two views see the same live event stream (subscribers are independent, so two views are no different to the backend than one tab and one phone hitting the same session).

---

## Install

The extension is plain JS — there is no `npm install`, no `tsc`, no bundler.

```bash
EXT_DIR=~/.vscode/extensions/localcode-local.localcode-0.1.0
mkdir -p "$EXT_DIR/media"
cp vscode-extension/package.json     "$EXT_DIR/"
cp vscode-extension/extension.js     "$EXT_DIR/"
cp vscode-extension/media/icon.svg   "$EXT_DIR/media/"
```

Then in VS Code: `Cmd+Shift+P` → `Developer: Reload Window`.

After the reload you should see a new icon in the activity bar. The view name in the command palette is `LocalCode`.

### Updating after edits

Re-run the same `cp` commands and reload the window. Or while developing, open `vscode-extension/` as its own folder and press `F5` to launch an Extension Development Host (a sandboxed VS Code window with the extension loaded — edits hot-reload on save in some scenarios; reload the host window to be safe).

### Uninstall

```bash
rm -rf ~/.vscode/extensions/localcode-local.localcode-0.1.0
```

Then reload the window.

---

## Prerequisites

The extension is a viewer. It does not start anything. Bring up the LocalCode stack first:

```bash
./setup.sh up    # backend :8080, opencode :4096, frontend :5173
```

If the frontend isn't running, the webview shows a blank page (or vite's default "Cannot GET /" depending on whether port 5173 has anything listening). The reload command below re-fetches the iframe.

---

## Commands

All command IDs are namespaced under `localcode.*` and titled under the `LocalCode:` category in the command palette.

| Command | What it does |
|---------|---|
| `LocalCode: Open Panel (split with current editor)` | Opens a wide webview in `ViewColumn.Beside` — i.e. as a new editor group beside the active file. If the panel already exists, it just reveals the existing one (no duplicate stacking). |
| `LocalCode: Focus Sidebar` | Programmatically focuses the activity-bar view. Same as clicking the icon. |
| `LocalCode: Reload Webview` | Re-renders the iframe in both surfaces. Use this if the frontend wasn't running when you opened the panel and you've now started it. |

The reload command also fires automatically when you change `localcode.url` or `localcode.backendPort` in settings, so you don't have to invoke it manually for config changes.

---

## Settings

| key | default | meaning |
|-----|---------|---------|
| `localcode.url` | `http://localhost:5173` | URL of the vite dev server. Override if you run the frontend on a different port or host. |
| `localcode.backendPort` | `8080` | FastAPI port; tunneled through `portMapping` so the iframe's WebSocket reaches the runner without CORS pain. |
| `localcode.openOnStartup` | `false` | Auto-open the editor panel beside the active editor when the VS Code window starts. Off by default — most users prefer the sidebar. |

Settings changes apply immediately to both surfaces (the extension subscribes to `onDidChangeConfiguration` and refreshes the iframe).

---

## Architecture

The extension does three things and three things only:

1. **Register a sidebar webview view provider** for the `localcode.chat` view ID so the activity bar entry has somewhere to render.
2. **Register the `localcode.open` command** which creates a `WebviewPanel` in `ViewColumn.Beside`.
3. **Inject an iframe** into both surfaces pointing at `localcode.url`.

Everything interesting — chat state, agent dispatch, persistence — happens in the existing FastAPI backend and React frontend. The extension is glue.

### The iframe shim

VS Code webviews run with a synthetic `vscode-webview://...` origin and CORS rules that block direct outbound calls to `localhost:5173` and friends. Two pieces unblock the LocalCode UI:

- **The iframe.** The webview HTML is just `<iframe src="http://localhost:5173">`. The iframe runs in a normal browser context with its own origin (`http://localhost:5173`), and from there WebSocket and fetch calls to other localhost ports work like any browser-tab dev session.
- **`portMapping`.** Even with the iframe, the extension host has to grant permission for the webview to reach those ports. `portMapping` declares the allowed `(webviewPort, extensionHostPort)` pairs; we map 5173 (frontend), the configurable backend port (default 8080), and 4096 (opencode). Without this, even an iframe gets blocked.

```js
portMapping: [
  { webviewPort: 5173, extensionHostPort: 5173 },
  { webviewPort: 8080, extensionHostPort: 8080 },
  { webviewPort: 4096, extensionHostPort: 4096 },
]
```

See [extension.js:30-39](../vscode-extension/extension.js#L30-L39).

### CSP

The wrapper page (the HTML that hosts the iframe) has a strict CSP:

```
default-src 'none';
frame-src http://localhost:* http://127.0.0.1:*;
style-src 'unsafe-inline';
```

`default-src 'none'` denies everything; `frame-src` re-permits the iframe load; `style-src 'unsafe-inline'` allows the wrapper's tiny `<style>` block. There is no `script-src` because the wrapper has no scripts — the iframe runs in its own browsing context with its own CSP (served by vite / FastAPI).

`ws://` and `connect-src` directives are intentionally absent: the wrapper itself never makes WebSocket or fetch calls, and the iframe's connections are governed by *its* CSP, not the wrapper's. Including them here would be cargo-culting.

See [extension.js:53-60](../vscode-extension/extension.js#L53-L60).

### `retainContextWhenHidden`

Both surfaces are created with `retainContextWhenHidden: true`. Without this, switching to another tab/file would unload the webview, which would close the iframe, which would close the WebSocket — and you'd reconnect every time you Cmd-tabbed.

The runner architecture (see [docs/architecture.md](architecture.md)) means a dropped WS doesn't kill an in-flight turn — but it does cost a reconnect round-trip and a replay. `retainContextWhenHidden` keeps the WS alive throughout the VS Code session.

There is a memory cost: a hidden webview keeps its DOM, JS heap, and open connections. For one or two sidebars this is negligible; the dev tools panel itself uses more.

### Singleton panel

`createWebviewPanel` is *not* idempotent — calling it twice gives you two panels. The extension keeps a module-level `panelRef` and reveals the existing panel rather than creating a new one. If you dispose the panel (close its tab), `panelRef` is set back to null on the `onDidDispose` callback so the next invocation creates a fresh one.

See [extension.js:115-130](../vscode-extension/extension.js#L115-L130).

### Why an iframe and not a native React mount

The frontend is already a complete vite + React app at `frontend/`. Mounting it natively in the extension would require:

- A separate webpack/vite config that emits a webview-compatible bundle (no `import.meta.url`, no module workers, etc.).
- Reproducing the dev server's HMR inside the extension (or losing HMR entirely, which is awful for iteration).
- Bundling and shipping the bundle on every change.

The iframe approach gets us all of that for free: vite's dev server handles HMR, the React app runs as a normal browser-tab session, and `Developer: Reload Window` is the only build step.

The cost is a tiny double-frame: VS Code → webview → iframe. For a localhost dev tool this is invisible; for a production extension shipped on the marketplace you'd want to bundle.

---

## Interaction with other features

### Coexistence with `anthropic.claude-code`

VS Code's official Claude Code extension is a sibling, not a parent. Both can run simultaneously. The two never share state — Claude Code spawns its own `claude` subprocess for its own chat surface; LocalCode talks to its own backend, which spawns its own `claude` subprocesses. They don't see each other's sessions.

If both extensions are active, the activity bar shows both icons. Click whichever one you want.

### The runner architecture

The webview is a [WS subscriber](architecture.md) like any other. When you switch sessions inside the LocalCode UI:

1. The frontend tears down the WS for the old session.
2. The runner notices the WS is gone but **keeps the turn running** in the background.
3. The frontend opens a WS to the new session.
4. If you switch back, the frontend reconnects with `?since=<lastEventId>` and the runner replays any events you missed during the gap.

None of this is special to the VS Code embedding — the same flow happens in a browser tab. The webview just inherits it.

### Multiple VS Code windows

Each VS Code window has its own webview process. Open the same project in window A and window B → both get their own iframe → both load `http://localhost:5173`. They're two browser-tab equivalents pointed at the same backend.

If both subscribe to the same session, the runner's broadcast fans out to both. You can open a turn from window A and watch it stream live in window B.

### DevTools

You can open Chromium DevTools on the webview itself: `Cmd+Shift+P` → `Developer: Open Webview Developer Tools`. The DevTools attach to the wrapper, and from there you can inspect `<iframe>` → drill into the actual React app.

For backend issues, `tail -f .run/backend.log` is still the better tool.

---

## Troubleshooting

### "Blank panel" when opening

Almost always: the frontend isn't running. Run `./setup.sh up` and then `LocalCode: Reload Webview`.

If the frontend *is* running but the panel still shows blank:
- Check the webview DevTools (`Developer: Open Webview Developer Tools`) for CSP errors. If the user changed `localcode.url` to a non-localhost URL, the CSP `frame-src http://localhost:*` will block it. Either use a localhost URL, or extend the CSP in [extension.js:55](../vscode-extension/extension.js#L55).
- Check that `portMapping` covers your backend port. Default is 8080; if you customised it, set `localcode.backendPort` to match.

### "WebSocket connection failed" inside the iframe

Means `portMapping` doesn't cover the WS endpoint. Open the webview DevTools → Network tab → look for the failing `ws://localhost:8080/api/sessions/.../ws` request. If you've moved the backend off 8080, update `localcode.backendPort` and reload.

### "Webview is reloading every time I click another file"

`retainContextWhenHidden` should prevent that. If it isn't:
- Confirm both `webviewView.webview.options` and the `WebviewPanel` options include `retainContextWhenHidden: true` ([extension.js:97-104](../vscode-extension/extension.js#L97-L104) for the sidebar; the panel options object [extension.js:120](../vscode-extension/extension.js#L120) sets it).
- Note that `webviewOptions` for `registerWebviewViewProvider` is *separate* from the per-view options set inside `resolveWebviewView`. Both need to be set for the sidebar to retain.

### "I edited extension.js but VS Code still runs the old code"

VS Code doesn't watch `~/.vscode/extensions/`. After re-running the install `cp` commands, you must `Developer: Reload Window` to load the new module.

### "The icon doesn't show in the activity bar"

The activity bar entry is contributed via the `viewsContainers` field in [package.json:31-39](../vscode-extension/package.json#L31-L39). The icon path is `media/icon.svg` *relative to the extension folder*. If you copied `package.json` but forgot the `media/` folder, the entry shows up but is iconless.

---

## Future improvements (not implemented)

A few directions if this gets more attention:

- **Pass the active file path to LocalCode.** The extension can read `vscode.window.activeTextEditor` and post a message to the iframe (via `webview.postMessage`) so the chat can pre-fill `cwd` or quote the current selection. Currently the user types it.
- **Status-bar entry showing the backend's health.** A small `vscode.StatusBarItem` polling `/api/health` would surface "LocalCode: not running" without making the user discover it via a blank panel.
- **Native bundle.** Replace the iframe with a real webview-compatible bundle of the React app for marketplace distribution. Loses HMR; gains offline support and one less network hop.

None of these change the architecture; they're polish.
