#!/bin/sh
set -eu

url=https://www.expressvpn.works/clients/linux/expressvpn-linux-universal-14.3.1.15429_release.run
sha256=f784af6628564f4fcb0a82064d4dc1f22520c3c917bb1c882bd61eb621b31efb
installer=$(mktemp /tmp/expressvpn.XXXXXX.run)
trap 'rm -f "$installer" /tmp/expressvpn_install.log' EXIT

curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --retry 3 --connect-timeout 15 --max-time 300 --output "$installer" "$url"
printf '%s  %s\n' "$sha256" "$installer" | sha256sum --check -

# The daemon gets its network privileges and TUN device at container startup.
sh "$installer" --nox11 --noprogress -- --skip-service --no-gui --force-dependencies --force-update
export LD_LIBRARY_PATH=/opt/expressvpn/lib
expressvpnctl --version
/opt/expressvpn/bin/expressvpn-daemon --version
install -d -m 0700 /opt/expressvpn/etc /opt/expressvpn/var
