import { GateItem, JsonObject, PrInfo, PrSummary, QueueEvent } from './PrModels';
import { GiteaConfig } from './GiteaConfig';

const GATE_CONTEXTS: Array<string> = [
  'protected-file-approval',
  'taichu/codex-pr-review',
  'taichu/codex-pr-test-review',
  'taichu/pr-build',
  'taichu/dev-cloud-preflight',
  'ci/merge-gate'
];

const SUCCESS_STATES: Array<string> = ['success', 'ok'];
const FAILURE_STATES: Array<string> = ['failure', 'failed', 'error'];

const QUEUE_KEYWORDS: Array<string> = [
  '/ci build', '/ci merge', 'queue', 'queued', 'waiting', 'pending', 'running',
  'ingest', 'stale_input', '排队', '队列', '等待', '正在执行', '运行中'
];

const QUEUE_NEGATIVE_KEYWORDS: Array<string> = [
  '已离开活动队列', '当前不在', 'build-timing', '耗时表', '等待已结束',
  '：通过', '= `通过`', '= `success`', '执行结果：成功'
];

const FAILURE_KEYWORDS: Array<string> = [
  'fail', 'failed', 'failure', 'error', 'stale_input', 'timeout',
  '缺失', '失败', '错误', '未通过', '阻塞'
];

export class PrBriefLogic {
  static buildSummary(
    prNumber: number,
    pr: JsonObject,
    statuses: Array<JsonObject>,
    comments: Array<JsonObject>
  ): PrSummary {
    const latest = PrBriefLogic.latestStatusesByContext(statuses);
    const summary = new PrSummary();
    summary.fetchedAt = new Date().toISOString();
    summary.pr = PrBriefLogic.prInfo(prNumber, pr);
    summary.gates = PrBriefLogic.gateItems(latest, comments, summary.pr.headSha);
    summary.queue = PrBriefLogic.queueEvents(comments, 3);
    summary.hiddenSuccessContexts = PrBriefLogic.hiddenSuccessContexts(latest);
    return summary;
  }

  private static prInfo(prNumber: number, pr: JsonObject): PrInfo {
    const head = PrBriefLogic.objectValue(pr, 'head');
    const base = PrBriefLogic.objectValue(pr, 'base');
    const user = PrBriefLogic.objectValue(pr, 'user');
    const info = new PrInfo();
    info.number = prNumber;
    info.title = PrBriefLogic.stringValue(pr, 'title');
    info.state = PrBriefLogic.stringValue(pr, 'state');
    info.body = PrBriefLogic.stringValue(pr, 'body');
    info.htmlUrl = PrBriefLogic.firstNonEmpty(
      PrBriefLogic.stringValue(pr, 'html_url'),
      `${GiteaConfig.WEB_BASE}/${GiteaConfig.OWNER}/${GiteaConfig.REPO}/pulls/${prNumber}`
    );
    info.headSha = PrBriefLogic.stringValue(head, 'sha');
    info.headRef = PrBriefLogic.stringValue(head, 'ref');
    info.baseRef = PrBriefLogic.stringValue(base, 'ref');
    info.updatedAt = PrBriefLogic.stringValue(pr, 'updated_at');
    info.author = PrBriefLogic.stringValue(user, 'login');
    return info;
  }

  private static latestStatusesByContext(statuses: Array<JsonObject>): Record<string, JsonObject> {
    const latest: Record<string, JsonObject> = {};
    for (const status of statuses) {
      const context = PrBriefLogic.firstNonEmpty(
        PrBriefLogic.stringValue(status, 'context'),
        PrBriefLogic.stringValue(status, 'name')
      ).trim();
      if (context.length === 0) {
        continue;
      }
      status.context = context;
      status.state = PrBriefLogic.normalizeState(status);
      const existing = latest[context];
      if (!existing || PrBriefLogic.statusSortKey(status) >= PrBriefLogic.statusSortKey(existing)) {
        latest[context] = status;
      }
    }
    return latest;
  }

  private static gateItems(
    latestStatuses: Record<string, JsonObject>,
    comments: Array<JsonObject>,
    currentHeadSha: string
  ): Array<GateItem> {
    const items: Array<GateItem> = [];
    for (const context of GATE_CONTEXTS) {
      const status = PrBriefLogic.latestStatusForGate(latestStatuses, context);
      if (!status) {
        continue;
      }
      const state = PrBriefLogic.normalizeState(status);
      if (SUCCESS_STATES.includes(state)) {
        continue;
      }

      const comment = FAILURE_STATES.includes(state)
        ? PrBriefLogic.latestRelevantComment(context, comments, currentHeadSha)
        : undefined;
      const description = PrBriefLogic.cleanText(PrBriefLogic.stringValue(status, 'description'));
      const commentSummary = comment
        ? PrBriefLogic.summarizeFailureText(PrBriefLogic.stringValue(comment, 'body'), 1000)
        : '';
      const item = new GateItem();
      item.context = context;
      item.state = state.length === 0 ? 'unknown' : state;
      item.summary = PrBriefLogic.joinDistinct(description, commentSummary);
      if (item.summary.length === 0) {
        item.summary = 'No failure detail was published.';
      }
      item.targetUrl = PrBriefLogic.stringValue(status, 'target_url');
      item.updatedAt = PrBriefLogic.firstNonEmpty(
        PrBriefLogic.stringValue(status, 'updated_at'),
        PrBriefLogic.stringValue(status, 'created_at')
      );
      item.commentUrl = comment ? PrBriefLogic.stringValue(comment, 'html_url') : '';
      items.push(item);
    }
    return items;
  }

  private static queueEvents(comments: Array<JsonObject>, limit: number): Array<QueueEvent> {
    const sorted = comments.slice().sort((left, right) => PrBriefLogic.commentSortKey(right)
      .localeCompare(PrBriefLogic.commentSortKey(left)));
    const events: Array<QueueEvent> = [];
    const seenCommands: Set<string> = new Set();
    for (const comment of sorted) {
      const body = PrBriefLogic.stringValue(comment, 'body');
      if (!PrBriefLogic.isQueueComment(body)) {
        continue;
      }
      const command = PrBriefLogic.exactCiCommand(body);
      if (command.length > 0 && seenCommands.has(command)) {
        continue;
      }
      if (command.length > 0) {
        seenCommands.add(command);
      }
      const user = PrBriefLogic.objectValue(comment, 'user');
      const event = new QueueEvent();
      event.author = PrBriefLogic.stringValue(user, 'login');
      event.createdAt = PrBriefLogic.stringValue(comment, 'created_at');
      event.updatedAt = PrBriefLogic.stringValue(comment, 'updated_at');
      event.htmlUrl = PrBriefLogic.stringValue(comment, 'html_url');
      event.summary = PrBriefLogic.summarizeComment(body, 700);
      events.push(event);
      if (events.length >= limit) {
        break;
      }
    }
    return events;
  }

  private static hiddenSuccessContexts(latestStatuses: Record<string, JsonObject>): Array<string> {
    const contexts: Array<string> = [];
    for (const context of GATE_CONTEXTS) {
      const status = PrBriefLogic.latestStatusForGate(latestStatuses, context);
      if (status && SUCCESS_STATES.includes(PrBriefLogic.normalizeState(status))) {
        contexts.push(context);
      }
    }
    return contexts;
  }

  private static latestStatusForGate(
    latestStatuses: Record<string, JsonObject>,
    context: string
  ): JsonObject | undefined {
    const aliases = context === 'protected-file-approval'
      ? ['protected-file-approval', 'taichu/protected-file-approval']
      : [context];
    let best: JsonObject | undefined = undefined;
    for (const alias of aliases) {
      const status = latestStatuses[alias];
      if (status && (!best || PrBriefLogic.statusSortKey(status) >= PrBriefLogic.statusSortKey(best))) {
        best = status;
      }
    }
    return best;
  }

  private static latestRelevantComment(
    context: string,
    comments: Array<JsonObject>,
    currentHeadSha: string
  ): JsonObject | undefined {
    const hints = PrBriefLogic.contextHints(context);
    const sorted = comments.slice().sort((left, right) => PrBriefLogic.commentSortKey(right)
      .localeCompare(PrBriefLogic.commentSortKey(left)));
    for (const comment of sorted) {
      const body = PrBriefLogic.stringValue(comment, 'body');
      const lowered = body.toLowerCase();
      if (PrBriefLogic.referencesDifferentHead(body, currentHeadSha)) {
        continue;
      }
      if (context === 'taichu/codex-pr-review'
        && (lowered.includes('taichu-codex-pr-test-review')
          || lowered.includes('taichu/codex-pr-test-review'))) {
        continue;
      }
      for (const hint of hints) {
        if (lowered.includes(hint.toLowerCase())) {
          return comment;
        }
      }
    }
    return undefined;
  }

  private static referencesDifferentHead(body: string, currentHeadSha: string): boolean {
    if (currentHeadSha.length < 7) {
      return false;
    }
    const lowered = body.toLowerCase();
    const current = currentHeadSha.toLowerCase();
    const markerPattern: RegExp = /<!--\s*taichu-(?:protected-file-approval|codex-pr(?:-test)?-review)-head\s*:\s*`?([0-9a-f]{7,64})`?\s*-->/gi;
    let marker: RegExpExecArray | null = markerPattern.exec(lowered);
    let foundMarker = false;
    while (marker !== null) {
      foundMarker = true;
      const markedHead = marker[1].toLowerCase();
      if (!current.startsWith(markedHead) && !markedHead.startsWith(current)) {
        return true;
      }
      marker = markerPattern.exec(lowered);
    }
    if (foundMarker) {
      return false;
    }
    if (lowered.includes(current.slice(0, 7)) || lowered.includes(current.slice(0, 12))) {
      return false;
    }
    return [
      'pr head',
      '当前 pr head',
      '当前 head',
      '顶端提交',
      'pr 顶端',
      'head |',
      '| head |'
    ].some((value: string) => lowered.includes(value));
  }

  private static contextHints(context: string): Array<string> {
    if (context === 'protected-file-approval') {
      return ['protected-file', 'protected file', 'approval'];
    }
    if (context === 'taichu/codex-pr-review') {
      return ['codex-pr-review', 'taichu-pr-codex-review', 'codex review'];
    }
    if (context === 'taichu/codex-pr-test-review') {
      return [
        'taichu-codex-pr-test-review',
        'taichu/codex-pr-test-review',
        'codex pr test review'
      ];
    }
    if (context === 'taichu/pr-build') {
      return ['taichu/pr-build', 'pr-build', '/ci build', 'ci build'];
    }
    if (context === 'taichu/dev-cloud-preflight') {
      return ['taichu-dev-cloud-preflight', 'taichu/dev-cloud-preflight'];
    }
    if (context === 'ci/merge-gate') {
      return ['ci/merge-gate', 'merge-gate', '/ci merge', 'ci merge'];
    }
    return [context];
  }

  private static isQueueComment(body: string): boolean {
    const lowered = body.toLowerCase();
    for (const keyword of QUEUE_NEGATIVE_KEYWORDS) {
      if (lowered.includes(keyword.toLowerCase()) || body.includes(keyword)) {
        return false;
      }
    }
    for (const keyword of QUEUE_KEYWORDS) {
      if (lowered.includes(keyword.toLowerCase()) || body.includes(keyword)) {
        return true;
      }
    }
    return false;
  }

  private static exactCiCommand(body: string): string {
    const command = body.trim().toLowerCase();
    return command === '/ci build' || command === '/ci merge' ? command : '';
  }

  private static summarizeFailureText(text: string, maxChars: number): string {
    const lines = PrBriefLogic.meaningfulLines(text);
    let selected: Array<string> = [];
    for (const line of lines) {
      const lowered = line.toLowerCase();
      if (lowered.includes('| pass |') || lowered.includes(' pass ')) {
        continue;
      }
      if (line.includes('说明/失败原因')) {
        continue;
      }
      if (FAILURE_KEYWORDS.some((keyword) => lowered.includes(keyword.toLowerCase()) || line.includes(keyword))) {
        selected.push(line);
      }
    }
    if (selected.length === 0) {
      selected = lines.slice(0, 8);
    }
    return PrBriefLogic.truncate(selected.slice(0, 12).join('\n'), maxChars);
  }

  private static summarizeComment(text: string, maxChars: number): string {
    return PrBriefLogic.truncate(PrBriefLogic.meaningfulLines(text).slice(0, 10).join('\n'), maxChars);
  }

  private static meaningfulLines(text: string): Array<string> {
    const stripped = text.replace(/<!--[\s\S]*?-->/g, '');
    const lines: Array<string> = [];
    for (const rawLine of stripped.split(/\r?\n/)) {
      const line = PrBriefLogic.cleanText(rawLine);
      if (line.length === 0) {
        continue;
      }
      if (line === '| --- | --- |' || line === '| --- | --- | --- |' || line === '```text' || line === '```') {
        continue;
      }
      lines.push(line);
    }
    return lines;
  }

  private static cleanText(text: string): string {
    return text
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&amp;/g, '&')
      .replace(/<[^>]+>/g, ' ')
      .replace(/\[([^\]]+)]\([^)]+\)/g, '$1')
      .replace(/^\s{0,3}#{1,6}\s*/, '')
      .replace(/\*\*/g, '')
      .replace(/__/g, '')
      .replace(/`/g, '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  private static normalizeState(status: JsonObject): string {
    return PrBriefLogic.firstNonEmpty(
      PrBriefLogic.stringValue(status, 'state'),
      PrBriefLogic.stringValue(status, 'status')
    ).trim().toLowerCase();
  }

  private static statusSortKey(status: JsonObject): string {
    return `${PrBriefLogic.statusTimestamp(status)}:${PrBriefLogic.numberValue(status, 'id')}`;
  }

  private static commentSortKey(comment: JsonObject): string {
    const timestamp = PrBriefLogic.firstNonEmpty(
      PrBriefLogic.stringValue(comment, 'updated_at'),
      PrBriefLogic.stringValue(comment, 'created_at')
    );
    return `${timestamp}:${PrBriefLogic.numberValue(comment, 'id')}`;
  }

  private static statusTimestamp(status: JsonObject): string {
    return PrBriefLogic.firstNonEmpty(
      PrBriefLogic.stringValue(status, 'updated_at'),
      PrBriefLogic.firstNonEmpty(
        PrBriefLogic.stringValue(status, 'created_at'),
        PrBriefLogic.firstNonEmpty(
          PrBriefLogic.stringValue(status, 'submitted_at'),
          PrBriefLogic.stringValue(status, 'date')
        )
      )
    );
  }

  private static joinDistinct(first: string, second: string): string {
    if (first.length === 0) {
      return second;
    }
    if (second.length === 0 || first.includes(second)) {
      return first;
    }
    return `${first}\n\n${second}`;
  }

  private static truncate(text: string, maxChars: number): string {
    if (text.length <= maxChars) {
      return text;
    }
    return `${text.substring(0, Math.max(0, maxChars - 1)).trim()}…`;
  }

  private static objectValue(object: JsonObject, key: string): JsonObject {
    const value = object[key];
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      return value as JsonObject;
    }
    return {};
  }

  private static stringValue(object: JsonObject, key: string): string {
    const value: Object = object[key];
    return typeof value === 'string' ? value : '';
  }

  private static numberValue(object: JsonObject, key: string): number {
    const value: Object = object[key];
    return typeof value === 'number' ? value : 0;
  }

  private static firstNonEmpty(first: string, second: string): string {
    return first.length === 0 ? second : first;
  }
}
