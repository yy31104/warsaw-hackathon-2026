import {Component, type ReactNode} from 'react';
import {Button} from '@mantine/core';

type Props = {
  children: ReactNode;
  onRetry?: () => void;
};

export class PageErrorBoundary extends Component<Props, {failed: boolean}> {
  state = {failed: false};

  static getDerivedStateFromError() {
    return {failed: true};
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return <section className="panel" role="alert">
      <h2>This page couldn’t load</h2>
      <Button variant="default" onClick={this.props.onRetry ?? (() => this.setState({failed: false}))}>
        Try again
      </Button>
    </section>;
  }
}
