"""FlyingLife 一次性认证向导。

密码和 Session ID 均使用隐藏输入；仅持久化验证成功的 Session ID。
"""

import argparse
import getpass
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)


class AuthError(Exception):
    pass


def build_client(base_url: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        follow_redirects=False,
        timeout=httpx.Timeout(30),
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": base_url.rstrip("/"),
            "Referer": f"{base_url.rstrip('/')}/",
            "Content-Type": "application/json",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
        },
    )


def parse_response(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception as e:
        raise AuthError("服务器返回了非 JSON 内容") from e
    if not isinstance(payload, dict):
        raise AuthError("服务器返回格式异常")
    code = payload.get("code")
    if response.status_code >= 300 or code not in (0, 200):
        raise AuthError(str(payload.get("message") or f"认证失败 code={code}"))
    return payload


def login(client: httpx.Client) -> str:
    email = input("邮箱: ").strip()
    password = getpass.getpass("密码（隐藏输入）: ")
    if not email or not password:
        raise AuthError("邮箱和密码不能为空")
    try:
        payload = parse_response(client.post("/api/login.php", json={"email": email, "password": password}))
    finally:
        password = ""
    data = payload.get("data")
    session_id = data.get("session_id") if isinstance(data, dict) else None
    if not isinstance(session_id, str) or not session_id.strip():
        raise AuthError("登录成功但响应中缺少 Session ID")
    return session_id.strip()


def input_session() -> str:
    session_id = getpass.getpass("Session ID（隐藏输入）: ").strip()
    if not session_id:
        raise AuthError("Session ID 不能为空")
    return session_id


def validate(client: httpx.Client, session_id: str) -> dict[str, Any]:
    response = client.post("/api/check_login.php", headers={"X-Session-Id": session_id}, json={})
    payload = parse_response(response)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise AuthError("登录验证返回格式异常")
    return data


def load_existing(auth_file: Path) -> str | None:
    if not auth_file.exists():
        return None
    try:
        data = json.loads(auth_file.read_text(encoding="utf-8"))
    except Exception:
        return None
    value = data.get("session_id") if isinstance(data, dict) else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def save_session(auth_file: Path, session_id: str) -> None:
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(UTC).isoformat()
    temp_file = auth_file.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "created_at": now,
                "validated_at": now,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if os.name != "nt":
        temp_file.chmod(0o600)
    temp_file.replace(auth_file)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="配置 FlyingLife 登录状态")
    parser.add_argument("--reauth", action="store_true", help="跳过现有会话检查并重新认证")
    parser.add_argument("--data-path", type=Path, default=None, help="Bot 数据目录, 默认读取 DATA_PATH 或 data")
    return parser.parse_args()


def main() -> int:
    load_dotenv()
    args = parse_args()
    base_url = os.getenv("FLYINGLIFE_BASE_URL", "https://parse.flyinglife.cn")
    data_path = args.data_path or Path(os.getenv("DATA_PATH", "data"))
    auth_file = data_path / "config" / "flyinglife_auth.json"

    try:
        with build_client(base_url) as client:
            existing = load_existing(auth_file)
            if existing and not args.reauth:
                try:
                    validate(client, existing)
                except AuthError:
                    print("现有 FlyingLife 会话已失效，需要重新认证。")
                else:
                    print("现有 FlyingLife 会话验证成功。")
                    if input("是否重新配置？[y/N]: ").strip().lower() not in {"y", "yes"}:
                        return 0

            print("\n请选择认证方式：")
            print("1. 邮箱密码交互式登录")
            print("2. 直接输入 Session ID")
            choice = input("选择 [1/2]: ").strip()
            if choice == "1":
                session_id = login(client)
            elif choice == "2":
                session_id = input_session()
            else:
                raise AuthError("无效选择")

            account = validate(client, session_id)
            save_session(auth_file, session_id)
            vip_text = (
                "永久会员" if account.get("is_permanent_vip") else ("会员" if account.get("is_vip") else "非会员")
            )
            print(f"认证成功：{vip_text}。会话已保存到 {auth_file}，请将其按密钥保护。")
            return 0
    except (AuthError, httpx.HTTPError) as e:
        print(f"认证失败：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
