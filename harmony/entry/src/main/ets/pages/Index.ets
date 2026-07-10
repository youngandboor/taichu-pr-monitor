import common from '@ohos.app.ability.common';
import promptAction from '@ohos.promptAction';
import webview from '@ohos.web.webview';
import { AuthStore } from '../model/AuthStore';
import { AuthRequiredError, GiteaClient } from '../model/GiteaClient';
import { GiteaConfig } from '../model/GiteaConfig';
import { GateItem, PrSummary, QueueEvent } from '../model/PrModels';

const REFRESH_INTERVAL_MS: number = 60000;

@Entry
@Component
struct Index {
  private readonly context: common.UIAbilityContext = getContext(this) as common.UIAbilityContext;
  private readonly webController: webview.WebviewController = new webview.WebviewController();
  private readonly authStore: AuthStore = new AuthStore(this.context);
  private readonly client: GiteaClient = new GiteaClient();
  private monitorTimer: number = -1;
  private oauthState: string = '';
  private oauthVerifier: string = '';
  private webReady: boolean = false;
  private autoCreateOAuth: boolean = false;
  private autoCreateSubmitted: boolean = false;
  private autoCreateAttempts: number = 0;

  @State private screen: string = 'loading';
  @State private loginMessage: string = '';
  @State private clientIdInput: string = '';
  @State private oauthUrl: string = GiteaConfig.OAUTH_APP_SETTINGS_URL;
  @State private prText: string = '1222';
  @State private title: string = 'PR #1222';
  @State private meta: string = '只看关键门禁、队列和 PR body';
  @State private status: string = '等待刷新';
  @State private loading: boolean = false;
  @State private monitoring: boolean = false;
  @State private postingCommand: boolean = false;
  @State private gates: Array<GateItem> = [];
  @State private queue: Array<QueueEvent> = [];
  @State private body: string = '';

  aboutToAppear(): void {
    webview.WebCookieManager.putAcceptCookieEnabled(true);
    void this.loadAuthState();
  }

  aboutToDisappear(): void {
    this.clearMonitorTimer();
  }

  build() {
    Column() {
      if (this.screen === 'monitor') {
        this.monitorView();
      } else if (this.screen === 'oauth') {
        this.oauthView();
      } else if (this.screen === 'setupWeb') {
        this.setupWebView();
      } else {
        this.setupView();
      }
    }
    .width('100%')
    .height('100%')
    .backgroundColor('#F5F6F8')
  }

  @Builder
  private setupView() {
    Column() {
      Text('配置 OAuth')
        .fontSize(22)
        .fontWeight(FontWeight.Bold)
        .fontColor('#151922')
        .width('100%')
      Text('在 Gitea 创建公开 OAuth2 应用，填入 client_id 后授权。')
        .fontSize(14)
        .fontColor('#667085')
        .lineHeight(20)
        .margin({ top: 6 })
        .width('100%')
      if (this.loginMessage.length > 0) {
        Text(this.loginMessage)
          .fontSize(13)
          .fontColor('#B42318')
          .lineHeight(19)
          .margin({ top: 8 })
          .width('100%')
      }
      Text('Redirect URI')
        .fontSize(13)
        .fontWeight(FontWeight.Bold)
        .fontColor('#363F4F')
        .margin({ top: 18 })
        .width('100%')
      Text(GiteaConfig.REDIRECT_URI)
        .fontSize(13)
        .fontColor('#283142')
        .lineHeight(20)
        .copyOption(CopyOptions.InApp)
        .padding(12)
        .backgroundColor('#FFFFFF')
        .border({ width: 1, color: '#D8DEE8' })
        .borderRadius(8)
        .margin({ top: 6 })
        .width('100%')
      TextInput({ text: this.clientIdInput, placeholder: 'OAuth client_id' })
        .height(48)
        .fontSize(14)
        .fontColor('#151922')
        .backgroundColor('#FFFFFF')
        .border({ width: 1, color: '#D8DEE8' })
        .borderRadius(8)
        .padding({ left: 12, right: 12 })
        .margin({ top: 12 })
        .onChange((value: string) => {
          this.clientIdInput = value;
        })
        .width('100%')
      Row() {
        Button('一键创建')
          .height(48)
          .layoutWeight(1)
          .fontSize(15)
          .fontWeight(FontWeight.Bold)
          .fontColor('#175CD3')
          .backgroundColor('#FFFFFF')
          .border({ width: 1, color: '#D8DEE8' })
          .borderRadius(9)
          .onClick(() => this.startAutoCreateOAuthApp())
        Button('授权')
          .height(48)
          .layoutWeight(1)
          .fontSize(15)
          .fontWeight(FontWeight.Bold)
          .fontColor('#FFFFFF')
          .backgroundColor('#175CD3')
          .borderRadius(9)
          .margin({ left: 8 })
          .onClick(() => this.saveClientAndStartOAuth())
      }
      .margin({ top: 12 })
      .width('100%')
      Text('创建应用时取消勾选“机密客户端”，Redirect URI 填上面的值。')
        .fontSize(13)
        .fontColor('#667085')
        .lineHeight(19)
        .margin({ top: 12 })
        .width('100%')
    }
    .padding({ left: 16, right: 16, top: 18, bottom: 16 })
    .height('100%')
    .width('100%')
  }

  @Builder
  private oauthView() {
    Column() {
      Text('授权 Gitea')
        .fontSize(22)
        .fontWeight(FontWeight.Bold)
        .fontColor('#151922')
        .width('100%')
      Text('登录并授权后会自动回到监控页。')
        .fontSize(14)
        .fontColor('#667085')
        .lineHeight(20)
        .margin({ top: 6 })
        .width('100%')
      if (this.loginMessage.length > 0) {
        Text(this.loginMessage)
          .fontSize(13)
          .fontColor('#B42318')
          .lineHeight(19)
          .margin({ top: 8 })
          .width('100%')
      }
      Row() {
        Button('重新授权')
          .height(48)
          .layoutWeight(1)
          .fontSize(15)
          .fontWeight(FontWeight.Bold)
          .fontColor('#FFFFFF')
          .backgroundColor('#175CD3')
          .borderRadius(9)
          .onClick(() => this.startOAuth())
        Button('改 client_id')
          .height(48)
          .layoutWeight(1)
          .fontSize(15)
          .fontWeight(FontWeight.Bold)
          .fontColor('#175CD3')
          .backgroundColor('#FFFFFF')
          .border({ width: 1, color: '#D8DEE8' })
          .borderRadius(9)
          .margin({ left: 8 })
          .onClick(() => {
            this.screen = 'setup';
          })
      }
      .margin({ top: 12, bottom: 12 })
      .width('100%')
      this.authWebView()
    }
    .padding({ left: 16, right: 16, top: 18, bottom: 16 })
    .height('100%')
    .width('100%')
  }

  @Builder
  private setupWebView() {
    Column() {
      Text('创建 OAuth2 应用')
        .fontSize(22)
        .fontWeight(FontWeight.Bold)
        .fontColor('#151922')
        .width('100%')
      Text('取消勾选“机密客户端”，创建后复制 client_id。')
        .fontSize(14)
        .fontColor('#667085')
        .lineHeight(20)
        .margin({ top: 6 })
        .width('100%')
      if (this.loginMessage.length > 0) {
        Text(this.loginMessage)
          .fontSize(13)
          .fontColor(this.loginMessage.indexOf('失败') >= 0 || this.loginMessage.indexOf('未找到') >= 0 ? '#B42318' : '#667085')
          .lineHeight(19)
          .margin({ top: 8 })
          .width('100%')
      }
      Button('返回填写 client_id')
        .height(48)
        .width('100%')
        .fontSize(15)
        .fontWeight(FontWeight.Bold)
        .fontColor('#FFFFFF')
        .backgroundColor('#175CD3')
        .borderRadius(9)
        .margin({ top: 12, bottom: 12 })
        .onClick(() => {
          this.screen = 'setup';
        })
      this.authWebView()
    }
    .padding({ left: 16, right: 16, top: 18, bottom: 16 })
    .height('100%')
    .width('100%')
  }

  @Builder
  private authWebView() {
    Web({ src: this.oauthUrl, controller: this.webController })
      .javaScriptAccess(true)
      .domStorageAccess(true)
      .onlineImageAccess(true)
      .borderRadius(10)
      .onControllerAttached(() => {
        this.webReady = true;
      })
      .onOverrideUrlLoading((request) => this.handleOAuthUrl(request.getRequestUrl()))
      .onLoadIntercept((event) => this.handleOAuthUrl(event.data.getRequestUrl()))
      .onPageEnd((event) => {
        webview.WebCookieManager.saveCookieSync();
        this.handleOAuthUrl(event.url);
        this.maybeContinueAutoCreate();
      })
      .layoutWeight(1)
      .width('100%')
  }

  @Builder
  private monitorView() {
    Column() {
      Column() {
        Text('TAICHU PR MONITOR')
          .fontSize(11)
          .fontWeight(FontWeight.Bold)
          .fontColor('#8A93A3')
          .width('100%')
        Text(this.title)
          .fontSize(22)
          .fontWeight(FontWeight.Bold)
          .fontColor('#151922')
          .lineHeight(28)
          .margin({ top: 4 })
          .width('100%')
        Text(this.meta)
          .fontSize(13)
          .fontColor('#667085')
          .lineHeight(18)
          .margin({ top: 4 })
          .width('100%')
        this.commandBar()
        this.ciCommandBar()
        Text(this.status)
          .fontSize(13)
          .fontColor(this.loading ? '#9A6700' : '#667085')
          .lineHeight(18)
          .margin({ top: 12 })
          .width('100%')
      }
      .padding({ left: 16, right: 16, top: 18, bottom: 12 })
      .backgroundColor('#F5F6F8')
      .width('100%')

      Scroll() {
        Column() {
          this.blockersSection()
          this.queueSection()
          this.bodySection()
        }
        .padding({ left: 16, right: 16, top: 4, bottom: 32 })
      }
      .layoutWeight(1)
      .width('100%')
    }
    .height('100%')
    .width('100%')
  }

  @Builder
  private commandBar() {
    Row() {
      TextInput({ text: this.prText, placeholder: 'PR #' })
        .type(InputType.Number)
        .height(48)
        .layoutWeight(1)
        .fontSize(14)
        .fontColor('#151922')
        .backgroundColor('#F3F6FA')
        .borderRadius(8)
        .padding({ left: 12, right: 12 })
        .onChange((value: string) => {
          this.prText = value;
        })
      Button('刷新')
        .enabled(!this.loading)
        .height(48)
        .width(70)
        .fontSize(14)
        .fontWeight(FontWeight.Bold)
        .fontColor('#175CD3')
        .backgroundColor('#FFFFFF')
        .border({ width: 1, color: '#D8DEE8' })
        .borderRadius(8)
        .margin({ left: 6 })
        .opacity(this.loading ? 0.62 : 1)
        .onClick(() => this.refreshSummary(true))
      Button(this.monitoring ? '暂停' : '监控')
        .height(48)
        .width(72)
        .fontSize(14)
        .fontWeight(FontWeight.Bold)
        .fontColor(this.monitoring ? '#175CD3' : '#FFFFFF')
        .backgroundColor(this.monitoring ? '#FFFFFF' : '#175CD3')
        .border({ width: 1, color: this.monitoring ? '#D8DEE8' : '#175CD3' })
        .borderRadius(8)
        .margin({ left: 6 })
        .onClick(() => this.toggleMonitoring())
    }
    .height(56)
    .padding(4)
    .margin({ top: 14 })
    .backgroundColor('#FFFFFF')
    .border({ width: 1, color: '#D8DEE8' })
    .borderRadius(12)
    .shadow({
      radius: 10,
      color: '#10182812',
      offsetX: 0,
      offsetY: 2
    })
    .width('100%')
  }

  @Builder
  private ciCommandBar() {
    Row() {
      Button('rebuild')
        .enabled(!this.loading && !this.postingCommand)
        .height(44)
        .layoutWeight(1)
        .fontSize(14)
        .fontWeight(FontWeight.Bold)
        .fontColor('#175CD3')
        .backgroundColor('#FFFFFF')
        .border({ width: 1, color: '#D8DEE8' })
        .borderRadius(8)
        .opacity(this.loading || this.postingCommand ? 0.62 : 1)
        .onClick(() => {
          void this.postCiCommand('/ci build');
        })
      Button('remerge')
        .enabled(!this.loading && !this.postingCommand)
        .height(44)
        .layoutWeight(1)
        .fontSize(14)
        .fontWeight(FontWeight.Bold)
        .fontColor('#FFFFFF')
        .backgroundColor('#344054')
        .borderRadius(8)
        .margin({ left: 8 })
        .opacity(this.loading || this.postingCommand ? 0.62 : 1)
        .onClick(() => {
          void this.postCiCommand('/ci merge');
        })
    }
    .margin({ top: 8 })
    .width('100%')
  }

  @Builder
  private blockersSection() {
    Column() {
      Text('LATEST BLOCKERS')
        .fontSize(13)
        .fontWeight(FontWeight.Bold)
        .fontColor('#363F4F')
        .width('100%')
      Column() {
        if (this.gates.length === 0) {
          this.messageCard('关键门禁暂无失败', '#14733F', '#EFFAF3')
        } else {
          ForEach(this.gates, (gate: GateItem, index: number) => {
            this.gateCard(gate)
          }, (gate: GateItem, index: number) => `${index}-${gate.context}-${gate.state}-${gate.updatedAt}`)
        }
      }
      .margin({ top: 10 })
      .width('100%')
    }
    .padding(14)
    .backgroundColor('#FFFFFF')
    .border({ width: 1, color: '#D8DEE8' })
    .borderRadius(10)
    .shadow({
      radius: 6,
      color: '#10182808',
      offsetX: 0,
      offsetY: 1
    })
    .margin({ bottom: 12 })
    .width('100%')
  }

  @Builder
  private queueSection() {
    Column() {
      Text('QUEUE')
        .fontSize(13)
        .fontWeight(FontWeight.Bold)
        .fontColor('#363F4F')
        .width('100%')
      Column() {
        if (this.queue.length === 0) {
          this.messageCard('暂无有用排队信息', '#667085', '#FAFBFC')
        } else {
          ForEach(this.queue, (event: QueueEvent, index: number) => {
            this.queueCard(event)
          }, (event: QueueEvent, index: number) => `${index}-${event.author}-${event.updatedAt}-${event.summary}`)
        }
      }
      .margin({ top: 10 })
      .width('100%')
    }
    .padding(14)
    .backgroundColor('#FFFFFF')
    .border({ width: 1, color: '#D8DEE8' })
    .borderRadius(10)
    .shadow({
      radius: 6,
      color: '#10182808',
      offsetX: 0,
      offsetY: 1
    })
    .margin({ bottom: 12 })
    .width('100%')
  }

  @Builder
  private bodySection() {
    Column() {
      Text('PR BODY')
        .fontSize(13)
        .fontWeight(FontWeight.Bold)
        .fontColor('#363F4F')
        .width('100%')
      Column() {
        Text(this.body.length === 0 ? 'PR body 为空' : this.body)
          .fontSize(13)
          .fontColor('#283142')
          .lineHeight(20)
          .copyOption(CopyOptions.InApp)
          .width('100%')
      }
      .margin({ top: 10 })
      .width('100%')
    }
    .padding(14)
    .backgroundColor('#FFFFFF')
    .border({ width: 1, color: '#D8DEE8' })
    .borderRadius(10)
    .shadow({
      radius: 6,
      color: '#10182808',
      offsetX: 0,
      offsetY: 1
    })
    .margin({ bottom: 12 })
    .width('100%')
  }

  @Builder
  private gateCard(gate: GateItem) {
    Column() {
      Text(`${gate.context} · ${gate.state}`)
        .fontSize(14)
        .fontWeight(FontWeight.Bold)
        .fontColor('#B42318')
        .lineHeight(20)
        .width('100%')
      Text(gate.summary)
        .fontSize(13)
        .fontColor('#283142')
        .lineHeight(20)
        .copyOption(CopyOptions.InApp)
        .margin({ top: 6 })
        .width('100%')
      if (gate.targetUrl.length > 0 || gate.commentUrl.length > 0) {
        Button('打开详情')
          .height(42)
          .width('100%')
          .fontSize(14)
          .fontWeight(FontWeight.Bold)
          .fontColor('#175CD3')
          .backgroundColor('#FFFFFF')
          .border({ width: 1, color: '#D8DEE8' })
          .borderRadius(8)
          .margin({ top: 8 })
          .onClick(() => this.openUrl(gate.targetUrl.length > 0 ? gate.targetUrl : gate.commentUrl))
      }
    }
    .padding(12)
    .backgroundColor('#FFF4F2')
    .border({ width: 1, color: '#FFD2CC' })
    .borderRadius(8)
    .margin({ bottom: 10 })
    .width('100%')
  }

  @Builder
  private queueCard(event: QueueEvent) {
    Column() {
      Text(`${this.firstNonEmpty(event.author, 'Gitea')} · ${this.firstNonEmpty(event.updatedAt, event.createdAt)}`)
        .fontSize(12)
        .fontWeight(FontWeight.Bold)
        .fontColor('#667085')
        .lineHeight(18)
        .width('100%')
      Text(event.summary)
        .fontSize(13)
        .fontColor('#283142')
        .lineHeight(20)
        .copyOption(CopyOptions.InApp)
        .margin({ top: 6 })
        .width('100%')
    }
    .padding(12)
    .backgroundColor('#FAFBFC')
    .border({ width: 1, color: '#EAEDF2' })
    .borderRadius(8)
    .margin({ bottom: 10 })
    .width('100%')
  }

  @Builder
  private messageCard(text: string, color: string, background: string) {
    Text(text)
      .fontSize(14)
      .fontWeight(FontWeight.Bold)
      .fontColor(color)
      .lineHeight(20)
      .padding(12)
      .backgroundColor(background)
      .borderRadius(8)
      .width('100%')
  }

  private async loadAuthState(): Promise<void> {
    try {
      const state = await this.authStore.load();
      this.clientIdInput = state.clientId;
      this.client.setAccessToken(state.accessToken);
      this.loginMessage = '';
      if (state.accessToken.trim().length > 0) {
        this.screen = 'monitor';
        await this.refreshSummary(true);
      } else if (state.clientId.trim().length > 0) {
        this.screen = 'oauth';
        this.startOAuth();
      } else {
        this.screen = 'setup';
      }
    } catch (error) {
      this.screen = 'setup';
      this.loginMessage = `加载授权配置失败：${this.errorMessage(error)}`;
    }
  }

  private openOAuthSettings(): void {
    this.oauthUrl = GiteaConfig.OAUTH_APP_SETTINGS_URL;
    this.screen = 'setupWeb';
    this.loadWebUrl(this.oauthUrl);
  }

  private startAutoCreateOAuthApp(): void {
    this.autoCreateOAuth = true;
    this.autoCreateSubmitted = false;
    this.autoCreateAttempts = 0;
    this.loginMessage = '正在打开 Gitea OAuth2 应用设置页…';
    this.openOAuthSettings();
    setTimeout(() => {
      void this.runAutoCreateStep();
    }, 2500);
  }

  private async saveClientAndStartOAuth(): Promise<void> {
    const clientId = this.clientIdInput.trim();
    if (clientId.length === 0) {
      this.loginMessage = '请先填入 OAuth client_id。';
      return;
    }
    try {
      await this.authStore.saveClientId(clientId);
      await this.authStore.clearAccessToken();
      this.loginMessage = '';
      this.screen = 'oauth';
      this.startOAuth();
    } catch (error) {
      this.loginMessage = `保存 client_id 失败：${this.errorMessage(error)}`;
    }
  }

  private startOAuth(): void {
    const clientId = this.clientIdInput.trim();
    if (clientId.length === 0) {
      this.screen = 'setup';
      this.loginMessage = '请先填入 OAuth client_id。';
      return;
    }
    this.oauthState = this.randomToken(24);
    this.oauthVerifier = this.randomToken(48);
    this.oauthUrl = this.buildAuthorizeUrl(clientId, this.oauthState, this.oauthVerifier);
    this.loadWebUrl(this.oauthUrl);
  }

  private loadWebUrl(url: string): void {
    if (!this.webReady) {
      return;
    }
    try {
      this.webController.loadUrl(url);
    } catch (_) {
    }
  }

  private maybeContinueAutoCreate(): void {
    if (!this.autoCreateOAuth) {
      return;
    }
    this.autoCreateAttempts += 1;
    if (this.autoCreateAttempts > 8) {
      this.autoCreateOAuth = false;
      this.loginMessage = '自动创建超时，请手动创建后填入 client_id。';
      return;
    }
    setTimeout(() => {
      void this.runAutoCreateStep();
    }, 700);
  }

  private async runAutoCreateStep(): Promise<void> {
    if (!this.autoCreateOAuth) {
      return;
    }
    try {
      const result = await this.webController.runJavaScript(this.autoCreateScript(this.autoCreateSubmitted));
      const message = this.unquoteJsResult(result);
      if (message.startsWith('client_id:')) {
        const clientId = message.substring('client_id:'.length).trim();
        this.autoCreateOAuth = false;
        this.clientIdInput = clientId;
        await this.authStore.saveClientId(clientId);
        await this.authStore.clearAccessToken();
        this.loginMessage = '';
        this.screen = 'oauth';
        this.startOAuth();
      } else if (message === 'submitted') {
        this.autoCreateSubmitted = true;
        this.loginMessage = 'OAuth2 应用已提交，正在读取 client_id…';
        setTimeout(() => {
          void this.runAutoCreateStep();
        }, 1300);
      } else if (message.length > 0) {
        this.loginMessage = message;
      }
    } catch (error) {
      this.loginMessage = `自动创建失败：${this.errorMessage(error)}`;
    }
  }

  private autoCreateScript(submitted: boolean): string {
    const appName = JSON.stringify(`TaiChu PR Monitor ${new Date().toISOString().substring(0, 16).replace('T', ' ')}`);
    const redirectUri = JSON.stringify(GiteaConfig.REDIRECT_URI);
    const alreadySubmitted = submitted ? 'true' : 'false';
    return `(() => {
      const text = document.body ? document.body.innerText : '';
      if (${alreadySubmitted}) {
        const idMatch = text.match(/Client\\s*ID\\s*[:：]?\\s*([A-Za-z0-9._~-]{20,})/i)
          || text.match(/客户端\\s*ID\\s*[:：]?\\s*([A-Za-z0-9._~-]{20,})/i)
          || text.match(/client_id\\s*[:：]?\\s*([A-Za-z0-9._~-]{20,})/i);
        if (idMatch) return 'client_id:' + idMatch[1];

        const inputs = Array.from(document.querySelectorAll('input, textarea'));
        for (const element of inputs) {
          const value = element.value || '';
          if (/^[A-Za-z0-9._~-]{20,}$/.test(value)) {
            const labelText = (element.closest('.field') || element.parentElement || document.body).innerText || '';
            if (/Client\\s*ID|客户端\\s*ID|client_id/i.test(labelText)) return 'client_id:' + value;
          }
        }
        return '正在等待 Gitea 返回 client_id…';
      }

      const details = Array.from(document.querySelectorAll('details'));
      for (const item of details) {
        if ((item.innerText || '').includes('OAuth2')) item.open = true;
      }
      const forms = Array.from(document.querySelectorAll('form'));
      const form = forms.find((candidate) => {
        const value = ((candidate.getAttribute('action') || '') + ' ' + (candidate.innerText || '')).toLowerCase();
        return value.includes('oauth2') && candidate.querySelector('textarea');
      });
      if (!form) return '正在等待 OAuth2 创建表单…';

      const textInputs = Array.from(form.querySelectorAll('input[type=text], input:not([type]), textarea'));
      const nameInput = textInputs.find((element) => element.tagName.toLowerCase() === 'input');
      const redirectInput = textInputs.find((element) => element.tagName.toLowerCase() === 'textarea')
        || textInputs.find((element) => (element.getAttribute('name') || '').toLowerCase().includes('redirect'));
      if (!nameInput || !redirectInput) return '未找到 OAuth2 应用名或 Redirect URI 输入框。';

      nameInput.value = ${appName};
      nameInput.dispatchEvent(new Event('input', { bubbles: true }));
      nameInput.dispatchEvent(new Event('change', { bubbles: true }));
      redirectInput.value = ${redirectUri};
      redirectInput.dispatchEvent(new Event('input', { bubbles: true }));
      redirectInput.dispatchEvent(new Event('change', { bubbles: true }));

      for (const checkbox of Array.from(form.querySelectorAll('input[type=checkbox]'))) {
        const label = (checkbox.closest('label') || checkbox.parentElement || form).innerText || '';
        if (/机密客户端|confidential/i.test(label) && checkbox.checked) {
          checkbox.click();
        }
      }

      const submit = form.querySelector('button[type=submit], input[type=submit], button.primary, button');
      if (!submit) return '未找到创建按钮。';
      submit.click();
      return 'submitted';
    })()`;
  }

  private unquoteJsResult(result: string): string {
    try {
      const value = JSON.parse(result) as Object;
      return typeof value === 'string' ? value : result;
    } catch (_) {
      return result;
    }
  }

  private handleOAuthUrl(url: string): boolean {
    if (!url.startsWith(GiteaConfig.REDIRECT_URI)) {
      return false;
    }
    const code = this.queryValue(url, 'code');
    const state = this.queryValue(url, 'state');
    const error = this.queryValue(url, 'error');
    if (error.length > 0) {
      this.loginMessage = error === 'server_error'
        ? 'OAuth 授权失败：server_error；如果刚升级权限，请返回配置页点“一键创建”重建 client_id。'
        : `OAuth 授权失败：${error}`;
      return true;
    }
    if (code.length === 0) {
      this.loginMessage = 'OAuth 回调缺少 code。';
      return true;
    }
    if (state !== this.oauthState) {
      this.loginMessage = 'OAuth state 不匹配，请重新授权。';
      return true;
    }
    void this.finishOAuth(code);
    return true;
  }

  private async finishOAuth(code: string): Promise<void> {
    this.loading = true;
    this.loginMessage = '';
    try {
      const token = await this.client.exchangeOAuthCode(this.clientIdInput.trim(), code, this.oauthVerifier);
      await this.authStore.saveAccessToken(token);
      this.client.setAccessToken(token);
      this.screen = 'monitor';
      await this.refreshSummary(true);
    } catch (error) {
      this.screen = 'oauth';
      this.loginMessage = `换取 access token 失败：${this.errorMessage(error)}`;
    } finally {
      this.loading = false;
    }
  }

  private buildAuthorizeUrl(clientId: string, state: string, verifier: string): string {
    const parts = [
      `client_id=${encodeURIComponent(clientId)}`,
      `redirect_uri=${encodeURIComponent(GiteaConfig.REDIRECT_URI)}`,
      `response_type=code`,
      `scope=${encodeURIComponent(GiteaConfig.OAUTH_SCOPE)}`,
      `state=${encodeURIComponent(state)}`,
      `code_challenge=${encodeURIComponent(verifier)}`,
      `code_challenge_method=plain`
    ];
    return `${GiteaConfig.OAUTH_AUTHORIZE_URL}?${parts.join('&')}`;
  }

  private queryValue(url: string, key: string): string {
    const queryIndex = url.indexOf('?');
    if (queryIndex < 0) {
      return '';
    }
    const fragmentIndex = url.indexOf('#', queryIndex);
    const query = fragmentIndex < 0 ? url.substring(queryIndex + 1) : url.substring(queryIndex + 1, fragmentIndex);
    for (const pair of query.split('&')) {
      const equals = pair.indexOf('=');
      const name = equals < 0 ? pair : pair.substring(0, equals);
      if (decodeURIComponent(name) === key) {
        const rawValue = equals < 0 ? '' : pair.substring(equals + 1);
        return decodeURIComponent(rawValue.replace(/\+/g, ' '));
      }
    }
    return '';
  }

  private randomToken(length: number): string {
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~';
    let value = '';
    for (let index = 0; index < length; index++) {
      value += alphabet.charAt(Math.floor(Math.random() * alphabet.length));
    }
    return value;
  }

  private async refreshSummary(userInitiated: boolean): Promise<void> {
    const prNumber = this.applyPrInput();
    this.loading = true;
    this.status = userInitiated ? `正在刷新 PR #${prNumber}…` : '自动刷新中…';
    try {
      const summary = await this.client.fetchSummary(prNumber);
      this.renderSummary(summary);
      if (this.monitoring) {
        this.scheduleMonitorTimer();
      }
    } catch (error) {
      if (error instanceof AuthRequiredError) {
        this.monitoring = false;
        await this.authStore.clearAccessToken();
        this.client.setAccessToken('');
        this.screen = this.clientIdInput.trim().length > 0 ? 'oauth' : 'setup';
        this.loginMessage = 'OAuth 授权过期，请重新授权。';
      } else {
        this.status = `刷新失败：${this.errorMessage(error)}`;
        if (this.monitoring) {
          this.scheduleMonitorTimer();
        }
      }
    } finally {
      this.loading = false;
    }
  }

  private async postCiCommand(command: string): Promise<void> {
    const prNumber = this.applyPrInput();
    this.postingCommand = true;
    this.status = `正在评论 ${command} 到 PR #${prNumber}…`;
    try {
      await this.client.postIssueComment(prNumber, command);
      this.status = `已评论 ${command}，正在刷新队列…`;
      promptAction.showToast({ message: `已发送 ${command}` });
      setTimeout(() => {
        void this.refreshSummary(true);
      }, 1500);
    } catch (error) {
      if (error instanceof AuthRequiredError) {
        this.monitoring = false;
        await this.authStore.clearAccessToken();
        this.client.setAccessToken('');
        this.screen = this.clientIdInput.trim().length > 0 ? 'oauth' : 'setup';
        this.loginMessage = '需要 write:issue 权限，请重新授权后再发送 CI 命令。';
      } else {
        this.status = `发送 ${command} 失败：${this.errorMessage(error)}`;
      }
    } finally {
      this.postingCommand = false;
    }
  }

  private renderSummary(summary: PrSummary): void {
    this.title = `PR #${summary.pr.number}: ${summary.pr.title}`;
    this.meta = `${summary.pr.author} · ${summary.pr.headRef} -> ${summary.pr.baseRef} · ${summary.pr.state}`;
    this.status = summary.gates.length === 0
      ? `没有关键失败；成功门禁已隐藏 · ${this.compactTime(summary.fetchedAt)}`
      : `发现 ${summary.gates.length} 个关键非成功门禁 · ${this.compactTime(summary.fetchedAt)}`;
    this.gates = summary.gates;
    this.queue = summary.queue;
    this.body = summary.pr.body;
  }

  private toggleMonitoring(): void {
    this.monitoring = !this.monitoring;
    if (this.monitoring) {
      void this.refreshSummary(true);
    } else {
      this.clearMonitorTimer();
      this.status = '已暂停自动刷新';
    }
  }

  private scheduleMonitorTimer(): void {
    this.clearMonitorTimer();
    this.monitorTimer = setTimeout(() => {
      void this.refreshSummary(false);
    }, REFRESH_INTERVAL_MS);
  }

  private clearMonitorTimer(): void {
    if (this.monitorTimer >= 0) {
      clearTimeout(this.monitorTimer);
      this.monitorTimer = -1;
    }
  }

  private applyPrInput(): number {
    const parsed = Number.parseInt(this.prText.trim(), 10);
    if (Number.isFinite(parsed) && parsed > 0) {
      return parsed;
    }
    this.prText = '1222';
    return 1222;
  }

  private async openUrl(url: string): Promise<void> {
    if (url.length === 0) {
      return;
    }
    try {
      await this.context.startAbility({
        action: 'ohos.want.action.viewData',
        uri: url
      });
    } catch (_) {
      promptAction.showToast({ message: '没有可用浏览器' });
    }
  }

  private compactTime(value: string): string {
    if (value.length < 19) {
      return '刚刚刷新';
    }
    return `刷新于 ${value.substring(11, 19)}`;
  }

  private errorMessage(error: Object): string {
    if (error instanceof Error) {
      return error.message;
    }
    return JSON.stringify(error);
  }

  private firstNonEmpty(first: string, second: string): string {
    return first.length === 0 ? second : first;
  }
}
