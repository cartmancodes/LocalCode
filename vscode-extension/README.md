# LocalCode VS Code Extension

Embeds the LocalCode multi-agent UI inside VS Code so you can drive the chat next to your code.

## Prerequisites

The extension is a thin webview wrapper. It does not start LocalCode services by itself.

From the repository root, start the LocalCode stack first:

```bash
./setup.sh up
```

This starts:

- Frontend: `http://localhost:5173`
- Backend: `http://localhost:8080`
- OpenCode: `http://localhost:4096`

Check service state with:

```bash
./setup.sh status
```

## Enable In VS Code

Use this path when working from a local checkout. The extension is plain JavaScript and does not need a build step.

1. Copy the extension files into VS Code's local extension directory:

```bash
EXT_DIR=~/.vscode/extensions/localcode-local.localcode-0.1.0
mkdir -p "$EXT_DIR/media"
cp vscode-extension/package.json "$EXT_DIR/"
cp vscode-extension/extension.js "$EXT_DIR/"
cp vscode-extension/media/icon.svg "$EXT_DIR/media/"
```

2. Reload VS Code:

```text
Cmd+Shift+P -> Developer: Reload Window
```

3. Confirm the extension is enabled:

```text
Cmd+Shift+X -> search "LocalCode"
```

4. Open the sidebar:

```text
Activity Bar -> LocalCode icon -> Chat
```

5. Or open the wider editor panel:

```text
Cmd+Shift+P -> LocalCode: Open Panel (split with current editor)
```

## Enable For Development

Use this path when editing the extension itself.

1. Open only the extension folder in a separate VS Code window:

```bash
code vscode-extension
```

2. Press `F5`.

3. VS Code opens an `Extension Development Host` window.

4. In that new window, run:

```text
Cmd+Shift+P -> LocalCode: Open Panel (split with current editor)
```

5. Make changes to `extension.js`, then restart the debug session or run:

```text
Cmd+Shift+P -> Developer: Reload Window
```

## Disable In VS Code

Use this if you want to keep the extension installed but stop it from running.

1. Open Extensions:

```text
Cmd+Shift+X
```

2. Search for `LocalCode`.

3. Click the gear icon next to LocalCode.

4. Choose one of:

- `Disable` — disables it globally for all VS Code windows.
- `Disable (Workspace)` — disables it only for the current workspace.

5. Reload the VS Code window if prompted.

To enable it again, go back to the same Extensions view and click `Enable`.

## Remove Completely

Use this if you installed by copying files into `~/.vscode/extensions`.

1. Close VS Code, or disable the extension first.

2. Remove the copied extension directory:

```bash
rm -rf ~/.vscode/extensions/localcode-local.localcode-0.1.0
```

3. Reopen VS Code or reload the window:

```text
Cmd+Shift+P -> Developer: Reload Window
```

If you installed from a `.vsix`, remove it through the Extensions view:

```text
Cmd+Shift+X -> LocalCode -> Uninstall
```

## Use The Extension

LocalCode exposes two VS Code surfaces:

- **Activity bar sidebar** — click the LocalCode icon in the activity bar. The `Chat` webview is narrow but persistent while you edit.
- **Editor panel** — run `LocalCode: Open Panel (split with current editor)`. This opens a wider LocalCode webview beside the active editor.

Available commands:

- `LocalCode: Open Panel (split with current editor)` — opens or reveals the editor-area webview beside your code.
- `LocalCode: Focus Sidebar` — focuses the LocalCode activity-bar view.
- `LocalCode: Reload Webview` — re-renders the iframe. This also happens automatically when `localcode.url` or `localcode.backendPort` changes.

## Settings

Open settings with:

```text
Cmd+, -> search "LocalCode"
```

Or edit `settings.json` directly:

```json
{
  "localcode.url": "http://localhost:5173",
  "localcode.backendPort": 8080,
  "localcode.openOnStartup": false
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `localcode.url` | `http://localhost:5173` | LocalCode frontend URL. Change this if you run the frontend on another host or port. |
| `localcode.backendPort` | `8080` | FastAPI backend port. The extension maps this through VS Code webview port forwarding for REST and WebSocket traffic. |
| `localcode.openOnStartup` | `false` | When `true`, opens the editor panel automatically when the extension activates. |

## Why An Iframe?

VS Code webviews run with a sandboxed origin and cannot freely access localhost services. LocalCode uses two pieces to make the UI work inside VS Code:

1. The webview HTML wraps the frontend in an iframe, usually `http://localhost:5173`.
2. `portMapping` maps the LocalCode ports through the extension host: frontend, backend, and OpenCode.

`retainContextWhenHidden: true` is set on both the sidebar and panel so an active chat connection is not dropped when you switch files.
