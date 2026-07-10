# TaiChu PR Monitor Harmony

HarmonyOS native ArkTS app for monitoring TaiChu Gitea pull requests.

## What It Does

- Uses Gitea OAuth2 public-client authorization with PKCE.
- Stores the OAuth `client_id` and access token in app preferences.
- Calls `https://taichu.fun/gitea/api/v1` with the OAuth access token.
- Supports arbitrary PR numbers.
- Shows only:
  - latest useful queue clues;
  - PR body;
  - latest non-success signal for:
    - `protected-file-approval`
    - `taichu/codex-pr-review`
    - `taichu/pr-build`
    - `taichu/dev-cloud-preflight`
    - `ci/merge-gate`
- Hides successful gates.
- Supports foreground monitoring with a 60 second refresh interval.
- Sends PR comments for quick CI actions:
  - `rebuild` posts `/ci build`;
  - `remerge` posts `/ci merge`.

## Open In DevEco Studio

1. Open DevEco Studio.
2. Choose **Open Project**.
3. Select this folder:

```text
harmony
```

4. Let DevEco sync Hvigor and OpenHarmony SDK settings.
5. Connect your HarmonyOS phone and run the `entry` module.

## Build

From DevEco Studio, use **Build HAP(s)/APP(s)**.

This machine's DevEco Studio bundle includes HarmonyOS 6.1.1 Release SDK
(`Ohos_sdk_public 6.1.1.125`, API 24). Command line build works with:

```bash
cd harmony
PATH="/Applications/DevEco-Studio.app/Contents/tools/node/bin:/Applications/DevEco-Studio.app/Contents/tools/ohpm/bin:/Applications/DevEco-Studio.app/Contents/tools/hvigor/bin:$PATH" \
DEVECO_SDK_HOME="/Applications/DevEco-Studio.app/Contents/sdk" \
hvigorw --mode module -p module=entry@default -p product=default assembleHap --no-daemon
```

The unsigned HAP is generated at:

```text
entry/build/default/outputs/default/entry-default-unsigned.hap
```

On a HarmonyOS phone, enable developer mode and connect the device in DevEco
Studio, then run the `entry` module. DevEco will sign and install the debug HAP
for the connected device.

## OAuth Setup

The first launch asks for a Gitea OAuth `client_id`.

1. Tap **一键创建**.
2. The app opens Gitea settings, fills an OAuth2 public client, and stores the generated `client_id`.
3. Approve the OAuth authorization page when Gitea asks.

The app uses this redirect URI:

```text
http://127.0.0.1:43122/oauth
```

The loopback URL is intercepted inside the WebView before navigation; the app
does not start a local HTTP server.

If automatic creation is blocked by a Gitea page change, create the OAuth2 app
manually with the same redirect URI, uncheck **机密客户端**, paste `client_id`,
and tap **授权**.

The app then requests `read:repository write:issue read:user` and uses the issued
access token for API calls. `write:issue` is needed only for posting `/ci build`
and `/ci merge` comments.

## Current Boundary

This app monitors while open. True background monitoring can be added later with
notification permission handling and a foreground task/background worker strategy.

## Private Installation Without Store Publishing

HarmonyOS commercial phones can use the signed HAP by USB side-loading:

```bash
/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc list targets
/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc -t <device-id> install -r entry/build/default/outputs/default/entry-default-signed.hap
```

The phone must enable developer mode/USB debugging. For a team, distribute the
signed HAP plus a small install script; each user connects the phone once.

Android users can use the sibling `android/` client. The practical non-store
route is a signed release APK side-load:

```bash
adb install -r taichu_pr_monitor-release.apk
```

Users can also open the APK file on the phone after enabling installation from
unknown sources. This avoids public publishing, but the APK must still be signed
with a stable private signing key so future updates install over the old version.
