# SciToday 后端上云部署(Linux + systemd)

替代原 Windows 托盘看守(`SciToday_admin.exe`),用 systemd 实现开机自启 + 崩溃自动重启。

本目录三个文件:
- `scitoday.service` —— systemd 服务单元
- `scitoday.env` —— 所有 `RSSAI_*` 环境变量(已按代码实际读取项核对)
- `README_DEPLOY.md` —— 本文件

---

## 约定的服务器路径

以下步骤假设你把包放成这样(可改,但要和 `.service` / `.env` 里的路径一致):

```
/opt/scitoday/
├── SciToday_Server/  ← 本包的 SciToday_Server/ 内容(serve.py 等源码)
│   └── deploy/       ← 这三个文件
├── ServerData/       ← 本包的 ServerData/(你的 68 个数据库 + 11 租户, 别丢!)
└── venv/             ← 服务器上新建的 Python 虚拟环境
```

## 部署步骤

```bash
# 1) 上传并就位(在服务器上, 假设包已传到 /tmp/SciToday_Server_CHD)
sudo mkdir -p /opt/scitoday
sudo cp -r /tmp/SciToday_Server_CHD/SciToday_Server /opt/scitoday/SciToday_Server
sudo cp -r /tmp/SciToday_Server_CHD/ServerData /opt/scitoday/ServerData

# 2) 建专用运行账户(非 root)
sudo useradd --system --home /opt/scitoday --shell /usr/sbin/nologin scitoday

# 3) 建虚拟环境并装依赖(Windows 的 .venv 用不了, 必须在 Linux 重建)
sudo python3 -m venv /opt/scitoday/venv
sudo /opt/scitoday/venv/bin/pip install --upgrade pip
sudo /opt/scitoday/venv/bin/pip install -r /opt/scitoday/SciToday_Server/requirements.txt

# 4) 设置最小权限
# /opt/scitoday、源码和 venv 由 root 持有，服务账户只能读取，不能篡改程序。
sudo chown root:root /opt/scitoday
sudo chmod 755 /opt/scitoday
sudo chown -R root:root /opt/scitoday/SciToday_Server /opt/scitoday/venv
sudo chmod -R u=rwX,go=rX /opt/scitoday/SciToday_Server /opt/scitoday/venv

# 环境文件和兼容配置可能含敏感值：仅 root 和 scitoday 组可读。
sudo chown root:scitoday \
  /opt/scitoday/SciToday_Server/deploy/scitoday.env \
  /opt/scitoday/SciToday_Server/config.json
sudo chmod 640 \
  /opt/scitoday/SciToday_Server/deploy/scitoday.env \
  /opt/scitoday/SciToday_Server/config.json

# 若尚未删除旧 frp SSH 私钥，至少确保服务账户和其他本机用户不可读。
sudo find /opt/scitoday/SciToday_Server/frp/ssh -type f ! -name '*.pub' \
  -exec chmod 600 {} + 2>/dev/null || true

# 只有 ServerData 交给服务账户读写；其中包含 token、API Key、数据库和用户数据。
sudo chown -R scitoday:scitoday /opt/scitoday/ServerData
sudo find /opt/scitoday/ServerData -type d -exec chmod 700 {} +
sudo find /opt/scitoday/ServerData -type f -exec chmod 600 {} +

# 5) 改 scitoday.env: 把 RSSAI_TRUSTED_HOSTS 换成你的真实域名(关键!)
sudo nano /opt/scitoday/SciToday_Server/deploy/scitoday.env

# 6) 装并启用服务
sudo cp /opt/scitoday/SciToday_Server/deploy/scitoday.service /etc/systemd/system/scitoday.service
# 先让 systemd 检查未知指令、错误 section 和路径语法，再加载服务。
sudo systemd-analyze verify /etc/systemd/system/scitoday.service
sudo systemctl daemon-reload
sudo systemctl enable --now scitoday

# 7) 验证
systemctl status scitoday --no-pager
curl -s http://127.0.0.1:5201/healthz        # 期望: {"ok": true}
journalctl -u scitoday -n 50 --no-pager      # 看启动日志
systemd-analyze security scitoday.service     # 查看 systemd 沙箱评分和未启用项
sudo -u scitoday test ! -w /opt/scitoday/SciToday_Server
sudo -u scitoday test ! -w /opt/scitoday/venv
sudo -u scitoday test -w /opt/scitoday/ServerData
```

## 日常运维命令

```bash
sudo systemctl restart scitoday   # 改代码/配置后重启
sudo systemctl stop scitoday      # 停(SIGTERM, 会等 SQLite 安全落盘, 最长 90s)
journalctl -u scitoday -f         # 实时看日志(替代原来的 server.log 尾巴)
```

## 常见坑(按发生概率排)

1. **控制台/App 打不开、返回 400** —— `RSSAI_TRUSTED_HOSTS` 没填你的域名。代码默认只信任
   `127.0.0.1/localhost`, 经 Nginx 反代进来的域名 Host 不在白名单会被拒。改 `.env` 第 5 步。

2. **起不来、日志报找不到数据/路径像 /storage/emulated/0** —— `RSSAI_RUNTIME` 没设成 `pc`,
   被当成了 Termux(安卓)。确认 `.env` 里 `RSSAI_RUNTIME=pc`。

3. **数据是空的/租户不见了** —— `RSSAI_SERVER_DATA_DIR` 指错了, 没指向你上传的 ServerData。
   确认 `.env` 里路径 = `/opt/scitoday/ServerData`, 且该目录下有 `control/ tenants/ shared/`。

4. **Nginx 502** —— Nginx 的 `proxy_pass` 端口要和 `RSSAI_SERVER_PORT` 一致。
   包里 `frp/nginx/rssaipush.conf` 已改为 `127.0.0.1:5201`(与本包 `RSSAI_SERVER_PORT=5201` 对齐),
   直接可用。若你改了后端端口, 记得同步改这里。

## 与 Nginx / 域名的关系

后端只监听 `127.0.0.1:5201`(见 `.env`)。对外的 HTTPS 由 Nginx 负责:
`443 → 反代 → 127.0.0.1:5201`。Nginx 配置可参考包里 `frp/nginx/rssaipush.conf`(把 15201 改成 5201)。
证书用 certbot 申请。安全组放行 443。

## 安全提醒

- 包内 `config.json` 含明文 DeepSeek API Key —— 上云 OK, 但别把这个包推到公开 git。
- 包内 `frp/ssh/` 有 SSH 私钥 —— **不该随应用上云**, 建议部署前删掉或单独保管。
- 后端上云后, frp 内网穿透不再需要(云服务器有公网 IP), 相关 frp 二进制/配置可停用。
