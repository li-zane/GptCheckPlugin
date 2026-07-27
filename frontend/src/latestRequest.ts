export type LatestRequestHandle = {
  isCurrent: () => boolean;
  finish: () => boolean;
};

export class LatestRequestCoordinator {
  private sequence = 0;
  private foregroundRequests = new Set<number>();

  beginForeground(): LatestRequestHandle {
    return this.begin(true);
  }

  beginBackground(): LatestRequestHandle | null {
    if (this.foregroundRequests.size > 0) return null;
    return this.begin(false);
  }

  invalidate(): void {
    this.sequence += 1;
    this.foregroundRequests.clear();
  }

  private begin(foreground: boolean): LatestRequestHandle {
    const requestSequence = ++this.sequence;
    if (foreground) this.foregroundRequests.add(requestSequence);
    let finished = false;

    return {
      isCurrent: () => requestSequence === this.sequence,
      finish: () => {
        if (!finished) {
          finished = true;
          if (foreground) this.foregroundRequests.delete(requestSequence);
        }
        return requestSequence === this.sequence;
      },
    };
  }
}
