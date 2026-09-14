/**
 * CoreClient — a typed WebSocket client for the core RPC protocol.
 *
 * Every command returns a promise that settles when its `response` arrives,
 * so callers write `await client.send({type: "get_state"})` instead of
 * correlating ids by hand. Events and UI requests are delivered to handlers.
 *
 * Reconnection is deliberately *not* automatic mid-session: a core session is
 * owned by the socket, so a dropped connection means a new session. Surfacing
 * that is honest; silently reconnecting into a different session would not be.
 */

import type {
  Command,
  ConnectOptions,
  Inbound,
  Response,
  ServerEvent,
  UIRequest,
  UIResponse,
} from "./protocol";
import { connectionUrl } from "./protocol";

export type ConnectionState = "connecting" | "open" | "closed" | "failed";

export interface CoreClientHandlers {
  onEvent?: (event: ServerEvent) => void;
  onUIRequest?: (request: UIRequest) => void;
  onState?: (state: ConnectionState, detail?: string) => void;
}

const COMMAND_TIMEOUT_MS = 120_000;

export class CoreClient {
  private socket: WebSocket | null = null;
  private nextId = 1;
  private pending = new Map<string, { resolve: (r: Response) => void; reject: (e: Error) => void; timer: number }>();
  private handlers: CoreClientHandlers;
  private closedByUs = false;

  constructor(handlers: CoreClientHandlers = {}) {
    this.handlers = handlers;
  }

  get connected(): boolean {
    return this.socket?.readyState === WebSocket.OPEN;
  }

  connect(options: ConnectOptions): void {
    this.disconnect();
    this.closedByUs = false;
    this.handlers.onState?.("connecting");
    const socket = new WebSocket(connectionUrl(options));
    this.socket = socket;

    socket.onopen = () => this.handlers.onState?.("open");
    socket.onmessage = (raw) => {
      let frame: Inbound;
      try {
        frame = JSON.parse(raw.data as string) as Inbound;
      } catch {
        return; // a malformed frame is not worth tearing the session down for
      }
      this.route(frame);
    };
    socket.onerror = () => this.handlers.onState?.("failed", "connection error");
    socket.onclose = (event) => {
      this.failPending(new Error(event.reason || "connection closed"));
      if (this.socket === socket) this.socket = null;
      this.handlers.onState?.(this.closedByUs ? "closed" : "failed", event.reason || undefined);
    };
  }

  disconnect(): void {
    this.closedByUs = true;
    this.failPending(new Error("disconnected"));
    const socket = this.socket;
    this.socket = null;
    if (socket && socket.readyState <= WebSocket.OPEN) socket.close(1000, "client closed");
  }

  /** Send a command and wait for its response. Rejects on a failed response. */
  send<T = unknown>(command: Command): Promise<T> {
    const socket = this.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return Promise.reject(new Error("not connected"));
    }
    const id = String(this.nextId++);
    return new Promise<T>((resolve, reject) => {
      const timer = window.setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`${command.type} timed out`));
      }, COMMAND_TIMEOUT_MS);
      this.pending.set(id, {
        timer,
        reject,
        resolve: (response) => {
          if (response.success) resolve(response.data as T);
          else reject(new Error(response.error || `${command.type} failed`));
        },
      });
      socket.send(JSON.stringify({ ...command, id }));
    });
  }

  /** Answer a blocking `extension_ui_request`. */
  respondToUI(response: UIResponse): void {
    this.socket?.send(JSON.stringify(response));
  }

  private route(frame: Inbound): void {
    if (frame.type === "response") {
      const id = frame.id;
      const entry = id ? this.pending.get(id) : undefined;
      if (entry && id) {
        this.pending.delete(id);
        window.clearTimeout(entry.timer);
        entry.resolve(frame);
      }
      return;
    }
    if (frame.type === "extension_ui_request") {
      this.handlers.onUIRequest?.(frame);
      return;
    }
    this.handlers.onEvent?.(frame);
  }

  private failPending(error: Error): void {
    for (const [, entry] of this.pending) {
      window.clearTimeout(entry.timer);
      entry.reject(error);
    }
    this.pending.clear();
  }
}
