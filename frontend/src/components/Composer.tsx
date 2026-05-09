import { KeyboardEvent, useEffect, useRef, useState } from "react";
import type { SessionRow } from "../types";
import { IconAttach, IconBolt, IconChevD, IconSend, IconSlash, IconSparkle } from "./icons";

interface Props {
  session: SessionRow | null;
  disabled: boolean;
  onSend: (text: string) => void;
}

export default function Composer({ session, disabled, onSend }: Props) {
  const [text, setText] = useState("");
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  // Autoresize the textarea up to 220px.
  useEffect(() => {
    const ta = taRef.current;
    if (!ta) return;
    ta.style.height = "0px";
    ta.style.height = Math.min(ta.scrollHeight, 220) + "px";
  }, [text]);

  const submit = () => {
    const t = text.trim();
    if (!t) return;
    onSend(t);
    setText("");
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // ⌘↵ or Ctrl+↵ — send. Plain ↵ inserts a newline (matches Claude web).
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit();
    }
  };

  const ready = !disabled && text.trim().length > 0;
  const placeholder = !session
    ? "Pick or create a chat to start…"
    : `Message ${session.provider}:${session.model}…`;

  return (
    <div className="lc-composer">
      <div className="lc-composer__contextbar">
        <button className="lc-chip lc-chip--ctx" disabled>
          <IconAttach size={12} /> Add context
        </button>
        <button className="lc-chip lc-chip--ctx" disabled>
          <IconSlash size={12} /> Slash commands
        </button>
        <button className="lc-chip lc-chip--ctx" disabled>
          <IconBolt size={12} /> Run mode: auto
        </button>
        <span className="lc-composer__hint">⌘↵ to send · Enter for newline</span>
      </div>

      <div className="lc-composer__field">
        <textarea
          ref={taRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKey}
          placeholder={placeholder}
          rows={1}
          disabled={disabled && !session}
        />

        <div className="lc-composer__row">
          <div className="lc-composer__left">
            {session && (
              <button className="lc-chip" title={`${session.provider}:${session.model}`}>
                <span
                  className="lc-chip__dot"
                  style={{ background: "var(--accent)" }}
                />
                {session.model}
                <IconChevD size={10} />
              </button>
            )}
            <button className="lc-chip" disabled>
              <IconSparkle size={12} /> Auto-route
            </button>
          </div>
          <div className="lc-composer__right">
            <span className="lc-tokencount">{text.length} chars</span>
            <button
              className={`lc-send ${ready ? "is-ready" : ""}`}
              disabled={!ready}
              onClick={submit}
            >
              <IconSend size={14} />
              <span>Send</span>
              <kbd className="lc-kbd lc-kbd--inv">⌘↵</kbd>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
