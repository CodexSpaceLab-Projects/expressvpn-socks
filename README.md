# ExpressVPN SOCKS5 网关

镜像使用 ExpressVPN 官方 Linux 客户端 14.3.1。Web 管理界面通过 `expressvpnctl` 登录、读取节点和切换连接。应用支持输入 ExpressVPN 邮箱密码，也支持账户后台提供的激活码或登录令牌。 [官方 CLI 指南](https://www.expressvpn.com/support/vpn-setup/app-for-linux-cli/) · [官方下载安装页](https://www.expressvpn.com/latest#linux)

## 准备

- 使用装有 Docker 和 Docker Compose v2 的 x86-64 Linux 主机。Docker 需要能向容器提供 `/dev/net/tun` 和 `NET_ADMIN`。
- 构建时需要访问 Docker Hub、Debian 软件源和 `www.expressvpn.works`。连接 VPN 需要有效的 ExpressVPN 账户。

## 安装和启动

克隆仓库，进入项目目录，生成 SOCKS5 密码，再构建并启动网关。已有的密码文件会保留。

```sh
git clone https://github.com/CodexSpaceLab-Projects/expressvpn-socks.git
cd expressvpn-socks
umask 077
mkdir -p .secrets
test -e .secrets/socks-password || openssl rand -hex 32 > .secrets/socks-password
docker compose build expressvpn
docker compose up -d --no-build --pull never
docker compose ps
```

如果手头已有可信的镜像归档 `dist/expressvpn-socks_14.3.1_linux-amd64.tar.gz`，生成密码后，可用 `docker image load -i dist/expressvpn-socks_14.3.1_linux-amd64.tar.gz` 代替构建，再执行启动命令。

## 登录和连接

1. 打开 `http://127.0.0.1:8080`，即可使用 Web 管理界面。可以访问该端口的设备均可管理 VPN。
2. 在 **ExpressVPN 账户** 面板粘贴账户后台「设置设备 → Linux」中的激活码或登录令牌。支持邮箱密码的账户也可切换到 **邮箱密码**。邮箱验证码用于登录账户后台。 [查找激活码或登录令牌](https://www.expressvpn.com/support/manage-account/find-activation-code/)
3. 按地区、国家或节点名称筛选，选择节点后点击 **连接**。切换时页面显示目标节点，等官方客户端确认实际连接后更新状态。等待期间可点击 **断开连接** 取消当前请求。

ExpressVPN 登录会话存放在 Compose 的数据卷中。登录表单通过内存文件向官方 CLI 传递凭据，命令结束后删除该文件。

网关默认使用 Lightway TCP。VPN 连接后，点击 **检测代理出口** 可通过当前 SOCKS5 地址完成一次 HTTPS 检测，并查看出口 IP。

## 代理设置

| 参数 | 默认值 |
| --- | --- |
| 类型 | SOCKS5，支持 TCP CONNECT |
| 地址 | `127.0.0.1`，其他设备使用 Docker 所在机器的地址 |
| 端口 | `1080` |
| 用户名 | `proxy` |
| 密码 | `.secrets/socks-password` 中的值 |

客户端可以使用 `socks5h://127.0.0.1:1080` 或开启 SOCKS5 的远程 DNS。TCP 和 DNS 套接字绑定到 VPN 隧道，切换或断开时关闭活动代理连接。SOCKS5 用户名密码认证适用于可信网络。

连上 VPN 后，可以在 Docker 主机上用 `curl` 检查 SOCKS5 出口。按提示输入 `.secrets/socks-password` 中的密码，响应中的 `ip=` 是代理的出口 IP。

```sh
curl --fail --show-error --max-time 20 \
  --proxy socks5h://127.0.0.1:1080 --proxy-user proxy \
  https://www.cloudflare.com/cdn-cgi/trace
```

Compose 默认将管理端口绑定到回环地址，将 SOCKS5 端口发布到所有 IPv4 接口。仅在本机使用代理时，在 `.env` 中设置 `SOCKS_BIND=127.0.0.1`。也可在 `.env` 中设置 `WEB_BIND`、`WEB_PORT`、`SOCKS_PORT` 或 `SOCKS_USER`。如果应用已连接且代理仍显示等待 VPN DNS，可用 `SOCKS_DNS` 指定隧道内可达的 DNS IP。

Compose 的两个服务使用同一个镜像。`expressvpn` 运行官方客户端、管理页面和 SOCKS5 服务，`ingress` 只发布 TCP 端口，将请求转发给同一 Docker 网络内的 `expressvpn`。这种布局让局域网客户端的回包通过 Docker 网络返回。

## 查看状态和停止

```sh
docker compose ps
docker compose logs --tail=100 expressvpn ingress
curl -fsS http://127.0.0.1:8080/healthz
```

`/healthz` 返回 `ready` 表示 ExpressVPN 服务已就绪。VPN 是否已经连接以及连接到了哪个节点，可在管理页面查看。

运行 `docker compose down` 停止服务。下次运行 `docker compose up -d --no-build --pull never` 时，Compose 会继续使用保存 ExpressVPN 登录会话的数据卷。
