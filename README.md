# ExpressVPN SOCKS5 网关

在一个 Docker 容器里运行 ExpressVPN 和 SOCKS5 代理。通过 Web 管理界面登录 ExpressVPN、选择节点和检测代理出口。镜像使用 ExpressVPN Linux 客户端 14.3.1。

## 准备

- 使用装有 Docker 和 OpenSSL 的 x86-64 Linux 主机。Docker 需要能向容器提供 `/dev/net/tun` 和 `NET_ADMIN`。
- 准备一个有效的 ExpressVPN 账户。镜像可从 GHCR 匿名拉取。

## 启动

在存放部署文件的目录生成 SOCKS5 密码。已有密码文件会保留。使用 Docker Compose 时，在仓库的项目目录执行这一步。

```sh
umask 077
mkdir -p .secrets
test -e .secrets/socks-password || openssl rand -hex 32 > .secrets/socks-password
```

### Docker

```sh
docker run -d --pull always --name expressvpn-socks --init --restart unless-stopped \
  --cap-add NET_ADMIN --device /dev/net/tun:/dev/net/tun \
  --publish "${WEB_BIND:-127.0.0.1}:${WEB_PORT:-8080}:8080" \
  --publish "${SOCKS_BIND:-0.0.0.0}:${SOCKS_PORT:-1080}:1080" \
  --env "SOCKS_USER=${SOCKS_USER:-proxy}" \
  --env SOCKS_PASSWORD_FILE=/run/secrets/socks-password \
  --env "SOCKS_PUBLISHED_PORT=${SOCKS_PORT:-1080}" --env "SOCKS_DNS=${SOCKS_DNS:-}" \
  --mount "type=bind,src=$PWD/.secrets/socks-password,dst=/run/secrets/socks-password,readonly" \
  --mount type=volume,src=expressvpn-socks-settings,dst=/opt/expressvpn/etc \
  --mount type=volume,src=expressvpn-socks-data,dst=/opt/expressvpn/var \
  ghcr.io/codexspacelab-projects/expressvpn-socks:latest
```

### Docker Compose

首次部署时获取项目文件。

```sh
git clone https://github.com/CodexSpaceLab-Projects/expressvpn-socks.git
cd expressvpn-socks
```

在项目目录按上面的步骤生成密码，再启动网关。

```sh
docker compose up -d --pull always
```

## 登录和连接

1. 打开 `http://127.0.0.1:8080`，即可使用 Web 管理界面。可以访问该端口的设备均可管理 VPN。
2. 在 **ExpressVPN 账户** 面板粘贴账户后台「设置设备 → Linux」中的激活码或登录令牌。支持邮箱密码的账户也可切换到 **邮箱密码**。邮箱验证码用于登录账户后台。 [查找激活码或登录令牌](https://www.expressvpn.com/support/manage-account/find-activation-code/)
3. 按地区、国家或节点名称筛选，选择节点后点击 **连接**。切换时页面显示目标节点，等官方客户端确认实际连接后更新状态。等待期间可点击 **断开连接** 取消当前请求。

ExpressVPN 登录会话存放在 Docker 数据卷中。登录表单通过内存文件向官方 CLI 传递凭据，命令结束后删除该文件。

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

管理端口默认仅绑定到本机，SOCKS5 端口默认发布到所有 IPv4 接口。仅在本机使用代理时，将 `SOCKS_BIND` 设为 `127.0.0.1`。可用 `WEB_BIND`、`WEB_PORT`、`SOCKS_PORT` 和 `SOCKS_USER` 修改绑定地址、端口和用户名。使用 Docker 时在启动命令前设置这些变量，使用 Compose 时也可在项目目录的 `.env` 中设置。若代理仍在等待 VPN DNS，可用 `SOCKS_DNS` 指定隧道内可达的 DNS IP。

## 查看状态、停止和更新

使用 `docker run` 启动后，可用下面的命令查看容器、日志和服务状态。

```sh
docker ps -a --filter name=expressvpn-socks
docker logs --tail=100 expressvpn-socks
curl -fsS http://127.0.0.1:8080/healthz
```

`/healthz` 返回 `ready` 表示 ExpressVPN 服务已就绪。VPN 是否已经连接以及连接到了哪个节点，可在管理页面查看。

- 使用 Docker 时，运行 `docker stop expressvpn-socks` 停止服务，运行 `docker start expressvpn-socks` 恢复服务。更新时停止并移除旧容器，再运行上面的 `docker run` 命令。从双容器版升级时，也要停止并移除 `expressvpn-socks-ingress`，以释放两个端口。
- 使用 Compose 时，运行 `docker compose logs expressvpn` 查看日志，运行 `docker compose stop` 停止服务。更新仓库文件后，运行 `docker compose up -d --pull always --remove-orphans` 拉取镜像并移除旧版的转发容器。

更新时保留 `.secrets/socks-password` 和现有 Docker 数据卷，即可继续使用原来的代理密码和 ExpressVPN 登录会话。
