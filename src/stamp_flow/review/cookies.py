"""Перенос кук между requests (CookieJar) и Playwright (капча в браузере)."""

from __future__ import annotations


def jar_to_browser_cookies(jar) -> list[dict]:
    """CookieJar → формат Playwright `context.add_cookies()`."""
    result = []
    for c in jar:
        item = {
            "name": c.name,
            "value": c.value or "",
            "domain": c.domain,
            "path": c.path or "/",
            "secure": bool(c.secure),
            "httpOnly": c.has_nonstandard_attr("HttpOnly"),
        }
        if c.expires:
            item["expires"] = float(c.expires)
        result.append(item)
    return result


def add_browser_cookies(jar, cookies) -> int:
    """Куки из Playwright (`context.cookies()`) → CookieJar.

    У MozillaCookieJar нет метода `.set()` (он есть только у RequestsCookieJar),
    поэтому собираем Cookie через requests.cookies.create_cookie.
    Возвращает, сколько кук добавлено (HHOnlyCookieJar отбрасывает не-hh).
    """
    from requests.cookies import create_cookie

    before = len(jar)
    for c in cookies:
        expires = c.get("expires")
        jar.set_cookie(
            create_cookie(
                name=c["name"],
                value=c["value"],
                domain=c.get("domain", ""),
                path=c.get("path", "/"),
                secure=bool(c.get("secure")),
                expires=int(expires) if expires and expires > 0 else None,
                rest={"HttpOnly": None} if c.get("httpOnly") else {},
            )
        )
    return len(jar) - before
