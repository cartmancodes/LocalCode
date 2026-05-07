import type { ChatTurn } from "../types";

export default function MessageBubble({ turn }: { turn: ChatTurn }) {
  return (
    <div className={`bubble ${turn.role}`}>
      <div className="bubble-role">{turn.role === "user" ? "You" : "Assistant"}</div>
      {turn.blocks.map((b, i) => {
        if (b.kind === "text") {
          return (
            <pre key={i} className="block text-block">
              {b.text}
            </pre>
          );
        }
        if (b.kind === "tool_use") {
          return (
            <div key={i} className="block tool-use">
              <div className="tool-head">
                <span className="tool-icon">⚙</span>
                <span className="tool-name">{b.toolName}</span>
              </div>
              {b.toolInput && (
                <pre className="tool-input">{JSON.stringify(b.toolInput, null, 2)}</pre>
              )}
            </div>
          );
        }
        if (b.kind === "tool_result") {
          return (
            <div key={i} className={`block tool-result ${b.isError ? "is-error" : ""}`}>
              <div className="tool-head">
                <span className="tool-icon">{b.isError ? "✗" : "✓"}</span>
                <span className="tool-name">result</span>
              </div>
              <pre className="tool-output">
                {typeof b.toolOutput === "string"
                  ? b.toolOutput
                  : JSON.stringify(b.toolOutput, null, 2)}
              </pre>
            </div>
          );
        }
        return null;
      })}
      {turn.role === "assistant" && turn.costUsd != null && (
        <div className="bubble-foot muted">
          ${turn.costUsd.toFixed(4)}
          {turn.durationMs ? ` · ${(turn.durationMs / 1000).toFixed(1)}s` : ""}
        </div>
      )}
      {turn.inProgress && <div className="bubble-foot muted">…working</div>}
    </div>
  );
}
