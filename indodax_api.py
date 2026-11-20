"""Restricted Indodax Trade API v2 adapter. No withdrawal endpoints are exposed."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class IndodaxAPIError(RuntimeError):
    pass


class IndodaxAPI:
    BASE_URL = "https://api.indodax.com"

    def __init__(self, api_key: str, secret: str, timeout: int = 15) -> None:
        if not api_key.strip() or not secret.strip():
            raise ValueError("INDODAX_API_KEY dan INDODAX_API_SECRET wajib diisi")
        self.api_key = api_key.strip()
        self.secret = secret.strip().encode("utf-8")
        self.timeout = timeout

    def _call(self, method: str, path: str, params: dict[str, object] | None = None) -> object:
        payload = dict(params or {})
        payload.setdefault("timestamp", int(time.time() * 1000))
        payload.setdefault("recvWindow", 5000)
        encoded = urlencode(payload).encode("utf-8")
        signature = hmac.new(self.secret, encoded, hashlib.sha256).hexdigest()
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "TradingAI-Termux/0.2",
            "X-APIKEY": self.api_key,
            "Sign": signature,
        }
        method = method.upper()
        url = self.BASE_URL + path
        data = None
        if method == "GET":
            url += "?" + encoded.decode("ascii")
        elif method == "POST":
            data = encoded
        else:
            raise ValueError("HTTP method tidak diizinkan oleh adapter")
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as exc:
            detail = ""
            if isinstance(exc, HTTPError):
                try:
                    detail = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
            raise IndodaxAPIError(f"Request {method} {path} gagal: {exc} {detail}") from exc
        if isinstance(result, dict) and (result.get("code") or result.get("success") is False):
            raise IndodaxAPIError(f"Indodax menolak {method} {path}: {result}")
        return result

    @staticmethod
    def _data(result: object) -> object:
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    def account(self) -> dict[str, object]:
        result = self._call("GET", "/api/v2/account", {"omitZeroBalances": "true"})
        data = self._data(result)
        if not isinstance(data, dict):
            raise IndodaxAPIError("Respons account Indodax tidak valid")
        return data

    def open_orders(self, symbol: str) -> list[dict[str, object]]:
        result = self._call("GET", "/api/v2/openOrders", {"symbol": symbol.upper()})
        data = self._data(result)
        if not isinstance(data, list):
            raise IndodaxAPIError("Respons openOrders Indodax tidak valid")
        return [row for row in data if isinstance(row, dict)]

    def my_trades(self, symbol: str, client_order_id: str) -> list[dict[str, object]]:
        result = self._call("GET", "/api/v2/myTrades", {
            "symbol": symbol.lower(), "clientOrderId": client_order_id, "limit": 100, "sort": "desc"
        })
        data = self._data(result)
        if not isinstance(data, list):
            raise IndodaxAPIError("Respons myTrades Indodax tidak valid")
        return [row for row in data if isinstance(row, dict)]

    def order_status(self, symbol: str, client_order_id: str) -> dict[str, object]:
        result = self._call("GET", "/api/v2/order", {
            "symbol": symbol.upper(), "origClientOrderId": client_order_id,
        })
        data = self._data(result)
        if not isinstance(data, dict):
            raise IndodaxAPIError("Respons status order Indodax tidak valid")
        return data

    def create_market_order(self, symbol: str, side: str, *, quote_idr: int | None = None,
                            quantity: str | None = None, client_order_id: str) -> object:
        side = side.upper()
        params: dict[str, object] = {
            "symbol": symbol.upper(), "side": side, "type": "MARKET",
            "newClientOrderId": client_order_id,
        }
        if side == "BUY" and quote_idr is not None and quantity is None:
            params["quoteOrderQty"] = int(quote_idr)
        elif side == "SELL" and quantity is not None and quote_idr is None:
            params["quantity"] = quantity
        else:
            raise ValueError("Parameter market order BUY/SELL tidak konsisten")
        return self._call("POST", "/api/v2/order", params)

    def cancel_by_client_id(self, symbol: str, client_order_id: str) -> object:
        # Cancellation is intentionally omitted; unresolved orders fail closed for manual review.
        raise IndodaxAPIError("Pembatalan order belum didukung; periksa order secara manual")
