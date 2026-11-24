import { Component, type ErrorInfo, type ReactNode } from "react";

interface State {
  error: Error | null;
}

/** Last-resort UI guard: a render bug shows a recoverable message instead of a blank screen. */
export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // In Azure this is picked up by the App Insights JS SDK (trackException) when configured.
    console.error("ui_render_error", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="pad">
          <h3>Something went wrong in the interface.</h3>
          <p className="muted">{this.state.error.message}</p>
          <button onClick={() => this.setState({ error: null })}>Try again</button>
        </div>
      );
    }
    return this.props.children;
  }
}
