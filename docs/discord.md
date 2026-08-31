# Discord通知の設定手順

news-bot(日本語ニュースの日次通知bot)をDiscordに通知させるための、手作業での準備手順です。
Bot本体の実装(`src/notify_discord.py`など)は完成しているので、ここではDiscord側の設定・
GitHubへのリポジトリ作成・Secretsへの登録という、コードでは自動化できない部分だけを扱います。

**このリポジトリはまだGitHub上にありません。** ローカルの`task-news-bot`フォルダに
コミットがあるだけの状態です。SecretsはGitHub上のリポジトリにしか登録できず、
ワークフローもpushされて初めてActionsタブに現れるため、**手順7でリポジトリを作成します**。
手順8以降で「このリポジトリ」と書かれているのは、すべて手順7で作るリポジトリのことです。

この方式はDiscord Bot TokenによるREST API送信のみを使います。Gateway(WebSocket)への
常時接続は行わないため、GitHub Actions上でcron実行するだけで動作します。常駐サーバーを
用意する必要はありません。

news-botは**1レーンにつき1メッセージ・1embed**で通知します。`config.yml`のレーン構成上、
毎朝「総合」1通・「テック・科学」1通の合計2通が届きます(arxiv-botのような1論文1メッセージ
ではありません)。これに加えて天気予報機能により、ニュースより先に天気1通が届くため、
**毎朝の通知は合計3通**(天気1通 + ニュース2通)になります。天気の取得元(Open-Meteo・
気象庁)はどちらもAPIキー不要・無料なので、天気機能のために新しいSecretsを追加する
必要はありません。また、news-botにフィードバック学習機能は無いため、投稿後に📖👍👎のような
リアクションを付ける処理も、それを回収する別ワークフローもありません。

必要なGitHub Secretsは次の3つです。

- `DISCORD_BOT_TOKEN`
- `DISCORD_CHANNEL_ID`
- `GEMINI_API_KEY`

任意で `GEMINI_MODEL` も設定できます(未設定なら`config.yml`の`gemini_model`が使われます)。

以下、上から順に進めてください。

## 1. Discord Developer PortalでApplicationを作成する

[Discord Developer Portal](https://discord.com/developers/applications) を開き、
右上の「New Application」をクリックします。任意の名前(例: `news-bot`)を入力して
「Create」を押してください。

arxiv-bot用のApplicationは既にありますが、**news-bot用には新規に作成することを
推奨します**。1つのTokenを使い回すと、どちらか一方のTokenを失効させて再発行した
とき(漏洩時の対応など)、もう一方のbotまで巻き添えで止まってしまうためです。

> **こうなればOK**: 作成したApplication名がページ上部に表示され、左側に
> General Information / Bot / OAuth2 などのメニューが並んだ設定画面に移動します。

## 2. Botを作成してTokenを発行する

左メニューの「Bot」を開き、「Reset Token」(初回は「Add Bot」の場合もあります)を
クリックします。確認ダイアログが出たら進めてください。表示された「Token」欄の
「Copy」ボタンでTokenをコピーし、あとで使うまで安全な場所に一時保存してください。

Tokenはこの画面でしか表示されません。閉じてしまったり見失ったりした場合は、
再度「Reset Token」で新しいTokenを発行する必要があります(古いTokenは無効になります)。

> **こうなればOK**: 「Token」欄に伏せ字ではない実際のTokenの文字列が表示され、
> 「Copy」ボタンでコピーできる状態になります。

同じ「Bot」画面にある「Privileged Gateway Intents」(MESSAGE CONTENT INTENTなど)は
**すべてOFFのままで構いません**。news-botの実装はREST APIでの送信のみでGatewayに
接続しないため、これらのIntentは不要です(arxiv-botと同じ理由です)。

> **こうなればOK**: 「PRESENCE INTENT」「SERVER MEMBERS INTENT」
> 「MESSAGE CONTENT INTENT」の3つのトグルがいずれもOFF(グレー)のままになっています。

## 3. 通知先チャンネルを用意する

news-bot専用に新しいサーバーを作る必要はありません。**arxiv-bot用に既に作った
サーバーに、通知先チャンネルを1つ追加するだけで構いません。** 同じサーバーに
複数のbotを招待することは問題なくできます。

Discordアプリ(またはブラウザ版)で、左端のサーバー一覧からarxiv-bot用サーバーの
アイコンを右クリックし、「チャンネルを作成」を選びます。チャンネルの種類は
「テキスト」のまま、任意の名前(例: `#news`)を入力して「チャンネルを作成」を
押してください。

> **こうなればOK**: arxiv-bot用サーバーのチャンネル一覧に、新しいテキスト
> チャンネル(例: `#news`)が追加されて表示されます。

## 4. OAuth2 URL GeneratorでBotの招待URLを作る

Developer Portalの左メニューから「OAuth2」→「URL Generator」を開きます。
「SCOPES」で `bot` にチェックを入れます。

> **こうなればOK**: チェックを入れると、その下に「BOT PERMISSIONS」という
> セクションが新しく表示されます。

続けて「BOT PERMISSIONS」で以下の2つだけにチェックを入れてください。

- View Channel
- Send Messages

news-botはメッセージを送るだけで、過去のメッセージを読み返したりリアクションを
付けたりしないため、必要な権限はこの2つだけです。arxiv-botの手順書ではここに
Read Message HistoryとAdd Reactionsも含めていますが、news-botにはリアクションを
回収する仕組み自体が無いので不要です。管理者権限やメッセージ削除・チャンネル
管理などの権限も不要なので、チェックを入れないでください。

> **こうなればOK**: ページ下部に生成されたURL(`https://discord.com/oauth2/authorize?...`)
> が表示され、「Copy」ボタンでコピーできる状態になっています。

コピーしたURLをブラウザで開きます。

> **こうなればOK**: 「どのサーバーに追加しますか?」という画面が表示され、
> ドロップダウンでarxiv-bot用サーバー(手順3でチャンネルを追加したサーバー)を
> 選べる状態になります。

サーバーを選択して「認可」をクリックします。

> **こうなればOK**: 「〇〇が認証されました」という完了画面が表示され、
> サーバーのメンバー一覧にnews-bot用のbotが(arxiv-bot用のbotとは別に)
> 追加されています。

## 5. 通知先チャンネルのIDを調べる

Discordの「ユーザー設定」(左下の歯車アイコン)→「詳細設定」を開き、
「開発者モード」をONにします。arxiv-bot設定時に既にONにしている場合はこの
手順は不要です。

> **こうなればOK**: 「開発者モード」のトグルがON(色が付いた状態)になります。

手順3で作成した通知先チャンネル(例: `#news`)を右クリックし、
「チャンネルIDをコピー」を選択します。

> **こうなればOK**: 右クリックメニューの一番下あたりに「チャンネルIDをコピー」が
> 表示され、クリックすると数字の羅列(チャンネルID)がクリップボードにコピーされます。

## 6. Gemini APIキーを取得する

news-botは要約の生成に既存のGemini APIを使います。arxiv-bot用に既にAPIキーを
発行済みであれば、そのキーを流用しても構いません(Gemini APIキーはbotごとに
分ける必要はありません)。まだ持っていない場合は、[Google AI Studio](https://aistudio.google.com/apikey)
を開き、「Create API key」をクリックしてください。

> **こうなればOK**: `AIza`から始まるAPIキーの文字列が表示され、コピーできる
> 状態になります。

無料枠(1日あたりのリクエスト数の上限)で足ります。news-botは1日1回、全トピックを
まとめて1リクエストで要約するため、無料枠のRPD(1日あたりのリクエスト数)を
消費する心配はほぼありません。

`GEMINI_API_KEY`は**必須ではありません**。未設定のままでもnews-bot自体は正常に
動作し、通知は届きます。ただしその場合、各トピックはGeminiによる要約無しで、
見出しとリンクだけの通知になります。まずは要約無しで動作確認だけ済ませ、
あとからキーを追加する、という進め方でも問題ありません。

## 7. GitHubにリポジトリを作成してpushする

次の手順でSecretsを登録する「リポジトリ」とは、**このnews-botのリポジトリを
GitHub上に作ったもの**を指します。現時点ではまだローカル(`task-news-bot`
フォルダ)にコミットがあるだけで、GitHub上には存在しません。Secretsは
GitHub上のリポジトリにしか登録できず、ワークフローもGitHub上にpushされて
初めてActionsタブに現れるため、先にリポジトリを作ります。

`task-news-bot` フォルダで以下を実行してください。arxiv-botに合わせて公開する
場合はこちらです。

```bash
gh repo create news-bot --public --source=. --remote=origin --push
```

非公開にする場合はこちらを使ってください。どちらを選んでもnews-botの動作は
変わりません(APIキーはコードに含まれず、すべてSecretsに置くためです)。

```bash
gh repo create news-bot --private --source=. --remote=origin --push
```

> **こうなればOK**: コマンドの出力に
> `https://github.com/<あなたのユーザー名>/news-bot` というURLが表示され、
> ブラウザでそのURLを開くと `src/` `config.yml` `docs/` などが並んだ
> リポジトリのページが見えます。

pushできたことを確認します。

```bash
git log --oneline origin/main -1
```

> **こうなればOK**: `feat: 日次ニュース通知bot (news-bot) の初期実装` の
> コミットが表示されます。`unknown revision` のようなエラーが出る場合は
> pushが完了していないので、`git push -u origin main` を実行してください。

以降の手順で「このリポジトリ」と書かれている箇所は、すべてここで作成した
`news-bot` リポジトリのことです。

## 8. GitHub Secretsに登録する

手順7で作成したリポジトリのページを開き、「Settings」タブ→左メニューの
「Secrets and variables」→「Actions」を開きます。

> **注意**: 「Settings」タブが見当たらない場合は、自分がオーナーではない
> リポジトリを開いている可能性があります。URLが
> `https://github.com/<あなたのユーザー名>/news-bot` になっているか
> 確認してください。

> **こうなればOK**: 「Repository secrets」という見出しの下に
> 「New repository secret」ボタンが表示されます。

「New repository secret」をクリックし、Name欄に `DISCORD_BOT_TOKEN` を入力、
Secret欄に手順2でコピーしたTokenを貼り付けて「Add secret」を押します。

> **こうなればOK**: Secrets一覧に `DISCORD_BOT_TOKEN` が追加され、
> 値は表示されずマスクされた状態になります。

同様に「New repository secret」をクリックし、Name欄に `DISCORD_CHANNEL_ID` を
入力、Secret欄に手順5でコピーしたチャンネルIDを貼り付けて「Add secret」を押します。

> **こうなればOK**: Secrets一覧に `DISCORD_CHANNEL_ID` が追加されます。

同様に「New repository secret」をクリックし、Name欄に `GEMINI_API_KEY` を入力、
Secret欄に手順6で取得したAPIキーを貼り付けて「Add secret」を押します。

要約無しでまず動作確認したい場合は、この`GEMINI_API_KEY`の登録は後回しにしても
構いません(手順9のdry_run確認は`GEMINI_API_KEY`が無くても実行できます。
このあと手順9で dry_run 実行し、ログを見てから追加しても間に合います)。

> **こうなればOK**: Secrets一覧に `DISCORD_BOT_TOKEN` / `DISCORD_CHANNEL_ID` /
> `GEMINI_API_KEY` の3つが並んで表示されます。

`GEMINI_MODEL`を`config.yml`の`gemini_model`(`gemini-3.6-flash`)と異なるモデルに
差し替えたい場合だけ、同じ手順で `GEMINI_MODEL` というSecretを追加してください。
特に理由が無ければ登録不要です。

> **こうなればOK**(`GEMINI_MODEL`を設定する場合のみ): Secrets一覧に
> `GEMINI_MODEL` が追加されます。

## 9. 動作確認

天気予報の部分だけであれば、Discord Bot Tokenを含む上記のSecrets設定を待たずに
確認できます。天気の情報源(Open-Meteo・気象庁)はどちらもAPIキー不要なので、
`task-news-bot`フォルダでローカルに`DRY_RUN=1 python src/main.py`を実行するだけで、
組み立てられた天気の内容をログで確認できます(詳しい実行方法はREADME.mdの
「ローカルでの実行」を参照してください)。

> **こうなればOK**: ログに`[weather]`で始まる行が出力され、取得できた時間数や
> 気象庁の最高/最低気温が表示されます。Discordへの実際の投稿は行われません。

リポジトリの「Actions」タブを開き、ワークフロー `daily-news`
(`.github/workflows/daily.yml`)を選んで「Run workflow」をクリックします。
`dry_run` に `true` を指定して実行してください。

arxiv-botと違い、**news-botは土日でも新着ゼロにはなりません**(ニュースは
毎日配信されるため、`lookback_hours`の範囲に必ず何かしら記事があります)。
そのため曜日を選ばずいつ実行しても構いません。

> **こうなればOK**: ワークフローが緑のチェックマークで完了し、ログに各レーンの
> embedのtitleとdescription(またはDRY_RUNである旨)が出力されますが、実際の
> Discordチャンネルにはメッセージが投稿されません。

ログの内容(見出しやレーンごとの件数)に問題がなければ、もう一度「Run workflow」を
実行し、今度は `dry_run` を `false`(またはチェックを外した状態)にして実行します。

> **こうなればOK**: 手順3で作成したチャンネルに、「総合」レーンのembedと
> 「テック・科学」レーンのembedが、それぞれ1通ずつ(そのレーンで通知対象が
> 0件のときはそのレーン分は届きません)投稿されます。

**同じ日にもう一度`dry_run: false`で実行すると、2回目はほぼ空(または大幅に
件数が減った状態)になります。** これは失敗ではありません。一度通知したトピックの
URLは`data/seen_urls.json`に記録され、`config.yml`の`seen_ttl_days`(14日間)は
再通知されない仕組みになっているためです。日を変えて再実行するか、動作確認だけ
やり直したい場合は`data/seen_urls.json`の該当エントリを削除してから実行して
ください。

## 10. 定期実行を有効にする際の注意

動作確認が済み、cronによる自動実行を有効にする前に、必ずユーザーの承認を得て
ください(本プロジェクトの規約上、定期実行の本番トリガー有効化は承認制です)。

有効化する際は、GitHub ActionsのcronはGitHubの公式仕様として**指定時刻どおりに
起動する保証が無く、数十分程度遅延することがある**点を踏まえてスケジュールを
組んでください。「毎朝7:00に届く」ことを厳密に期待せず、多少のずれを許容できる
時刻に設定するのが安全です。

> **こうなればOK**: cronのスケジュールをリポジトリのオーナー(ユーザー本人)が
> 確認・承認した上で、ワークフローファイルのcron式が有効化(コメントアウト解除
> など)された状態でpushされています。

## トラブルシューティング

- **401 Unauthorized**: `DISCORD_BOT_TOKEN` が誤っているか失効しています。
  Developer Portalの「Bot」画面で「Reset Token」を行い、新しいTokenを
  GitHub Secretsに登録し直してください。
- **403 Forbidden**: Botに必要な権限(View Channel / Send Messages)が
  付与されていないか、Botがそのチャンネルにアクセスできません。手順4の招待を
  やり直すか、チャンネルの権限設定でBotのロールにアクセスを許可してください。
- **404 Not Found**: `DISCORD_CHANNEL_ID` が誤っています。手順5の方法で
  チャンネルIDを取り直し、Secretsを更新してください。
- **Settingsタブが見当たらない / Actionsタブにワークフローが出てこない**:
  手順7のリポジトリ作成とpushが済んでいません。`git remote -v` を実行して
  `origin` が表示されるか確認してください。何も出なければ未作成です。
- **通知が空になる/件数が少ない**: 上記(手順9)のとおり、同じ日に2回実行すると
  `data/seen_urls.json`により大半のトピックが既報扱いになります。まずは
  `data/seen_urls.json`を確認し、対象URLが記録されていないか確かめてください。
  それでも心当たりが無い場合は、`config.yml`の`min_score`(この点数未満は
  通知しない)が高すぎないか、ログに出る各レーンのクラスタ数・スコアの内訳と
  合わせて確認してください。
- **要約が付かない**: `GEMINI_API_KEY`が未設定、またはGemini API側のエラーが
  続いている場合、通知自体は届きますが要約無し(見出しとリンクのみ)になります。
  ログに`[summarize]`で始まる行が出ていないか確認してください。
- **一部のトピックだけ要約が付かない**: これは仕様です。Yahoo!トピックスは
  見出ししか配信しておらず、他社記事と名寄せできなかったトピックには要約の
  材料になるリード文がありません。見出しだけから要約を書かせると内容を捏造
  するため、そのトピックは意図的にGeminiへ送らず見出しとリンクだけで通知します。
- **天気だけ届かない / ニュースだけ届かない**: 天気とニュースは互いに独立して
  動いており、一方が失敗してももう一方の通知は続行されます。どちらの段で
  止まったかは、Actionsの実行ログで`[weather]`で始まる行と`[fetch_feeds]`で
  始まる行のどちらが出ているか(あるいはエラーで止まっているか)で切り分けて
  ください。`weather.enabled: false`になっていないかも合わせて確認してください。

  > **こうなればOK**: ログの`[weather]`行と`[fetch_feeds]`行の両方が正常に
  > 出力されている場合、片方だけ届かないのは通知(`notify_discord`)側の問題
  > です。どちらか一方の行にエラーが出ている場合は、そちらの取得元
  > (Open-Meteo/気象庁、またはRSSフィード)が原因です。
