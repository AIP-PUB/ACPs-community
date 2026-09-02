"""ACPs inventory 地址派生 filters."""

from __future__ import annotations

from typing import Any


class FilterModule:
    def filters(self) -> dict[str, Any]:
        return {
            "acps_group_hosts": acps_group_hosts,
            "acps_group_primary": acps_group_primary,
            "acps_group_addr": acps_group_addr,
            "acps_pg_url": acps_pg_url,
            "acps_http_url": acps_http_url,
            "acps_urlquote": acps_urlquote,
            "acps_compose_extra_hosts": acps_compose_extra_hosts,
        }


def _inventory_hosts(hostvars: dict, groups: dict, group: str) -> list[str]:
    return list(groups.get(group, []) or [])


def acps_group_hosts(groups: dict, group: str) -> list[str]:
    return list(groups.get(group, []) or [])


def acps_group_primary(groups: dict, group: str) -> str:
    hosts = acps_group_hosts(groups, group)
    if len(hosts) != 1:
        raise ValueError(
            f"component group {group!r} must have exactly 1 host in V1, got {len(hosts)}: {hosts}"
        )
    return hosts[0]


def acps_group_addr(hostvars: dict, groups: dict, group: str, *, managed: bool = True, external_host: str | None = None) -> str:
    """返回组件组 G 的 advertise 地址."""
    if not managed:
        if not external_host:
            raise ValueError(f"group {group!r} is unmanaged but *_external_host is empty")
        return external_host
    primary = acps_group_primary(groups, group)
    hv = hostvars.get(primary, {})
    return hv.get("acps_advertise_host") or hv.get("ansible_host") or primary


def acps_pg_url(
    user: str,
    password: str,
    host: str,
    port: int | str,
    db: str,
    *,
    driver: str = "postgresql+asyncpg",
) -> str:
    from urllib.parse import quote_plus

    return f"{driver}://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


def acps_urlquote(value: Any) -> str:
    """URL-encode for userinfo / query（密码中的 # @ : / ? 等）。"""
    from urllib.parse import quote

    return quote(str(value), safe="")


def acps_http_url(host: str, port: int | str, *, scheme: str = "http", path: str = "") -> str:
    path = path or ""
    if path and not path.startswith("/"):
        path = "/" + path
    return f"{scheme}://{host}:{port}{path}"


def _host_ipv4(hv: dict) -> str | None:
    """尽力从 hostvars 条目获取 IPv4（facts、字面 ansible_host，或解析 FQDN）。"""
    ipv4 = hv.get("ansible_default_ipv4") or {}
    if isinstance(ipv4, dict):
        addr = ipv4.get("address")
        if addr:
            return str(addr)
    ah = str(hv.get("ansible_host") or "").strip()
    # 仅字面 IPv4 — FQDN 需要 gathered facts 或下方解析。
    if ah and all(p.isdigit() and 0 <= int(p) <= 255 for p in ah.split(".")) and ah.count(".") == 3:
        return ah
    # Control-node DNS /hosts 解析 advertise / ansible_host FQDN（多机 image 必需）。
    for candidate in (
        hv.get("acps_advertise_host"),
        hv.get("ansible_host"),
    ):
        name = str(candidate or "").strip()
        if not name or name in ("127.0.0.1", "localhost", "::1"):
            continue
        if all(p.isdigit() and 0 <= int(p) <= 255 for p in name.split(".")) and name.count(".") == 3:
            return name
        try:
            import socket

            return socket.gethostbyname(name)
        except OSError:
            continue
    return None


def acps_compose_extra_hosts(hostvars: dict, groups: dict) -> list[str]:
    """Compose extra_hosts entries so containers can resolve peer advertise FQDNs.

    Docker embedded DNS does not use the host /etc/hosts. Multi-host installs
    inject advertise_host:ipv4 for every inventory business host that has facts.
    Loopback advertise addresses are skipped (single-node local path).
    """
    seen_names: set[str] = set()
    out: list[str] = []
    for group_hosts in (groups or {}).values():
        for h in group_hosts or []:
            if h in ("localhost", "127.0.0.1"):
                continue
            hv = hostvars.get(h, {}) or {}
            ip = _host_ipv4(hv)
            if not ip or ip in ("127.0.0.1", "::1"):
                continue
            names: list[str] = []
            adv = hv.get("acps_advertise_host") or hv.get("ansible_host") or h
            for name in (adv, h, hv.get("ansible_host")):
                if not name:
                    continue
                name_s = str(name).strip()
                if not name_s or name_s in ("127.0.0.1", "localhost", "::1"):
                    continue
                # 跳过纯 IP 名称（与地址冗余）。
                if name_s == ip:
                    continue
                if name_s not in names:
                    names.append(name_s)
            for name_s in names:
                if name_s in seen_names:
                    continue
                seen_names.add(name_s)
                out.append(f"{name_s}:{ip}")
    return out
