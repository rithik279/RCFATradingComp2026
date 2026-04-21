"""LT3 market decision-support helpers (NO TRADING).

Contains Rotman-provided cumulative VWAP/volume logic adapted to:
- Return structured data
- Print nothing
- Sleep nowhere

This module must remain trade-free.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import requests


class ApiException(Exception):
    pass


class RateLimitException(ApiException):
    def __init__(self, retry_after: Optional[float] = None, message: str = "HTTP 429 Rate limit") -> None:
        super().__init__(message)
        self.retry_after = retry_after


def calculate_cumulatives(book: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Calculate cumulative available volume and VWAP at each depth level.

    Uses available size = quantity - quantity_filled (if present).
    Mutates the dicts in-place by adding:
      - cumulative_vol (int)
      - cumulative_vwap (float)

    Returns the same list reference.
    """

    cumulative_vol = 0
    cumulative_notional = 0.0

    for level in book:
        qty = int(level.get("quantity", 0) or 0)
        filled = int(level.get("quantity_filled", 0) or 0)
        available = qty - filled
        if available < 0:
            available = 0

        price = level.get("price")
        price_f = float(price) if price is not None else 0.0

        cumulative_vol += available
        cumulative_notional += price_f * available

        level["cumulative_vol"] = int(cumulative_vol)
        level["cumulative_vwap"] = (cumulative_notional / cumulative_vol) if cumulative_vol > 0 else 0.0

    return book


def depth_view(
    session: requests.Session,
    *,
    tickers: Sequence[str],
    base_url: str = "http://localhost:9999/v1",
    limit_levels: Optional[int] = None,
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Fetch and return order-book depth for tickers with cumulative fields.

    Returns:
      {
        "CRZY": {"bids": [...], "asks": [...]},
        "TAME": {"bids": [...], "asks": [...]},
      }

    No printing. No sleeping.
    """

    out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    base = base_url.rstrip("/")

    for ticker in tickers:
        resp = session.get(
            f"{base}/securities/book",
            params={"ticker": ticker},
            timeout=2.5,
        )

        if resp.status_code == 401:
            raise ApiException(
                "HTTP 401 Unauthorized: session must include X-API-Key header matching the RIT client key"
            )

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            ra = float(retry_after) if retry_after else None
            raise RateLimitException(ra)

        if not (200 <= resp.status_code < 300):
            raise ApiException(f"HTTP {resp.status_code} fetching book for {ticker}: {resp.text}")

        book = resp.json()
        bids = list(book.get("bids", []))
        asks = list(book.get("asks", []))

        if limit_levels is not None:
            bids = bids[: int(limit_levels)]
            asks = asks[: int(limit_levels)]

        calculate_cumulatives(bids)
        calculate_cumulatives(asks)

        out[str(ticker)] = {"bids": bids, "asks": asks}

    return out
