# Ubuntu運用

本番のコードと状態は `/home/seiya/services/news-bot` に置く。編集用の
checkoutとは分け、日常実行中に `git pull` / `git commit` / `git push` はしない。

## 秘密情報

`/home/seiya/.config/research-bots/news.env` を作り、`deploy/news.env.example`
の4項目を設定する。ディレクトリは `0700`、ファイルは `0600` にする。値を
リポジトリ、バックアップ、journalへ出力しない。

## systemd

- `research-bots-news-collect.timer` — 毎日 00:05, 03:05, ..., 21:05 JST
- `research-bots-news-daily.timer` — 毎日 07:10 JST
- `research-bots-backup.timer` — 毎日 02:30 JST。14世代を保持

timerは `Persistent=false` である。停止中に逃した通知を再起動時に補うと、状態
遷移との組合せで天気・ニュースを二重送信するおそれがあるためである。

```bash
sudo systemctl start research-bots-news-collect.service
sudo systemctl start research-bots-news-daily.service
sudo systemctl stop research-bots-news-collect.timer research-bots-news-daily.timer
sudo systemctl status research-bots-news-collect.timer research-bots-news-daily.timer
journalctl -u research-bots-news-daily.service -n 200 --no-pager
systemctl list-timers --all 'research-bots-*'
```

手動実行もserviceを通す。共有ロックを通らない `python src/main.py` の直接実行は
本番状態を書き換えるため避ける。

## 更新と復元

更新時は対象timerを止め、稼働中serviceの終了を確認してから、バックアップを取り、
専用checkoutのコードを更新する。`data/` は更新で上書きしない。復元は一時ディレクトリ
へ展開して内容とハッシュを確認後、timerを止めた状態で行う。

このローカルバックアップはPC故障には備えられない。秘密情報を公開リポジトリに置かず、
別媒体への暗号化バックアップは別途管理する。
