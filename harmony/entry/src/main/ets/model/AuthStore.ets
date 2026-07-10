import common from '@ohos.app.ability.common';
import preferences from '@ohos.data.preferences';

const STORE_NAME: string = 'pr_monitor_auth';
const KEY_CLIENT_ID: string = 'oauth_client_id';
const KEY_ACCESS_TOKEN: string = 'oauth_access_token';

export class AuthStore {
  private readonly context: common.UIAbilityContext;

  constructor(context: common.UIAbilityContext) {
    this.context = context;
  }

  async load(): Promise<AuthState> {
    const store = await preferences.getPreferences(this.context, { name: STORE_NAME });
    const clientIdValue = await store.get(KEY_CLIENT_ID, '');
    const tokenValue = await store.get(KEY_ACCESS_TOKEN, '');
    return {
      clientId: typeof clientIdValue === 'string' ? clientIdValue : '',
      accessToken: typeof tokenValue === 'string' ? tokenValue : ''
    };
  }

  async saveClientId(clientId: string): Promise<void> {
    const store = await preferences.getPreferences(this.context, { name: STORE_NAME });
    await store.put(KEY_CLIENT_ID, clientId.trim());
    await store.flush();
  }

  async saveAccessToken(accessToken: string): Promise<void> {
    const store = await preferences.getPreferences(this.context, { name: STORE_NAME });
    await store.put(KEY_ACCESS_TOKEN, accessToken.trim());
    await store.flush();
  }

  async clearAccessToken(): Promise<void> {
    const store = await preferences.getPreferences(this.context, { name: STORE_NAME });
    await store.put(KEY_ACCESS_TOKEN, '');
    await store.flush();
  }
}

export interface AuthState {
  clientId: string;
  accessToken: string;
}
