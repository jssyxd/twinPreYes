"""Scanner for Polymarket Negative Risk (Neg-Risk) multi-outcome events and CLOB orderbooks."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from typing import Any, Iterable

from neg_risk.models import BookLevel, BucketBook, EventMarket, OutcomeBucket

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
GAMMA_EVENTS_ENDPOINT = f"{GAMMA_BASE}/events"
BOOKS_ENDPOINT = f"{CLOB_BASE}/books"
BOOK_ENDPOINT = f"{CLOB_BASE}/book"


class NegRiskScanner:
    def __init__(
        self,
        *,
        timeout_seconds: float = 6.0,
        max_workers: int = 15,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_workers = max_workers

    def _fetch_json(self, url: str) -> Any:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "twinPreYes-NegRisk/1.0", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
            data = resp.read().decode("utf-8")
        return json.loads(data)

    def scan_active_events_from_gamma(
        self,
        *,
        tags: list[str] | None = None,
        limit: int = 50,
        require_closed_mece: bool = True,
    ) -> list[EventMarket]:
        """Fetch active multi-outcome markets from Polymarket Gamma API.
        
        If require_closed_mece is True, validates that all candidate markets within
        the event are open and active, filtering out events where crucial outcomes
        (e.g., 'Other') are inactive, which would break the guaranteed parity basket.
        """
        params = {
            "closed": "false",
            "active": "true",
            "limit": str(limit),
            "order": "volume24hr",
            "ascending": "false",
        }
        if tags:
            params["tag"] = tags[0]
        url = f"{GAMMA_EVENTS_ENDPOINT}?{urllib.parse.urlencode(params)}"
        try:
            raw_events = self._fetch_json(url)
        except Exception:
            return []

        if not isinstance(raw_events, list):
            return []

        event_markets: list[EventMarket] = []
        for raw in raw_events:
            if not isinstance(raw, dict):
                continue
            event_id = str(raw.get("id") or "")
            slug = str(raw.get("slug") or "")
            title = str(raw.get("title") or slug)
            raw_markets = raw.get("markets") or []
            if not isinstance(raw_markets, list) or len(raw_markets) < 2:
                continue

            # Invariant check: if require_closed_mece is enabled, ensure no market is disabled/closed
            # that represents a valid candidate outcome (especially 'Other')
            if require_closed_mece:
                has_inactive_other = any(
                    isinstance(m, dict) and ('Other' in str(m.get('groupItemTitle', '')) or 'Other' in str(m.get('question', '')))
                    and (m.get('active') is False or m.get('closed') is True)
                    for m in raw_markets
                )
                if has_inactive_other:
                    # Incomplete basket! Parity cannot be guaranteed
                    continue

            buckets: list[OutcomeBucket] = []
            is_neg_risk = bool(raw.get("negRisk") or any(m.get("negRisk") for m in raw_markets if isinstance(m, dict)))

            for m in raw_markets:
                if not isinstance(m, dict):
                    continue
                if not (m.get("active") is True and m.get("closed") is False):
                    continue
                market_id = str(m.get("id") or "")
                question = str(m.get("question") or m.get("groupItemTitle") or market_id)
                try:
                    outcomes = json.loads(m.get("outcomes", "[]")) if isinstance(m.get("outcomes"), str) else (m.get("outcomes") or [])
                    token_ids = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else (m.get("clobTokenIds") or [])
                except Exception:
                    continue

                if not isinstance(outcomes, list) or not isinstance(token_ids, list):
                    continue
                if len(outcomes) != len(token_ids) or not outcomes:
                    continue

                yes_token = None
                no_token = None
                for outcome_name, tok in zip(outcomes, token_ids):
                    if outcome_name == "Yes":
                        yes_token = str(tok)
                    elif outcome_name == "No":
                        no_token = str(tok)

                if not yes_token and len(token_ids) >= 1:
                    yes_token = str(token_ids[0])
                    if len(token_ids) > 1:
                        no_token = str(token_ids[1])

                if not yes_token:
                    continue

                label = str(m.get("groupItemTitle") or question)
                buckets.append(
                    OutcomeBucket(
                        bucket_id=market_id,
                        label=label,
                        yes_token_id=yes_token,
                        no_token_id=no_token,
                        neg_risk=is_neg_risk,
                        market_id=market_id,
                    )
                )

            if len(buckets) >= 2:
                event_markets.append(
                    EventMarket(
                        event_id=event_id,
                        event_slug=slug,
                        title=title,
                        buckets=tuple(buckets),
                        neg_risk=is_neg_risk,
                        resolution_date=str(raw.get("endDate") or ""),
                    )
                )

        return event_markets

    def scan_weather_rules_as_events(self, rules: list[dict[str, Any]]) -> list[EventMarket]:
        """Convert parsed weather market rules from market_adapter into EventMarket instances."""
        events: list[EventMarket] = []
        for r in rules:
            raw_buckets = r.get("buckets", [])
            if len(raw_buckets) < 2:
                continue
            buckets = [
                OutcomeBucket(
                    bucket_id=str(b["bucket_id"]),
                    label=str(b.get("label", b["bucket_id"])),
                    yes_token_id=str(b["yes_token_id"]),
                    no_token_id=str(b.get("no_token_id")),
                    lo=b.get("lo"),
                    hi=b.get("hi"),
                    neg_risk=bool(b.get("neg_risk", True)),
                    market_id=str(b.get("market_id", b["bucket_id"])),
                )
                for b in raw_buckets
            ]
            event_id = str(r.get("event_id") or r.get("market_rule_id"))
            slug = str(r.get("event_slug") or event_id)
            title = f"{r.get('city_id', '')} {r.get('direction', '')} {r.get('market_local_date', '')}"
            events.append(
                EventMarket(
                    event_id=event_id,
                    event_slug=slug,
                    title=title,
                    buckets=tuple(buckets),
                    neg_risk=True,
                    category="temperature",
                    resolution_date=str(r.get("market_local_date")),
                )
            )
        return events

    def fetch_orderbooks(self, token_ids: Iterable[str]) -> dict[str, BucketBook]:
        """Fetch orderbooks for multiple token IDs concurrently."""
        tokens = list({str(t) for t in token_ids})
        if not tokens:
            return {}

        results: dict[str, BucketBook] = {}

        def fetch_single(tok: str) -> tuple[str, BucketBook | None]:
            url = f"{BOOK_ENDPOINT}?{urllib.parse.urlencode({'token_id': tok})}"
            try:
                t_fetch = time.time()
                data = self._fetch_json(url)
                if not isinstance(data, dict):
                    return tok, None

                raw_bids = data.get("bids") or []
                raw_asks = data.get("asks") or []

                bids = tuple(
                    BookLevel(price=Decimal(str(b["price"])), size=Decimal(str(b["size"])))
                    for b in raw_bids
                    if "price" in b and "size" in b
                )
                asks = tuple(
                    BookLevel(price=Decimal(str(a["price"])), size=Decimal(str(a["size"])))
                    for a in raw_asks
                    if "price" in a and "size" in a
                )

                best_bid = max((b.price for b in bids), default=None)
                best_ask = min((a.price for a in asks), default=None)
                bid_size = sum((b.size for b in bids if b.price == best_bid), Decimal("0")) if best_bid else Decimal("0")
                ask_size = sum((a.size for a in asks if a.price == best_ask), Decimal("0")) if best_ask else Decimal("0")

                book = BucketBook(
                    token_id=tok,
                    label=tok,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    bid_size=bid_size,
                    ask_size=ask_size,
                    bids=bids,
                    asks=asks,
                    fetched_at=t_fetch,
                )
                return tok, book
            except Exception:
                return tok, None

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = [executor.submit(fetch_single, tok) for tok in tokens]
            for fut in as_completed(futures):
                tok, book = fut.result()
                if book is not None:
                    results[tok] = book

        return results
