export class PrInfo {
  number: number = 0;
  title: string = '';
  state: string = '';
  body: string = '';
  htmlUrl: string = '';
  headSha: string = '';
  headRef: string = '';
  baseRef: string = '';
  updatedAt: string = '';
  author: string = '';
}

export class GateItem {
  context: string = '';
  state: string = '';
  summary: string = '';
  targetUrl: string = '';
  updatedAt: string = '';
  commentUrl: string = '';
}

export class QueueEvent {
  author: string = '';
  createdAt: string = '';
  updatedAt: string = '';
  htmlUrl: string = '';
  summary: string = '';
}

export class PrSummary {
  fetchedAt: string = '';
  pr: PrInfo = new PrInfo();
  gates: Array<GateItem> = [];
  queue: Array<QueueEvent> = [];
  hiddenSuccessContexts: Array<string> = [];
}

export type JsonObject = Record<string, Object>;
