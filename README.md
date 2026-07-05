# SciToday

SciToday 是由 Android 阅读端、Python 后端和 Web 管理台组成的个人科研阅读工具。

## 权威源与生成目录

- 当前目录是后端唯一权威源。
- Android 权威源默认位于相邻的 `RssAiPushApp` 目录。
- `SciToday_publish/` 是由构建脚本生成的公开源码树，禁止手工修改。
- `dist/` 是 PC 安装包构建产物。

生成综合发布树：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\installer\build_publish.ps1
```

如 Android 源不在默认相邻目录：

```powershell
.\installer\build_publish.ps1 -AndroidSource D:\src\RssAiPushApp
```

生成结果包含：

```text
SciToday_publish/
  app/          Android Gradle 工程
  pc_backend/   PC/Termux 共用后端源码
```

## 后端运行

复制示例配置并填写自己的密钥和访问令牌：

```powershell
Copy-Item config.example.json config.json
.\start_server_pc.ps1 -InstallDeps
```

PC 启动脚本设置 `RSSAI_RUNTIME=pc`；Termux 启动脚本设置
`RSSAI_RUNTIME=termux`。所有数据库、下载目录和配置路径均可继续通过
`RSSAI_*` 环境变量覆盖。

通知通道由 `RSSAI_NOTIFICATION_CHANNEL` 或
`notifications.channel` 控制，环境变量优先。`auto` 只在 Termux 调用
`termux-notification`，PC 不发系统通知。

`pdf_watch_summarize.py` 仅为一版兼容 CLI 入口。新的调度应调用后端 API 或
`tasks.run_pdf_watch()`。

## PC 安装包

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\installer\build_package.ps1
```

真实 `config.json`、API Key、数据库、日志、私人 OPML、PDF、inbox 和构建缓存
不会进入发布树或安装包。
