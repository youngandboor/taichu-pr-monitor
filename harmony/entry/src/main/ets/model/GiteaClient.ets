import http from '@ohos.net.http';
import { GiteaConfig } from './GiteaConfig';
import { JsonObject, PrSummary } from './PrModels';
import { PrBriefLogic } from './PrBriefLogic';

export class AuthRequiredError extends Error {
  constructor(message: string = 'Gitea authorization is required') {
    super(message);
  }
}

export class GiteaClient {
  private accessToken: string = '';

  setAccessToken(accessToken: string): void {
    this.accessToken = accessToken;
  }

  async hasValidLogin(): Promise<boolean> {
    await this.requestObject('/user');
    return true;
  }

  async fetchSummary(prNumber: number): Promise<PrSummary> {
    const pr = await this.requestObject(`/repos/${GiteaConfig.OWNER}/${GiteaConfig.REPO}/pulls/${prNumber}`);
    const head = this.objectValue(pr, 'head');
    const headSha = this.stringValue(head, 'sha');
    if (headSha.trim().length === 0) {
      throw new Error('PR response has no head sha');
    }

    let statuses: Array<JsonObject> = [];
    try {
      statuses = await this.requestArrayPages(
        `/repos/${GiteaConfig.OWNER}/${GiteaConfig.REPO}/statuses/${headSha}`,
        5
      );
    } catch (error) {
      if (error instanceof AuthRequiredError) {
        throw error;
      }
      statuses = [];
    }
    if (statuses.length === 0) {
      const combined = await this.requestObject(
        `/repos/${GiteaConfig.OWNER}/${GiteaConfig.REPO}/commits/${headSha}/status`
      );
      const value: Object = combined.statuses;
      statuses = Array.isArray(value) ? value as Array<JsonObject> : [];
    }

    const comments = await this.requestArrayPages(
      `/repos/${GiteaConfig.OWNER}/${GiteaConfig.REPO}/issues/${prNumber}/comments`,
      3
    );
    return PrBriefLogic.buildSummary(prNumber, pr, statuses, comments);
  }

  async postIssueComment(prNumber: number, body: string): Promise<void> {
    const payload = JSON.stringify({ body });
    await this.requestWithAuth(
      `/repos/${GiteaConfig.OWNER}/${GiteaConfig.REPO}/issues/${prNumber}/comments`,
      'bearer',
      http.RequestMethod.POST,
      payload,
      { 'Content-Type': 'application/json' }
    );
  }

  async exchangeOAuthCode(clientId: string, code: string, codeVerifier: string): Promise<string> {
    const body = [
      `grant_type=${encodeURIComponent('authorization_code')}`,
      `client_id=${encodeURIComponent(clientId)}`,
      `code=${encodeURIComponent(code)}`,
      `redirect_uri=${encodeURIComponent(GiteaConfig.REDIRECT_URI)}`,
      `code_verifier=${encodeURIComponent(codeVerifier)}`
    ].join('&');
    const text = await this.requestRaw(GiteaConfig.OAUTH_TOKEN_URL, http.RequestMethod.POST, body, {
      'Accept': 'application/json',
      'Content-Type': 'application/x-www-form-urlencoded'
    });
    const value = JSON.parse(text) as Object;
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      const token = (value as JsonObject).access_token;
      if (typeof token === 'string' && token.length > 0) {
        return token;
      }
    }
    throw new Error(`OAuth token response has no access_token: ${text}`);
  }

  private async requestObject(path: string): Promise<JsonObject> {
    const text = await this.request(path);
    const value = JSON.parse(text) as Object;
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      return value as JsonObject;
    }
    throw new Error(`expected object payload for ${path}`);
  }

  private async requestArrayPages(path: string, maxPages: number): Promise<Array<JsonObject>> {
    const allItems: Array<JsonObject> = [];
    for (let page = 1; page <= maxPages; page++) {
      const separator = path.includes('?') ? '&' : '?';
      const text = await this.request(`${path}${separator}limit=100&page=${page}`);
      const value = JSON.parse(text) as Object;
      if (!Array.isArray(value)) {
        throw new Error(`expected list payload for ${path}`);
      }
      const pageItems = value as Array<JsonObject>;
      allItems.push(...pageItems);
      if (pageItems.length < 100) {
        break;
      }
    }
    return allItems;
  }

  private async request(path: string): Promise<string> {
    if (this.accessToken.trim().length === 0) {
      throw new AuthRequiredError();
    }
    return this.requestWithAuth(path, 'bearer');
  }

  private async requestWithAuth(
    path: string,
    scheme: string,
    method: http.RequestMethod = http.RequestMethod.GET,
    body: string | undefined = undefined,
    extraHeaders: Record<string, string> = {}
  ): Promise<string> {
    try {
      const headers: Record<string, string> = {
        'Accept': 'application/json',
        'Authorization': `${scheme} ${this.accessToken}`
      };
      for (const key of Object.keys(extraHeaders)) {
        headers[key] = extraHeaders[key];
      }
      return await this.requestRaw(`${GiteaConfig.API_BASE}${path}`, method, body, headers);
    } catch (error) {
      if (scheme === 'bearer' && error instanceof AuthRequiredError) {
        return this.requestWithAuth(path, 'token', method, body, extraHeaders);
      }
      throw error instanceof Error ? error : new Error(JSON.stringify(error));
    }
  }

  private async requestRaw(
    url: string,
    method: http.RequestMethod,
    body: string | undefined,
    headers: Record<string, string>
  ): Promise<string> {
    const request = http.createHttp();
    try {
      const response = await request.request(url, {
        method,
        extraData: body,
        connectTimeout: 15000,
        readTimeout: 20000,
        header: headers
      });
      const code = response.responseCode as number;
      const text = typeof response.result === 'string' ? response.result : JSON.stringify(response.result);
      if (code === 401 || code === 403) {
        throw new AuthRequiredError(`Gitea API ${code}: ${text}`);
      }
      if (code >= 400) {
        throw new Error(`Gitea API ${code}: ${text}`);
      }
      return text;
    } finally {
      request.destroy();
    }
  }

  private objectValue(object: JsonObject, key: string): JsonObject {
    const value = object[key];
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      return value as JsonObject;
    }
    return {};
  }

  private stringValue(object: JsonObject, key: string): string {
    const value = object[key];
    return typeof value === 'string' ? value : '';
  }
}
