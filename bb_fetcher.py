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
    "url", "bestSellingRank", "priceUpdateDate", "condition"
])

# How many products to pull per category into the pool.
# Full report displays top 10; signal filters search the full pool for matches.
POOL_SIZE     = 50
DISPLAY_SIZE  = 10


class BBFetcher:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch_all(self) -> dict:
        async with aiohttp.ClientSession() as session:
            category_tasks = [
                self._fetch_category(session, name, cat_id)
                for name, cat_id in CATEGORIES
            ]
            signal_tasks = [
                self._fetch_signals(session, name, cat_id)
                for name, cat_id in CATEGORIES
            ]
            cat_results    = await asyncio.gather(*category_tasks, return_exceptions=True)
            signal_results = await asyncio.gather(*signal_tasks,   return_exceptions=True)

        output = {}
        for i, (name, _) in enumerate(CATEGORIES):
            all_products = cat_results[i]    if not isinstance(cat_results[i],    Exception) else []
            signals      = signal_results[i] if not isinstance(signal_results[i], Exception) else {}

            if isinstance(cat_results[i],    Exception): logger.error(f"Category fetch failed [{name}]: {cat_results[i]}")
            if isinstance(signal_results[i], Exception): logger.error(f"Signal fetch failed [{name}]: {signal_results[i]}")

            trending_skus    = signals.get("trendingViewed", [])
            most_viewed_skus = signals.get("mostViewed",     [])

            trending_rank    = {sku: rank+1 for rank, sku in enumerate(trending_skus)}
            most_viewed_rank = {sku: rank+1 for rank, sku in enumerate(most_viewed_skus)}

            # Annotate every product in the pool with signal data
            for p in all_products:
                sku = p.get("sku")
                tr  = trending_rank.get(sku)
                mv  = most_viewed_rank.get(sku)
                bs  = p.get("bestSellingRank")
                p["trending_rank"]    = tr
                p["most_viewed_rank"] = mv
                p["best_seller_rank"] = bs
                p["trending_str"]     = f"🔥 #{tr}" if tr and tr <= 10 else "—"
                p["most_viewed_str"]  = f"👁 #{mv}" if mv and mv <= 10 else "—"
                p["best_seller_str"]  = f"🛒 #{bs}" if bs and bs <= 10 else "—"

            # For the full report, top DISPLAY_SIZE by best seller rank.
            # Signal filters will search the full pool in report_builder.
            output[name] = {
                "products":     all_products[:DISPLAY_SIZE],   # shown in full report
                "pool":         all_products,                  # full pool for signal filters
            }

        return output

    async def _fetch_category(self, session, name: str, cat_id: str) -> list:
        url = f"{BB_BASE}/products(categoryPath.id={cat_id})"
        params = {
            "apiKey":    self.api_key,
            "format":    "json",
            "show":      SHOW_FIELDS,
            "sort":      "bestSellingRank.asc",
            "pageSize":  str(POOL_SIZE),
            "condition": "New",
        }
        logger.info(f"Fetching category: {name} ({cat_id}) — pool size {POOL_SIZE}")
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    logger.error(f"BB API {resp.status} for {name}: {txt[:200]}")
                    return []
                data = await resp.json()
                products = data.get("products", [])
                # Safety net: keep only New condition items
                products = [p for p in products
                            if (p.get("condition") or "New").strip().lower() in ("new", "")]
                logger.info(f"  {name}: {len(products)} New products in pool")
                return products
        except Exception as e:
            logger.error(f"Fetch error [{name}]: {e}")
            return []

    async def _fetch_signals(self, session, name: str, cat_id: str) -> dict:
        signals   = {}
        endpoints = {
            "trendingViewed": f"{BB_BASE}/products/trendingViewed(categoryId={cat_id})",
            "mostViewed":     f"{BB_BASE}/products/mostViewed(categoryId={cat_id})",
        }
        params = {
            "apiKey":   self.api_key,
            "format":   "json",
            "show":     "sku,rank",
            "pageSize": "10",
        }
        for signal_name, url in endpoints.items():
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data  = await resp.json()
                        items = data.get("results") or data.get("products") or []
                        signals[signal_name] = [item.get("sku") for item in items if item.get("sku")]
                        logger.info(f"  {name} {signal_name}: {len(signals[signal_name])} SKUs")
                    else:
                        logger.warning(f"Signal {signal_name} [{name}] returned {resp.status}")
                        signals[signal_name] = []
            except Exception as e:
                logger.warning(f"Signal fetch error [{name}/{signal_name}]: {e}")
                signals[signal_name] = []
        return signals

    async def test_connection(self) -> tuple:
        async with aiohttp.ClientSession() as session:
            url    = f"{BB_BASE}/products(search=laptop)"
            params = {"apiKey": self.api_key, "format": "json", "show": "sku,name,salePrice", "pageSize": "3"}
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 403:
                        return False, "API key invalid or rate limited (403)", ""
                    if resp.status != 200:
                        txt = await resp.text()
                        return False, f"HTTP {resp.status}: {txt[:100]}", ""
                    data     = await resp.json()
                    products = data.get("products", [])
                    if products:
                        name, cat_id  = CATEGORIES[0]
                        cat_products  = await self._fetch_category(session, name, cat_id)
                        sample        = products[0].get("name", "—")[:60]
                        return True, len(products), f"{sample} | Category [{name}]: {len(cat_products)} in pool"
                    return False, "API connected but no products returned", ""
            except Exception as e:
                return False, f"Connection error: {e}", ""
