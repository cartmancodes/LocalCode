// LocalCode VS Code extension — embeds the LocalCode frontend in a VS Code
// webview so the chat lives next to your code.
//
// Two surfaces:
//   1. A sidebar view (activity bar → LocalCode → "Chat") — always visible,
//      narrow but persistent. Good while editing.
//   2. A wider panel opened in the editor area beside the current file —
//      better for actively driving the agents.
//
// Both surfaces load the same `localcode.url` (the vite dev server, default
// http://localhost:5173) inside an iframe. `portMapping` lets the webview's
// CORS-restricted context reach the backend WS at :8080 and opencode at :4096.

const vscode = require("vscode");

const VIEW_ID = "localcode.chat";

function getUrl() {
  return (
    vscode.workspace.getConfiguration("localcode").get("url") ||
    "http://localhost:5173"
  );
}

function getBackendPort() {
  return (
    vscode.workspace.getConfiguration("localcode").get("backendPort") || 8080
  );
}

// Webviews can't reach localhost on the host directly — they run in their own
// frame with a synthetic origin. `portMapping` makes the listed ports tunnel
// through to the extension host so iframe `fetch` and WebSocket calls land.
function portMappings() {
  const backend = getBackendPort();
  return [
    { webviewPort: 5173, extensionHostPort: 5173 }, // vite (frontend)
    { webviewPort: backend, extensionHostPort: backend }, // FastAPI
    { webviewPort: 4096, extensionHostPort: 4096 }, // opencode
  ];
}

// CSP for the wrapper page only. The inner iframe is a separate browsing
// context with its own CSP (served by the LocalCode app), so WebSocket and
// fetch directives belong there, not here. We just need to permit the
// iframe load (`frame-src`) and the inline `<style>` block.
function buildHtml(url) {
  const csp = [
    "default-src 'none'",
    "frame-src http://localhost:* http://127.0.0.1:*",
    "style-src 'unsafe-inline'",
  ].join("; ");

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="${csp}">
  <title>LocalCode</title>
  <style>
    html, body { margin: 0; padding: 0; height: 100vh; width: 100vw; overflow: hidden; background: #1e1e1e; }
    iframe { border: 0; height: 100%; width: 100%; display: block; }
  </style>
</head>
<body>
  <iframe src="${url}" allow="clipboard-read; clipboard-write; web-share"></iframe>
</body>
</html>`;
}

class LocalCodeViewProvider {
  constructor(context) {
    this.context = context;
    this.view = null;
  }

  resolveWebviewView(webviewView) {
    this.view = webviewView;
    webviewView.webview.options = {
      enableScripts: true,
      portMapping: portMappings(),
    };
    webviewView.webview.html = buildHtml(getUrl());
  }

  reload() {
    if (this.view) {
      this.view.webview.html = buildHtml(getUrl());
    }
  }
}

let panelRef = null;
let providerRef = null;

function openPanel() {
  // If the panel already exists, just bring it to front instead of stacking
  // duplicates. ViewColumn.Beside opens it next to the active editor, so
  // code stays on the left and LocalCode lands on the right.
  if (panelRef) {
    panelRef.reveal(vscode.ViewColumn.Beside, false);
    return;
  }
  panelRef = vscode.window.createWebviewPanel(
    "localcode.panel",
    "LocalCode",
    { viewColumn: vscode.ViewColumn.Beside, preserveFocus: false },
    {
      enableScripts: true,
      retainContextWhenHidden: true, // keep WS alive when the tab loses focus
      portMapping: portMappings(),
    },
  );
  panelRef.webview.html = buildHtml(getUrl());
  panelRef.onDidDispose(() => {
    panelRef = null;
  });
}

function activate(context) {
  providerRef = new LocalCodeViewProvider(context);

  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider(VIEW_ID, providerRef, {
      webviewOptions: { retainContextWhenHidden: true },
    }),
    vscode.commands.registerCommand("localcode.open", openPanel),
    vscode.commands.registerCommand("localcode.openSidebar", () => {
      vscode.commands.executeCommand(`${VIEW_ID}.focus`);
    }),
    vscode.commands.registerCommand("localcode.reload", () => {
      if (panelRef) panelRef.webview.html = buildHtml(getUrl());
      if (providerRef) providerRef.reload();
    }),
    // If the user changes localcode.url at runtime, refresh both surfaces so
    // they pick up the new URL without a window reload.
    vscode.workspace.onDidChangeConfiguration((ev) => {
      if (
        ev.affectsConfiguration("localcode.url") ||
        ev.affectsConfiguration("localcode.backendPort")
      ) {
        if (panelRef) panelRef.webview.html = buildHtml(getUrl());
        if (providerRef) providerRef.reload();
      }
    }),
  );

  if (
    vscode.workspace.getConfiguration("localcode").get("openOnStartup") === true
  ) {
    openPanel();
  }
}

function deactivate() {
  if (panelRef) panelRef.dispose();
  panelRef = null;
  providerRef = null;
}

module.exports = { activate, deactivate };
