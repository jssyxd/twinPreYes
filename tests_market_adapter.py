"""Regression test: 裸 socket 读超时不得穿透结算循环。

背景（2026-09-12 02:11:46Z 实测）: `_fetch_json` 只把 HTTPError/URLError 转成
RuntimeError，未处理 `TimeoutError`（socket 读超时不会包成 URLError）。该异常穿透
`fetch_market_resolution` 的 `except (RuntimeError, ValueError, JSONDecodeError)`，
在 `_r_cycle` 记 `settle_failed` 并**中止整轮结算**。

修复（操作者选 A）: `_fetch_json` 增 `except TimeoutError -> RuntimeError`，
超时按 unresolved 静默重试（settle_poll 每分钟重试）。

Run: python3 tests_market_adapter.py
"""
from __future__ import annotations

import socket
import sys
import urllib.error
import urllib.request

sys.path.insert(0, ".")

import market_adapter as ma  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def _patch_urlopen(exc: BaseException):
    orig = urllib.request.urlopen

    def boom(*_a, **_k):
        raise exc

    urllib.request.urlopen = boom
    return orig


def test_timeout_becomes_runtime_error() -> None:
    orig = _patch_urlopen(TimeoutError("The read operation timed out"))
    try:
        try:
            ma._fetch_json("https://gamma-api.polymarket.com/events?slug=x")
        except RuntimeError as exc:
            check("timeout -> RuntimeError", "timeout" in str(exc).lower(), repr(str(exc)))
        except BaseException as exc:  # noqa: BLE001
            check("timeout -> RuntimeError", False, f"got {type(exc).__name__}: {exc}")
        else:
            check("timeout -> RuntimeError", False, "no exception raised")
    finally:
        urllib.request.urlopen = orig


def test_socket_timeout_alias() -> None:
    orig = _patch_urlopen(socket.timeout("timed out"))
    try:
        try:
            ma._fetch_json("https://gamma-api.polymarket.com/events?slug=x")
        except RuntimeError:
            check("socket.timeout -> RuntimeError", True)
        except BaseException as exc:  # noqa: BLE001
            check("socket.timeout -> RuntimeError", False, f"got {type(exc).__name__}")
        else:
            check("socket.timeout -> RuntimeError", False, "no exception raised")
    finally:
        urllib.request.urlopen = orig


def test_resolution_returns_none_on_timeout() -> None:
    orig = _patch_urlopen(TimeoutError("The read operation timed out"))
    try:
        try:
            got = ma.fetch_market_resolution(
                {"city_id": "shanghai", "market_city_slug": "shanghai"}, "2026-09-12", "high", "12345"
            )
        except BaseException as exc:  # noqa: BLE001
            check("resolution timeout -> None (不中断结算)", False, f"escaped {type(exc).__name__}: {exc}")
        else:
            check("resolution timeout -> None (不中断结算)", got is None, repr(got))
    finally:
        urllib.request.urlopen = orig


def test_404_still_empty() -> None:
    orig = _patch_urlopen(urllib.error.HTTPError("u", 404, "nf", {}, None))
    try:
        try:
            got = ma._fetch_json("https://gamma-api.polymarket.com/events?slug=x")
        except BaseException as exc:  # noqa: BLE001
            check("404 -> {} (既有行为不变)", False, f"raised {type(exc).__name__}")
        else:
            check("404 -> {} (既有行为不变)", got == {}, repr(got))
    finally:
        urllib.request.urlopen = orig


def main() -> None:
    print("tests_market_adapter.py")
    test_timeout_becomes_runtime_error()
    test_socket_timeout_alias()
    test_resolution_returns_none_on_timeout()
    test_404_still_empty()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
