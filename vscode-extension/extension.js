"use strict";

const crypto = require("crypto");
const vscode = require("vscode");

const EXTENSION_ID = "localcode";
const VIEW_ID = "localcode.chat";
const PANEL_VIEW_TYPE = "localcode.panel";
const PANEL_TITLE = "LocalCode";

const DEFAULT_FRONTEND_URL = "http://localhost:5173";
const DEFAULT_BACKEND_PORT = 8080;
const OPENCODE_PORT = 4096;

const CONFIG_KEYS = Object.freeze({
  url: "url",
  backendPort: "backendPort",
  openOnStartup: "openOnStartup",
});

let activeExtension;

/**
 * Sidebar webview provider. It delegates all rendering decisions to the owner
 * so the panel and sidebar always use identical HTML and port mappings.
 */
class LocalCodeViewProvider {
  constructor(renderWebview) {
    this.renderWebview = renderWebview;
    this.view = undefined;
  }

  resolveWebviewView(webviewView) {
    this.view = webviewView;
    this.renderWebview(webviewView.webview);
  }

  reload() {
    if (this.view) {
      this.renderWebview(this.view.webview);
    }
  }

  dispose() {
    this.view = undefined;
  }
}

class LocalCodeExtension {
  constructor(context) {
    this.context = context;
    this.panel = undefined;
    this.viewProvider = new LocalCodeViewProvider((webview) => this.renderWebview(webview));
  }

  activate() {
    this.context.subscriptions.push(
      vscode.window.registerWebviewViewProvider(VIEW_ID, this.viewProvider, {
        webviewOptions: { retainContextWhenHidden: true },
      }),
      vscode.commands.registerCommand("localcode.open", () => this.openPanel()),
      vscode.commands.registerCommand("localcode.openSidebar", () => this.focusSidebar()),
      vscode.commands.registerCommand("localcode.reload", () => this.reloadAll()),
      vscode.workspace.onDidChangeConfiguration((event) => this.onConfigurationChanged(event)),
      { dispose: () => this.dispose() },
    );

    if (readConfiguration().openOnStartup) {
      this.openPanel();
    }
  }

  openPanel() {
    if (this.panel) {
      this.panel.reveal(vscode.ViewColumn.Beside, false);
      return;
    }

    this.panel = vscode.window.createWebviewPanel(
      PANEL_VIEW_TYPE,
      PANEL_TITLE,
      { viewColumn: vscode.ViewColumn.Beside, preserveFocus: false },
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        portMapping: buildPortMappings(),
      },
    );

    this.renderWebview(this.panel.webview);
    this.panel.onDidDispose(
      () => {
        this.panel = undefined;
      },
      undefined,
      this.context.subscriptions,
    );
  }

  focusSidebar() {
    vscode.commands.executeCommand(`${VIEW_ID}.focus`);
  }

  reloadAll() {
    if (this.panel) {
      this.renderWebview(this.panel.webview);
    }
    this.viewProvider.reload();
  }

  renderWebview(webview) {
    webview.options = {
      enableScripts: true,
      portMapping: buildPortMappings(),
    };
    webview.html = buildHtml(readConfiguration().frontendUrl);
  }

  onConfigurationChanged(event) {
    if (
      event.affectsConfiguration(`${EXTENSION_ID}.${CONFIG_KEYS.url}`) ||
      event.affectsConfiguration(`${EXTENSION_ID}.${CONFIG_KEYS.backendPort}`)
    ) {
      this.reloadAll();
    }
  }

  dispose() {
    if (this.panel) {
      this.panel.dispose();
      this.panel = undefined;
    }
    this.viewProvider.dispose();
  }
}

function activate(context) {
  activeExtension = new LocalCodeExtension(context);
  activeExtension.activate();
}

function deactivate() {
  if (activeExtension) {
    activeExtension.dispose();
    activeExtension = undefined;
  }
}

function readConfiguration() {
  const config = vscode.workspace.getConfiguration(EXTENSION_ID);
  const rawUrl = config.get(CONFIG_KEYS.url, DEFAULT_FRONTEND_URL);
  const rawBackendPort = config.get(CONFIG_KEYS.backendPort, DEFAULT_BACKEND_PORT);

  return {
    frontendUrl: normalizeFrontendUrl(rawUrl),
    backendPort: normalizePort(rawBackendPort, DEFAULT_BACKEND_PORT),
    openOnStartup: config.get(CONFIG_KEYS.openOnStartup, false) === true,
  };
}

function normalizeFrontendUrl(value) {
  const candidate = typeof value === "string" && value.trim() ? value.trim() : DEFAULT_FRONTEND_URL;

  try {
    const parsed = new URL(candidate);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return parsed.toString();
    }
  } catch (_) {
    // Fall through to the default.
  }

  return DEFAULT_FRONTEND_URL;
}

function normalizePort(value, fallback) {
  const port = Number(value);
  if (Number.isInteger(port) && port > 0 && port <= 65535) {
    return port;
  }
  return fallback;
}

function buildPortMappings() {
  const { frontendUrl, backendPort } = readConfiguration();
  const frontendPort = getUrlPort(frontendUrl) || 5173;
  const ports = new Set([frontendPort, backendPort, OPENCODE_PORT]);

  return Array.from(ports, (port) => ({
    webviewPort: port,
    extensionHostPort: port,
  }));
}

function getUrlPort(value) {
  try {
    const parsed = new URL(value);
    if (parsed.port) {
      return normalizePort(parsed.port, undefined);
    }
    return parsed.protocol === "https:" ? 443 : 80;
  } catch (_) {
    return undefined;
  }
}

function buildHtml(frontendUrl) {
  const nonce = crypto.randomBytes(16).toString("base64");
  const frontendOrigin = getOrigin(frontendUrl);
  const frameSources = [frontendOrigin, "http://localhost:*", "http://127.0.0.1:*"]
    .filter(Boolean)
    .join(" ");
  const csp = [
    "default-src 'none'",
    `frame-src ${frameSources}`,
    `style-src 'nonce-${nonce}'`,
  ].join("; ");

  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="Content-Security-Policy" content="${escapeAttribute(csp)}">
  <title>LocalCode</title>
  <style nonce="${nonce}">
    html,
    body {
      width: 100vw;
      height: 100vh;
      margin: 0;
      padding: 0;
      overflow: hidden;
      background: #1e1e1e;
    }

    iframe {
      display: block;
      width: 100%;
      height: 100%;
      border: 0;
    }
  </style>
</head>
<body>
  <iframe src="${escapeAttribute(frontendUrl)}" title="LocalCode" allow="clipboard-read; clipboard-write; web-share"></iframe>
</body>
</html>`;
}

function getOrigin(value) {
  try {
    return new URL(value).origin;
  } catch (_) {
    return new URL(DEFAULT_FRONTEND_URL).origin;
  }
}

function escapeAttribute(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

module.exports = { activate, deactivate };
