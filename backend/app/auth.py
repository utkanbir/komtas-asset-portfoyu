"""Kimlik doğrulama.

AUTH_MODE:
  none    Geliştirme / kapalı ağ. Herkes DEV_USER olarak görünür.
  header  Önde bir kimlik vekili (oauth2-proxy + Entra ID / Google vb.) çalışır ve
          kullanıcıyı AUTH_HEADER başlığında iletir (varsayılan X-Auth-Request-Email).
          Bu modda uygulamaya yalnızca vekil üzerinden erişilebilmelidir (Ingress/NetworkPolicy).
  basic   Basit paylaşım: BASIC_USERS="ad:parola,ad2:parola2" (Kubernetes Secret'tan gelir).

WRITERS: yazma yetkisi olanlar. "*" herkes; virgüllü e-posta listesi ve/veya "@komtas.com"
gibi alan adları. Diğer kullanıcılar salt okunur görür (ön yüz düzenleme düğmelerini gizler).
"""
from __future__ import annotations

import base64
import hmac
import os
from dataclasses import dataclass

from fastapi import HTTPException, Request

AUTH_MODE = os.getenv("AUTH_MODE", "none").lower()
AUTH_HEADER = os.getenv("AUTH_HEADER", "X-Auth-Request-Email")
AUTH_NAME_HEADER = os.getenv("AUTH_NAME_HEADER", "X-Auth-Request-Preferred-Username")
DEV_USER = os.getenv("DEV_USER", "gelistirici@komtas.local")
WRITERS = [w.strip().lower() for w in os.getenv("WRITERS", "*").split(",") if w.strip()]
BASIC_USERS = {}
for pair in os.getenv("BASIC_USERS", "").split(","):
    if ":" in pair:
        u, p = pair.split(":", 1)
        BASIC_USERS[u.strip()] = p.strip()


@dataclass
class User:
    id: str
    name: str
    can_write: bool

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "can_write": self.can_write, "auth_mode": AUTH_MODE}


def _can_write(uid: str) -> bool:
    uid = uid.lower()
    for w in WRITERS:
        if w == "*" or w == uid or (w.startswith("@") and uid.endswith(w)):
            return True
    return False


def _unauthorized(basic: bool = False):
    headers = {"WWW-Authenticate": 'Basic realm="Asset Portfoyu", charset="UTF-8"'} if basic else None
    return HTTPException(401, detail={"code": "unauthenticated", "message": "giriş gerekli"}, headers=headers)


def current_user(request: Request) -> User:
    if AUTH_MODE == "none":
        return User(DEV_USER, DEV_USER.split("@")[0], _can_write(DEV_USER))
    if AUTH_MODE == "header":
        uid = request.headers.get(AUTH_HEADER, "").strip()
        if not uid:
            raise _unauthorized()
        name = request.headers.get(AUTH_NAME_HEADER, "").strip() or uid.split("@")[0]
        return User(uid, name, _can_write(uid))
    if AUTH_MODE == "basic":
        h = request.headers.get("Authorization", "")
        if not h.lower().startswith("basic "):
            raise _unauthorized(True)
        try:
            u, p = base64.b64decode(h[6:]).decode("utf-8").split(":", 1)
        except Exception:
            raise _unauthorized(True)
        exp = BASIC_USERS.get(u)
        if exp is None or not hmac.compare_digest(exp.encode(), p.encode()):
            raise _unauthorized(True)
        return User(u, u.split("@")[0], _can_write(u))
    raise HTTPException(500, detail={"message": f"bilinmeyen AUTH_MODE: {AUTH_MODE}"})


def require_writer(request: Request) -> User:
    u = current_user(request)
    if not u.can_write:
        raise HTTPException(403, detail={"code": "permission_denied", "message": "yazma yetkiniz yok"})
    return u
