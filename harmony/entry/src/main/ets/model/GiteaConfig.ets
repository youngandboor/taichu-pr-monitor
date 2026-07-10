export class GiteaConfig {
  static readonly API_BASE: string = 'https://taichu.fun/gitea/api/v1';
  static readonly WEB_BASE: string = 'https://taichu.fun/gitea';
  static readonly LOGIN_URL: string = 'https://taichu.fun/gitea/user/login';
  static readonly OAUTH_AUTHORIZE_URL: string = 'https://taichu.fun/gitea/login/oauth/authorize';
  static readonly OAUTH_TOKEN_URL: string = 'https://taichu.fun/gitea/login/oauth/access_token';
  static readonly OAUTH_APP_SETTINGS_URL: string = 'https://taichu.fun/gitea/user/settings/applications';
  static readonly REDIRECT_URI: string = 'http://127.0.0.1:43122/oauth';
  static readonly OAUTH_SCOPE: string = 'read:repository write:issue read:user';
  static readonly OWNER: string = 'SystemAgentDev';
  static readonly REPO: string = 'TaiChu';
}
