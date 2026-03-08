import aiohttp
import asyncio
import logging

logger = logging.getLogger(__name__)

BB_BASE = "https://api.bestbuy.com/v1"

CATEGORIES = [
    ("Gaming Desktops",  "pcmcat287600050002"),
    ("Gaming Laptops",   "pcmcat287600050003"),
    ("MacBooks",         "pcmcat247400050001"),
    ("All-in-One PCs",   "abcat0501005"),
    ("Windows Laptops",  "pcmcat247400050000"),
]

SHOW_FIELDS = ",".join([
    "sku", "name", "manufacturer", "salePrice", "regularPrice",
    "dollarSavings", "percentSavings", "onSale", "onlineAvailability",
    "url", "bestSellingRank", "priceUpdateDate",
])

# Fetch 50 into pool — full report shows top 10, filters search the rest
POOL_SIZE    = 50
DISPLAY_SIZE = 10

# Words that flag a non-new product
EXCLUDE = ("refurbished", "open-box", "open box", "pre-owned", "preowned", "renewed")


def _is_new(p: dict) -> bool:
    name = (p.get("name") or "").lower()
    return not any(w in name for w in EXCLUDE)


class BBFetcher:
    def __init__(self, api_key: str):
        self.api_key = api_key

    # ── Public ────────────────────────────────────────────────────────────────

    async def fetch_all(self) -> dict:
        """
        Returns dict keyed by category name:
        {
          "Gaming Laptops": {
            "products":              [...],   # top 10 by best seller rank (full report)
            "pool":                  [...],   # top 50 by best seller rank (on_sale / hot filters)
            "trending_products":     [...],   # full product details from trendingViewed endpoint
            "most_viewed_products":  [...],   # full product details from mostViewed endpoint
          }, ...
        }
        """
        async with aiohttp.ClientSession() as session:
            cat_tasks = [self._fetch_category(session, n, c) for n, c in CATEGORIES]
            sig_tasks = [self._fetch_signal_products(session, n, c) for n, c in CATEGORIES]
            cat_results = await asyncio.gather(*cat_tasks, return_exceptions=True)
            sig_results = await asyncio.gather(*sig_tasks, return_exceptions=True)

        output = {}
        for i, (name, _) in enumerate(CATEGORIES):
            pool     = cat_results[i] if not isinstance(cat_results[i], Exception) else []
            sig_data = sig_results[i] if not isinstance(sig_results[i], Exception) else {}

            if isinstance(cat_results[i], Exception):
                logger.error(f"Category failed [{name}]: {cat_results[i]}")
            if isinstance(sig_results[i], Exception):
                logger.error(f"Signals failed [{name}]: {sig_results[i]}")

            trending_products    = sig_data.get("trending", [])
            most_viewed_products = sig_data.get("most_viewed", [])

            # Build rank lookups from signal products
            tr_rank = {str(p["sku"]): p["_trending_rank"]    for p in trending_products    if p.get("sku")}
            mv_rank = {str(p["sku"]): p["_most_viewed_rank"] for p in most_viewed_products if p.get("sku")}

            # Annotate pool products with any signal ranks they happen to have
            for p in pool:
                sku = str(p.get("sku", ""))
                bs  = p.get("bestSellingRank")
                p["_trending_rank"]    = tr_rank.get(sku)
                p["_most_viewed_rank"] = mv_rank.get(sku)
                p["_best_seller_rank"] = bs
                p["trending_str"]      = f"🔥 #{tr_rank[sku]}" if sku in tr_rank else "—"
                p["most_viewed_str"]   = f"👁 #{mv_rank[sku]}" if sku in mv_rank else "—"
                p["best_seller_str"]   = f"🛒 #{bs}"           if bs and bs <= 50 else "—"

            output[name] = {
                "products":             pool[:DISPLAY_SIZE],
                "pool":                 pool,
                "trending_products":    trending_products,
                "most_viewed_products": most_viewed_products,
            }

        return output

    # ── Private ───────────────────────────────────────────────────────────────

    async def _fetch_category(self, session, name: str, cat_id: str) -> list:
        """Top POOL_SIZE new products sorted by best seller rank."""
        url    = f"{BB_BASE}/products(categoryPath.id={cat_id})"
        params = {
            "apiKey":   self.api_key,
            "format":   "json",
            "show":     SHOW_FIELDS,
            "sort":     "bestSellingRank.asc",
            "pageSize": str(POOL_SIZE),
        }
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as r:
                if r.status != 200:
                    logger.error(f"Category {name} → HTTP {r.status}")
                    return []
                data     = await r.json()
                products = [p for p in data.get("products", []) if _is_new(p)]
                logger.info(f"  {name}: {len(products)} products")
                return products
        except Exception as e:
            logger.error(f"Category fetch error [{name}]: {e}")
            return []

    async def _fetch_signal_products(self, session, name: str, cat_id: str) -> dict:
        """
        For each signal endpoint (trending, most_viewed):
          1. Fetch the ranked SKU list
          2. Immediately do a second call to get full product details for those SKUs
        This guarantees signal filters always have real populated data.
        """
        endpoints = {
            "trending":   f"{BB_BASE}/products/trendingViewed(categoryId={cat_id})",
            "most_viewed": f"{BB_BASE}/products/mostViewed(categoryId={cat_id})",
        }
        rank_keys = {
            "trending":    "_trending_rank",
            "most_viewed": "_most_viewed_rank",
        }
        str_prefixes = {
            "trending":    "🔥 #",
            "most_viewed": "👁 #",
        }

        result = {"trending": [], "most_viewed": []}

        for sig_key, sig_url in endpoints.items():
            try:
                # Step 1 — get ranked SKUs
                async with session.get(
                    sig_url,
                    params={"apiKey": self.api_key, "format": "json", "show": "sku,rank", "pageSize": "10"},
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as r:
                    if r.status != 200:
                        logger.warning(f"Signal {sig_key} [{name}] → HTTP {r.status}")
                        continue
                    data  = await r.json()
                    items = data.get("results") or data.get("products") or []
                    # Build {sku: rank} — use index+1 as fallback rank
                    ranked = {str(item["sku"]): item.get("rank", idx + 1)
                              for idx, item in enumerate(items) if item.get("sku")}

                if not ranked:
                    continue

                # Step 2 — fetch full product details for those SKUs in one call
                sku_list = ",".join(ranked.keys())
                async with session.get(
                    f"{BB_BASE}/products(sku in({sku_list}))",
                    params={"apiKey": self.api_key, "format": "json", "show": SHOW_FIELDS, "pageSize": "10"},
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as r:
                    if r.status != 200:
                        logger.warning(f"Signal product lookup {sig_key} [{name}] → HTTP {r.status}")
                        continue
                    pdata    = await r.json()
                    products = [p for p in pdata.get("products", []) if _is_new(p)]

                # Attach rank and signal display strings to each product
                rk  = rank_keys[sig_key]
                pfx = str_prefixes[sig_key]
                for p in products:
                    sku       = str(p.get("sku", ""))
                    rank      = ranked.get(sku, 99)
                    bs        = p.get("bestSellingRank")
                    p[rk]                  = rank
                    p["trending_str"]      = f"🔥 #{rank}" if sig_key == "trending"    else "—"
                    p["most_viewed_str"]   = f"👁 #{rank}" if sig_key == "most_viewed" else "—"
                    p["_best_seller_rank"] = bs
                    p["best_seller_str"]   = f"🛒 #{bs}" if bs and bs <= 50 else "—"
                    # Zero out the other signal rank
                    other_rk = "_most_viewed_rank" if sig_key == "trending" else "_trending_rank"
                    p.setdefault(other_rk, None)

                # Sort by rank ascending
                products.sort(key=lambda p: p.get(rk) or 99)
                result[sig_key] = products
                logger.info(f"  {name} {sig_key}: {len(products)} products with full details")

            except Exception as e:
                logger.warning(f"Signal error [{name}/{sig_key}]: {e}")

        return result

    async def test_connection(self) -> tuple:
        async with aiohttp.ClientSession() as session:
            url    = f"{BB_BASE}/products(search=laptop)"
            params = {"apiKey": self.api_key, "format": "json", "show": "sku,name,salePrice", "pageSize": "3"}
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                    if r.status == 403:
                        return False, "API key invalid or rate limited (403)", ""
                    if r.status != 200:
                        return False, f"HTTP {r.status}", ""
                    data     = await r.json()
                    products = data.get("products", [])
                    if not products:
                        return False, "No products returned", ""
                    name, cat_id = CATEGORIES[0]
                    cat          = await self._fetch_category(session, name, cat_id)
                    sample       = products[0].get("name", "—")[:60]
                    return True, len(products), f"{sample} | {name}: {len(cat)} products"
            except Exception as e:
                return False, f"Connection error: {e}", ""
