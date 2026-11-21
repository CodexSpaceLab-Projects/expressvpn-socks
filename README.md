# ExpressVPN SOCKS5 网关

镜像使用 ExpressVPN 官方 Linux 客户端 14.3.1。Web 管理界面通过 `expressvpnctl` 登录、读取节点和切换连接。应用支持输入 ExpressVPN 邮箱密码，也支持账户后台提供的激活码或登录令牌。 [官方 CLI 指南](https://www.expressvpn.com/support/vpn-setup/app-for-linux-cli/) · [官方下载安装页](https://www.expressvpn.com/latest#linux)

## 准备

- 使用装有 Docker 和 OpenSSL 的 x86-64 Linux 主机。Docker 需要能向容器提供 `/dev/net/tun` 和 `NET_ADMIN`。
- 构建时需要访问 Docker Hub、Debian 软件源和 `www.expressvpn.works`。连接 VPN 需要有效的 ExpressVPN 账户。

## 下载镜像

从 [GitHub Releases](https://github.com/CodexSpaceLab-Projects/expressvpn-socks/releases/tag/v14.3.1) 下载 `expressvpn-socks_14.3.1_linux-amd64.tar.gz` 和同名 `.sha256` 文件。在下载目录校验并加载镜像，即可执行下文的 `docker run` 命令。

```sh
sha256sum -c expressvpn-socks_14.3.1_linux-amd64.tar.gz.sha256 &&
docker image load -i expressvpn-socks_14.3.1_linux-amd64.tar.gz
```

有 GHCR 包读取权限的用户也可以直接拉取。在 `docker login ghcr.io` 中使用 GitHub 用户名和带 `read:packages` 权限的 classic PAT 登录。

```sh
docker pull ghcr.io/codexspacelab-projects/expressvpn-socks:14.3.1
```

在 GitHub 的 **Actions → Publish Docker image → Run workflow** 可以手动构建并推送 `linux/amd64` 镜像到 GHCR。

也可以克隆仓库，在项目目录自行构建。随后在同一个终端执行启动命令。

```sh
git clone https://github.com/CodexSpaceLab-Projects/expressvpn-socks.git
cd expressvpn-socks
IMAGE=expressvpn-socks:14.3.1
docker build --platform linux/amd64 --tag "$IMAGE" .
```

## 使用 `docker run` 启动

在一个固定目录运行下面的命令。密码存放在该目录下的 `.secrets/socks-password`，已有文件会保留。两个容器共享自定义 Docker 网络。`expressvpn-socks` 运行 VPN 网关，`expressvpn-socks-ingress` 发布管理和 SOCKS5 端口，并通过该网络转发流量。

```sh
IMAGE=${IMAGE:-ghcr.io/codexspacelab-projects/expressvpn-socks:14.3.1}
WEB_BIND=${WEB_BIND:-127.0.0.1}
WEB_PORT=${WEB_PORT:-8080}
SOCKS_BIND=${SOCKS_BIND:-0.0.0.0}
SOCKS_PORT=${SOCKS_PORT:-1080}
SOCKS_USER=${SOCKS_USER:-proxy}

umask 077
mkdir -p .secrets
test -e .secrets/socks-password || openssl rand -hex 32 > .secrets/socks-password
docker network inspect expressvpn-socks >/dev/null 2>&1 || docker network create expressvpn-socks

docker run -d --name expressvpn-socks --init --restart unless-stopped \
  --network expressvpn-socks --network-alias expressvpn \
  --cap-add NET_ADMIN --device /dev/net/tun:/dev/net/tun \
  --env "SOCKS_USER=$SOCKS_USER" \
  --env SOCKS_PASSWORD_FILE=/run/secrets/socks-password \
  --env "SOCKS_PUBLISHED_PORT=$SOCKS_PORT" --env "SOCKS_DNS=${SOCKS_DNS:-}" \
  --mount "type=bind,src=$PWD/.secrets/socks-password,dst=/run/secrets/socks-password,readonly" \
  --mount type=volume,src=expressvpn-socks-settings,dst=/opt/expressvpn/etc \
  --mount type=volume,src=expressvpn-socks-data,dst=/opt/expressvpn/var \
  "$IMAGE" &&
docker run -d --name expressvpn-socks-ingress --restart unless-stopped \
  --network expressvpn-socks --user 65534:65534 --cap-drop ALL --read-only \
  --publish "$WEB_BIND:$WEB_PORT:8080" --publish "$SOCKS_BIND:$SOCKS_PORT:1080" \
  --entrypoint /usr/bin/python3 "$IMAGE" /app/relay.py expressvpn
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

管理端口默认仅绑定到本机，SOCKS5 端口默认发布到所有 IPv4 接口。仅在本机使用代理时，启动前在终端设置 `SOCKS_BIND=127.0.0.1`。如果应用已连接且代理仍显示等待 VPN DNS，可在启动前用 `SOCKS_DNS` 指定隧道内可达的 DNS IP。

## 查看状态和停止

```sh
docker ps -a --filter name=expressvpn-socks
docker logs --tail=100 expressvpn-socks
docker logs --tail=100 expressvpn-socks-ingress
curl -fsS http://127.0.0.1:8080/healthz
```

`/healthz` 返回 `ready` 表示 ExpressVPN 服务已就绪。VPN 是否已经连接以及连接到了哪个节点，可在管理页面查看。

运行 `docker stop expressvpn-socks-ingress expressvpn-socks` 停止服务。再次启动运行 `docker start expressvpn-socks expressvpn-socks-ingress`。更新镜像时，先停止并用 `docker rm expressvpn-socks-ingress expressvpn-socks` 移除旧容器，再加载新归档或拉取新镜像，并重新运行启动命令。保留数据卷即可继续使用已有的 ExpressVPN 登录会话。
