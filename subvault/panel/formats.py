import base64
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit


SUPPORTED_SCHEMES = {"ss", "vmess", "vless", "trojan", "hysteria2", "hy2", "tuic"}


def _b64decode(value: str) -> bytes:
    value = value.strip().replace("-", "+").replace("_", "/")
    value += "=" * (-len(value) % 4)
    return base64.b64decode(value)


def _name_from_fragment(fragment: str, fallback: str) -> str:
    return unquote(fragment) if fragment else fallback


def parse_node(uri: str, fallback_name: str = "节点") -> dict:
    uri = uri.strip()
    scheme = uri.split(":", 1)[0].lower() if ":" in uri else ""
    if scheme not in SUPPORTED_SCHEMES:
        raise ValueError(f"暂不支持 {scheme or '未知'} 协议转换")

    if scheme == "vmess":
        raw = uri.split("://", 1)[1].split("#", 1)[0]
        data = json.loads(_b64decode(raw).decode("utf-8"))
        return {
            "type": "vmess",
            "name": data.get("ps") or fallback_name,
            "server": data.get("add", ""),
            "port": int(data.get("port", 0)),
            "uuid": data.get("id", ""),
            "alter_id": int(data.get("aid", 0) or 0),
            "cipher": data.get("scy") or "auto",
            "network": data.get("net") or "tcp",
            "tls": str(data.get("tls", "")).lower() in ("tls", "true", "1"),
            "sni": data.get("sni") or data.get("host") or "",
            "path": data.get("path") or "",
            "host": data.get("host") or "",
        }

    if scheme == "ss":
        body = uri.split("://", 1)[1]
        fragment = body.split("#", 1)[1] if "#" in body else ""
        body = body.split("#", 1)[0]
        query = body.split("?", 1)[1] if "?" in body else ""
        body = body.split("?", 1)[0]
        if "@" not in body:
            body = _b64decode(body).decode("utf-8")
        userinfo, hostport = body.rsplit("@", 1)
        if ":" not in userinfo:
            userinfo = _b64decode(userinfo).decode("utf-8")
        method, password = userinfo.split(":", 1)
        parsed = urlsplit("ss://x@" + hostport)
        return {
            "type": "ss",
            "name": _name_from_fragment(fragment, fallback_name),
            "server": parsed.hostname or "",
            "port": parsed.port or 0,
            "method": unquote(method),
            "password": unquote(password),
            "plugin": parse_qs(query).get("plugin", [""])[0],
        }

    parsed = urlsplit(uri)
    query = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
    node = {
        "type": "hysteria2" if scheme == "hy2" else scheme,
        "name": _name_from_fragment(parsed.fragment, fallback_name),
        "server": parsed.hostname or "",
        "port": parsed.port or 0,
        "username": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "query": query,
    }
    return node


def _scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def yaml_dump(value, indent=0):
    pad = " " * indent
    lines = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.extend(yaml_dump(item, indent + 2))
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                first = True
                for key, child in item.items():
                    marker = "- " if first else "  "
                    if isinstance(child, (dict, list)):
                        lines.append(f"{pad}{marker}{key}:")
                        lines.extend(yaml_dump(child, indent + 4))
                    else:
                        lines.append(f"{pad}{marker}{key}: {_scalar(child)}")
                    first = False
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    return lines


def clash_proxy(node: dict) -> dict:
    base = {
        "name": node["name"],
        "server": node["server"],
        "port": node["port"],
        "udp": True,
    }
    kind = node["type"]
    if kind == "ss":
        base.update(type="ss", cipher=node["method"], password=node["password"])
        if node.get("plugin"):
            plugin, _, opts = node["plugin"].partition(";")
            base["plugin"] = plugin
            if opts:
                base["plugin-opts"] = {"mode": opts}
        return base
    if kind == "vmess":
        base.update(type="vmess", uuid=node["uuid"], alterId=node["alter_id"], cipher=node["cipher"])
        network, tls = node.get("network"), node.get("tls")
        if network and network != "tcp":
            base["network"] = network
        if tls:
            base["tls"] = True
            if node.get("sni"):
                base["servername"] = node["sni"]
        if network == "ws":
            base["ws-opts"] = {"path": node.get("path") or "/"}
            if node.get("host"):
                base["ws-opts"]["headers"] = {"Host": node["host"]}
        return base

    query = node.get("query", {})
    if kind == "vless":
        base.update(type="vless", uuid=node["username"])
        network = query.get("type", "tcp")
        if network != "tcp":
            base["network"] = network
        security = query.get("security", "")
        if security in ("tls", "reality"):
            base["tls"] = True
            base["servername"] = query.get("sni") or query.get("serverName") or node["server"]
        if query.get("flow"):
            base["flow"] = query["flow"]
        if network == "ws":
            base["ws-opts"] = {"path": query.get("path", "/")}
            if query.get("host"):
                base["ws-opts"]["headers"] = {"Host": query["host"]}
        if network == "grpc":
            base["grpc-opts"] = {"grpc-service-name": query.get("serviceName", "")}
        if security == "reality":
            base["reality-opts"] = {
                "public-key": query.get("pbk", ""),
                "short-id": query.get("sid", ""),
            }
            base["client-fingerprint"] = query.get("fp", "chrome")
        return base
    if kind == "trojan":
        base.update(type="trojan", password=node["username"] or node["password"])
        base["sni"] = query.get("sni") or query.get("peer") or node["server"]
        if query.get("allowInsecure") in ("1", "true"):
            base["skip-cert-verify"] = True
        network = query.get("type", "tcp")
        if network == "ws":
            base["network"] = "ws"
            base["ws-opts"] = {"path": query.get("path", "/")}
        return base
    if kind == "hysteria2":
        base.update(type="hysteria2", password=node["username"] or node["password"])
        base["sni"] = query.get("sni") or node["server"]
        if query.get("insecure") in ("1", "true"):
            base["skip-cert-verify"] = True
        if query.get("obfs"):
            base["obfs"] = query["obfs"]
            base["obfs-password"] = query.get("obfs-password", "")
        return base
    if kind == "tuic":
        base.update(type="tuic", uuid=node["username"], password=node["password"])
        base["sni"] = query.get("sni") or node["server"]
        base["alpn"] = [query.get("alpn", "h3")]
        return base
    raise ValueError(f"Clash 暂不支持 {kind}")


def singbox_outbound(node: dict) -> dict:
    kind = node["type"]
    base = {"type": "shadowsocks" if kind == "ss" else kind, "tag": node["name"], "server": node["server"], "server_port": node["port"]}
    if kind == "ss":
        base.update(method=node["method"], password=node["password"])
    elif kind == "vmess":
        base.update(uuid=node["uuid"], security=node["cipher"], alter_id=node["alter_id"])
        if node.get("tls"):
            base["tls"] = {"enabled": True, "server_name": node.get("sni") or node["server"]}
        if node.get("network") == "ws":
            base["transport"] = {"type": "ws", "path": node.get("path") or "/", "headers": {"Host": node.get("host") or node["server"]}}
    else:
        query = node.get("query", {})
        if kind == "vless":
            base["uuid"] = node["username"]
            if query.get("flow"):
                base["flow"] = query["flow"]
        elif kind == "trojan":
            base["password"] = node["username"] or node["password"]
        elif kind == "hysteria2":
            base["password"] = node["username"] or node["password"]
        elif kind == "tuic":
            base.update(uuid=node["username"], password=node["password"])
        security = query.get("security", "tls" if kind in ("trojan", "hysteria2", "tuic") else "")
        if security in ("tls", "reality") or kind in ("trojan", "hysteria2", "tuic"):
            tls = {"enabled": True, "server_name": query.get("sni") or node["server"]}
            if query.get("insecure") in ("1", "true"):
                tls["insecure"] = True
            if security == "reality":
                tls["reality"] = {"enabled": True, "public_key": query.get("pbk", ""), "short_id": query.get("sid", "")}
                tls["utls"] = {"enabled": True, "fingerprint": query.get("fp", "chrome")}
            base["tls"] = tls
        network = query.get("type", "tcp")
        if network == "ws":
            base["transport"] = {"type": "ws", "path": query.get("path", "/"), "headers": {"Host": query.get("host", node["server"])}}
        elif network == "grpc":
            base["transport"] = {"type": "grpc", "service_name": query.get("serviceName", "")}
    return base


def surge_proxy(node: dict) -> str | None:
    name = node["name"].replace(",", "，")
    if node["type"] == "ss":
        return f"{name} = ss, {node['server']}, {node['port']}, encrypt-method={node['method']}, password={node['password']}, udp-relay=true"
    if node["type"] == "trojan":
        query = node.get("query", {})
        password = node["username"] or node["password"]
        sni = query.get("sni") or node["server"]
        return f"{name} = trojan, {node['server']}, {node['port']}, password={password}, sni={sni}"
    return None


def render_subscription(target: str, template: str, nodes: list[dict]) -> tuple[bytes, str]:
    parsed = []
    used_names = {}
    for row in nodes:
        try:
            node = parse_node(row["uri"], row["name"])
            desired = row["name"] or node["name"]
            used_names[desired] = used_names.get(desired, 0) + 1
            node["name"] = desired if used_names[desired] == 1 else f"{desired} {used_names[desired]}"
            parsed.append(node)
        except (ValueError, KeyError, json.JSONDecodeError):
            continue

    if target == "base64":
        raw = "\n".join(row["uri"].strip() for row in nodes if row["uri"].strip())
        return base64.b64encode(raw.encode("utf-8")), "text/plain; charset=utf-8"
    if target == "clash":
        proxies = [clash_proxy(node) for node in parsed]
        proxy_lines = "\n".join(yaml_dump(proxies)) or "  []"
        names = "\n".join(f"      - {_scalar(p['name'])}" for p in proxies) or "      - DIRECT"
        content = template.replace("{{PROXIES}}", proxy_lines).replace("{{PROXY_NAMES}}", names)
        return content.encode("utf-8"), "text/yaml; charset=utf-8"
    if target == "singbox":
        outbounds = [singbox_outbound(node) for node in parsed]
        outbounds.append({"type": "direct", "tag": "direct"})
        out_text = json.dumps(outbounds, ensure_ascii=False, indent=2)[1:-1].strip()
        first = parsed[0]["name"] if parsed else "direct"
        content = template.replace("{{OUTBOUNDS}}", _indent(out_text, 4)).replace("{{FIRST_TAG}}", json.dumps(first, ensure_ascii=False))
        return content.encode("utf-8"), "application/json; charset=utf-8"
    if target == "surge":
        lines = [line for line in (surge_proxy(node) for node in parsed) if line]
        names = ", ".join(node["name"].replace(",", "，") for node in parsed if surge_proxy(node)) or "DIRECT"
        content = template.replace("{{SURGE_PROXIES}}", "\n".join(lines)).replace("{{SURGE_NAMES}}", names)
        return content.encode("utf-8"), "text/plain; charset=utf-8"
    raise ValueError("未知订阅目标")


def _indent(text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line if line else line for line in text.splitlines())


def safe_filename(name: str, extension: str) -> str:
    clean = re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_", name, flags=re.UNICODE).strip("._")
    return f"{clean or 'subscription'}.{extension}"
