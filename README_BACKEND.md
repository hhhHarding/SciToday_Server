# SciToday PC/Termux Backend

本目录同时支持 Windows PC 和 Termux，不维护平台专用的 `tasks.py` 副本。

- `app.py`：Flask API、调度器和 Web 控制台入口。
- `tasks.py`：RSS、PDF、数据库、配置和管理任务的唯一实现。
- `push.py`：按运行环境选择通知通道。
- `pdf_watch_summarize.py`：已弃用的兼容 CLI 壳。
- `admin_web/`：Web 管理台。
- `user_web/`：Vue 公网用户端源码；构建后的 `user_web/dist/` 由 `/user/` 提供。
- `installer/`：Windows 托盘和 ZIP 安装包构建脚本。

从 `config.example.json` 创建本地 `config.json`。不要提交真实配置、数据库、
日志、OPML、PDF 或 inbox 内容。

RSS discovery 默认按 60 分钟回退间隔运行，实际下次抓取会结合 HTTP
`Cache-Control`/`Expires`、RSS `ttl`、连续无更新次数及错误退避动态计算，配置下限为
15 分钟。为通过部分出版社（ACS、Wiley 等）Cloudflare/Atypon 对脚本 UA 的
拦截，请求默认使用真实浏览器 User-Agent；运营者可通过
`RSSAI_RSS_USER_AGENT` 覆盖为包含版本和联系信息的透明客户端标识（如旧默认
`SciTodayRSS/1.0`）。普通 403 采用 1 小时起步的指数退避（上限 24 小时）；
Wiley 任一 RSS 出现 403 时启用出版社级熔断，所有 Wiley RSS 至少暂停 24 小时，
随后指数退避，最长 7 天。

RSS 新条目在共享消化阶段会默认访问一次公网原文链接，依次从论文元数据、
JSON-LD 和页面 abstract 区块提取原文摘要。请求沿用 RSS 的 SSRF/DNS 固定与逐跳
重定向校验，正文最多读取 2 MiB，连接/读取超时为 5/10 秒；任何抓取或解析失败
都会回退到 RSS 自带摘要，不阻断消化和租户投递。可在管理后台关闭
`rss.fetch_original_abstract`。

部署前运行 `user_web\build.ps1`，确保发布包包含
`user_web\dist\index.html`；线上运行不需要 Node.js。
