import { Component, ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
  /** Optional label so the same boundary can be reused around different sections. */
  label?: string;
}

interface State {
  error: Error | null;
  componentStack: string;
}

/**
 * Catches React render errors and shows the message + stack inline instead of
 * white-screening the whole app. Cheap, no dependencies. Wrap any subtree
 * that you suspect of crashing — particularly modals and async-data views.
 */
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: "" };

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Log to the console so the dev tools still get the original stack.
    // eslint-disable-next-line no-console
    console.error(`[ErrorBoundary${this.props.label ? `:${this.props.label}` : ""}]`, error, info);
    this.setState({ componentStack: info.componentStack ?? "" });
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div
        style={{
          padding: 20,
          margin: 16,
          background: "var(--code-bg)",
          color: "var(--status-err)",
          border: "1px solid var(--status-err)",
          borderRadius: 10,
          fontFamily: "Geist Mono, monospace",
          fontSize: 12,
          whiteSpace: "pre-wrap",
          overflow: "auto",
          maxHeight: "60vh",
        }}
      >
        <strong style={{ fontFamily: "Geist, sans-serif", fontSize: 14 }}>
          UI error{this.props.label ? ` in ${this.props.label}` : ""}
        </strong>
        {"\n\n"}
        {String(this.state.error.message || this.state.error)}
        {"\n\n"}
        {this.state.error.stack}
        {this.state.componentStack && "\n\n"}
        {this.state.componentStack}
      </div>
    );
  }
}
