import { Component, type ErrorInfo, type ReactNode } from "react";
import { ErrorState } from "./ErrorState";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
}

/**
 * Top-level error boundary wrapping the whole application tree.
 *
 * Without it, any uncaught render error anywhere in the tree unmounts the root
 * and leaves a blank white screen. This catches that case and shows a recover
 * able full-viewport fallback with a reload action, so an operator is never
 * stranded on a blank page. Scoped boundaries (e.g. the evidence panel) still
 * handle their own local failures first; this is the last line of defense.
 */
export class AppErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false };
  }

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Log for diagnostics; a real deployment can forward this to an error sink.
    console.error("[App] Uncaught render error:", error, info);
  }

  private readonly handleReload = () => {
    // A full reload is the safest recovery from an unknown render error: it
    // rebuilds the entire React tree and re-runs session restoration.
    window.location.reload();
  };

  render() {
    if (!this.state.hasError) {
      return this.props.children;
    }
    return (
      <div
        style={{
          minHeight: "100vh",
          display: "grid",
          placeItems: "center",
          padding: "var(--space-6)",
        }}
      >
        <ErrorState
          title="The console hit an unexpected error"
          body="Something went wrong while rendering the page. Reloading usually fixes it."
          action={
            <button type="button" className="btn btn-primary" onClick={this.handleReload}>
              Reload
            </button>
          }
        />
      </div>
    );
  }
}
