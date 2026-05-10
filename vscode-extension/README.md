# LocalCode VS Code extension

Embeds the LocalCode multi-agent UI inside VS Code so you can drive the chat next to your code.

## Two surfaces

- **Activity bar** — click the LocalCode icon in the activity bar; a sidebar webview shows the chat. Narrow but always visible while you edit.
- **Editor panel** — `Cmd+Shift+P` → `LocalCode: Open Panel (split with current editor)` opens a wider webview *beside* the active editor. Better for actively driving the agents.

## Install

The extension is plain JS — no build step. To install/refresh:

```bash
EXT_DIR=~/.vscode/extensions/localcode-local.localcode-0.1.0
mkdir -p "$EXT_DIR/media"
cp vscode-extension/package.json     "$EXT_DIR/"
cp vscode-extension/extension.js     "$EXT_DIR/"
cp vscode-extension/media/icon.svg   "$EXT_DIR/media/"
# then: reload VS Code window (Cmd+Shift+P → "Developer: Reload Window")
```

Or run from inside the extension folder:

```bash
code --install-extension localcode-0.1.0.vsix   # if you packaged with vsce
```

For development: open the `vscode-extension/` folder in a separate VS Code window and press `F5` to launch an Extension Development Host.

## Settings

| key | default | meaning |
|-----|---------|---------|
| `localcode.url` | `http://localhost:5173` | Vite dev server URL. Change if you run the frontend elsewhere. |
| `localcode.backendPort` | `8080` | FastAPI port; tunneled through `portMapping` so the iframe's WS reaches the runner. |
| `localcode.openOnStartup` | `false` | Auto-open the panel beside the active editor when the window starts. |

## Commands

- `LocalCode: Open Panel (split with current editor)` — opens the editor-area webview beside your code.
- `LocalCode: Focus Sidebar` — focuses the activity bar view.
- `LocalCode: Reload Webview` — re-renders the iframe (also fires automatically when you change `localcode.url`).

## Prerequisites

The extension is a thin webview wrapper — it doesn't start the backend or frontend. Run them yourself first:

```bash
./setup.sh up   # starts backend (8080), opencode (4096), frontend (5173)
```

## Why an iframe?

VS Code webviews run with a sandboxed origin and can't make outbound localhost requests directly. Two pieces unblock the LocalCode UI:

1. The webview HTML wraps an `<iframe src="http://localhost:5173">`. The iframe runs in a normal browser context and can make WebSocket / fetch calls to localhost.
2. `portMapping` declares which extension-host ports the webview is allowed to reach. We map `5173` (frontend), `8080` (backend), `4096` (opencode).

`retainContextWhenHidden: true` is set on both surfaces so an active WS doesn't drop when you click away to a different file.
