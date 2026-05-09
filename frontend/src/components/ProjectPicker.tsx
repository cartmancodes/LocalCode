import { useEffect, useRef, useState } from "react";
import { IconPlus, IconX } from "./icons";

interface Props {
  /** Primary working directory the agent is "rooted" in. */
  cwd: string | null;
  /** Backend's process cwd — used as the implied default when `cwd` is null. */
  defaultCwd: string | null;
  /** Extra absolute paths the agent's tools may also operate on. */
  additionalDirs: string[];
  onChange: (cwd: string | null, additionalDirs: string[]) => void;
}

/**
 * Topbar pill: shows the active project root + a count of additional grant
 * paths. Click to open a popover that edits both:
 *   - Primary cwd input (text)
 *   - Additional-dirs list (add / remove)
 *
 * The popover holds local draft state so the user can edit freely and then
 * commit with Save (or discard with Cancel / Esc / click-outside).
 */
export default function ProjectPicker({ cwd, defaultCwd, additionalDirs, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const [draftCwd, setDraftCwd] = useState<string>(cwd ?? defaultCwd ?? "");
  const [draftDirs, setDraftDirs] = useState<string[]>(additionalDirs);
  const [newDir, setNewDir] = useState("");
  const ref = useRef<HTMLDivElement | null>(null);

  // Sync draft when external state changes (e.g. on first load).
  useEffect(() => {
    if (!open) {
      setDraftCwd(cwd ?? defaultCwd ?? "");
      setDraftDirs(additionalDirs);
    }
  }, [cwd, defaultCwd, additionalDirs, open]);

  // Click-outside closes the popover.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        commit();
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, draftCwd, draftDirs]);

  const display = cwd ?? defaultCwd ?? "—";
  const short = display.replace(/^\/Users\/[^/]+/, "~");

  const commit = () => {
    const trimmed = draftCwd.trim();
    const cleaned = draftDirs.map((d) => d.trim()).filter(Boolean);
    const nextCwd = trimmed.length === 0 || trimmed === defaultCwd ? null : trimmed;
    // Dedupe the additional-dirs list, drop the primary cwd if duplicated.
    const dedup: string[] = [];
    const primary = nextCwd ?? defaultCwd;
    for (const d of cleaned) {
      if (d === primary) continue;
      if (!dedup.includes(d)) dedup.push(d);
    }
    onChange(nextCwd, dedup);
    setOpen(false);
  };

  const cancel = () => {
    setDraftCwd(cwd ?? defaultCwd ?? "");
    setDraftDirs(additionalDirs);
    setNewDir("");
    setOpen(false);
  };

  const addDir = () => {
    const v = newDir.trim();
    if (!v) return;
    if (draftDirs.includes(v)) {
      setNewDir("");
      return;
    }
    setDraftDirs((cur) => [...cur, v]);
    setNewDir("");
  };

  const removeDir = (path: string) => {
    setDraftDirs((cur) => cur.filter((d) => d !== path));
  };

  return (
    <div className="lc-project" ref={ref}>
      <button
        className="lc-project__btn"
        onClick={() => setOpen((o) => !o)}
        title={`Project root: ${display}\n${additionalDirs.length} additional dir${additionalDirs.length === 1 ? "" : "s"}\nClick to change`}
      >
        <span className="lc-project__lbl">Project</span>
        <span className="lc-project__path">{short}</span>
        {additionalDirs.length > 0 && (
          <span className="lc-project__badge">+{additionalDirs.length}</span>
        )}
      </button>

      {open && (
        <div className="lc-project__pop" onMouseDown={(e) => e.stopPropagation()}>
          <div className="lc-project__pop-hdr">Working directory for new chats</div>
          <input
            className="lc-project__input"
            value={draftCwd}
            onChange={(e) => setDraftCwd(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") commit();
              if (e.key === "Escape") cancel();
            }}
            placeholder={defaultCwd ?? "/path/to/your/project"}
            autoFocus
            spellCheck={false}
          />
          {defaultCwd && draftCwd.trim() !== defaultCwd && (
            <div className="lc-project__pop-help">
              <button
                type="button"
                className="lc-project__reset"
                onClick={() => setDraftCwd(defaultCwd)}
              >
                reset to default ({defaultCwd.replace(/^\/Users\/[^/]+/, "~")})
              </button>
            </div>
          )}

          <div className="lc-project__pop-hdr" style={{ marginTop: 8 }}>
            Additional directories the agent can access
          </div>
          {draftDirs.length === 0 && (
            <div className="lc-project__pop-help">
              No extras. Useful if you need to reference sibling repos or shared dirs.
            </div>
          )}
          {draftDirs.length > 0 && (
            <div className="lc-project__dirs">
              {draftDirs.map((d) => (
                <div key={d} className="lc-project__dir">
                  <span className="lc-project__dir-path" title={d}>
                    {d}
                  </span>
                  <button
                    className="lc-project__dir-rm"
                    onClick={() => removeDir(d)}
                    title="Remove"
                    aria-label={`Remove ${d}`}
                  >
                    <IconX size={11} />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="lc-project__add">
            <input
              className="lc-project__input"
              value={newDir}
              onChange={(e) => setNewDir(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  addDir();
                }
                if (e.key === "Escape") cancel();
              }}
              placeholder="/another/path/to/grant"
              spellCheck={false}
            />
            <button
              className="lc-project__add-btn"
              onClick={addDir}
              disabled={!newDir.trim()}
              title="Add directory"
            >
              <IconPlus size={12} /> Add
            </button>
          </div>

          <div className="lc-project__pop-help">
            Existing chats keep the directories they were created with.
          </div>
          <div className="lc-project__pop-actions">
            <button className="lc-project__cancel" onClick={cancel}>
              Cancel
            </button>
            <button className="lc-project__apply" onClick={commit}>
              Save
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
