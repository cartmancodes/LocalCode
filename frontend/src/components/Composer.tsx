import { KeyboardEvent, useState } from "react";

interface Props {
  disabled: boolean;
  onSend: (text: string) => void;
}

export default function Composer({ disabled, onSend }: Props) {
  const [text, setText] = useState("");

  const submit = () => {
    const t = text.trim();
    if (!t) return;
    onSend(t);
    setText("");
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="composer">
      <textarea
        value={text}
        placeholder={disabled ? "Pick or create a chat to start…" : "Ask anything. ⌘+↵ to send."}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKey}
        disabled={disabled}
        rows={3}
      />
      <button onClick={submit} disabled={disabled || !text.trim()} className="primary">
        Send
      </button>
    </div>
  );
}
