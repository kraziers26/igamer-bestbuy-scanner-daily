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
    "url", "bestSellingRank", "priceUpdateDate"
])

POOL_SIZE    = 50
DISPLAY_SIZE = 10

EXCLUDE_WORDS = ("refurbished", "open-box", "open box", "pre-owned", "preowned", "renewed")

def is_new(p: dict) -> bool:
    return not any(w in (p.get("name") or "").lower() for w in EXCLUDE_WORDS)


class BBFetcher:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch_all(self) -> dict:
        async with aiohttp.ClientSession() as session:
            # Run all category + signal fetches concurrently
            category_tasks = [
                self._fetch_category(session, name, cat_id)
                for name, cat_id in CATEGORIES
            ]
            signal_tasks = [
                self._fetch_signal_products(session, name, cat_id)
                for name, cat_id in CATEGORIES
            ]
            cat_results    = await asyncio.gather(*category_tasks, return_exceptions=True)
            signal_results = await asyncio.gather(*signal_tasks,   return_exceptions=True)

        output = {}
        for i, (name, _) in enumerate(CATEGORIES):
            cat_products = cat_results[i]    if not isinstance(cat_results[i],    Exception) else []
            sig_data     = signal_results[i] if not isinstance(signal_results[i], Exception) else {}

            if isinstance(cat_results[i],    Exception): logger.error(f"Category fetch failed [{name}]: {cat_results[i]}")
            if isinstance(signal_results[i], Exception): logger.error(f"Signal fetch failed [{name}]: {signal_results[i]}")

            # Signal products come back with full details + rank already set
            trending_products    = sig_data.get("trendingViewed", [])
            most_viewed_products = sig_data.get("mostViewed",     [])

            # Build SKU lookup maps for cross-referencing
            trending_rank    = {p["sku"]: p["trending_rank"]    for p in trending_products    if p.get("sku")}
            most_viewed_rank = {p["sku"]: p["most_viewed_rank"] for p in most_viewed_products if p.get("sku")}

            # Annotate category pool products with signal ranks
            for p in cat_products:
                sku = p.get("sku")
                tr  = trending_rank.get(sku)
                mv  = most_viewed_rank.get(sku)
                bs  = p.get("bestSellingRank")
                p["trending_rank"]    = tr
                p["most_viewed_rank"] = mv
                p["best_seller_rank"] = bs
                p["trending_str"]     = f"🔥 #{tr}" if tr else "—"
                p["most_viewed_str"]  = f"👁 #{mv}" if mv else "—"
                p["best_seller_str"]  = f"🛒 #{bs}" if bs and bs <= 50 else "—"

            # Annotate signal products that aren't already in category pool
            cat_skus = {p.get("sku") for p in cat_products}
            for p in trending_products + most_viewed_products:
                if p.get("sku") not in cat_skus:
                    bs = p.get("bestSellingRank")
                    p.setdefault("best_seller_rank", bs)
                    p["best_seller_str"] = f"🛒 #{bs}" if bs and bs <= 50 else "—"

            output[name] = {
                "products":          cat_products[:DISPLAY_SIZE],  # top 10 for full report
                "pool":              cat_products,                  # full 50 for on_sale / hot filters
                "trending_products": trending_products,             # direct signal products
                "most_viewed_products": most_viewed_products,
            }

        return output

    async def _fetch_category(self, session, name: str, cat_id: str) -> list:
        """Fetch top POOL_SIZE products by best seller rank for a category."""
        url    = f"{BB_BASE}/products(categoryPath.id={cat_id})"
        params = {
            "apiKey":   self.api_key,
            "format":   "json",
            "show":     SHOW_FIELDS,
            "sort":     "bestSellingRank.asc",
            "pageSize": str(POOL_SIZE),
        }
        logger.info(f"Fetching category: {name} ({cat_id})")
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    logger.error(f"BB API {resp.status} for {name}: {txt[:200]}")
                    return []
                data     = await resp.json()
                products = [p for p in data.get("products", []) if is_new(p)]
                logger.info(f"  {name}: {len(products)} products")
                return products
        except Exception as e:
            logger.error(f"Fetch error [{name}]: {e}")
            return []

    async def _fetch_signal_products(self, session, name: str, cat_id: str) -> dict:
        """
        Fetch trending + most viewed SKUs, then immediately look up full
        product details for those SKUs so signal filters have real data.
        """
        sig_endpoints = {
            "trendingViewed": f"{BB_BASE}/products/trendingViewed(categoryId={cat_id})",
            "mostViewed":     f"{BB_BASE}/products/mostViewed(categoryId={cat_id})",
        }
        sig_params = {
            "apiKey":   self.api_key,
            "format":   "json",
            "show":     "sku,rank",
            "pageSize": "10",
        }

        result = {}
        for sig_name, sig_url in sig_endpoints.items():
            try:
                async with session.get(sig_url, params=sig_params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning(f"Signal {sig_name} [{name}] returned {resp.status}")
                        result[sig_name] = []
                        continue

                    data  = await resp.json()
                    items = data.get("results") or data.get("products") or []
                    # Build ordered SKU list with ranks
                    ranked_skus = [(item.get("sku"), item.get("rank", idx+1))
                                   for idx, item in enumerate(items) if item.get("sku")]

                    if not ranked_skus:
                        result[sig_name] = []
                        continue

                    # Fetch full product details for these SKUs in one call
                    sku_filter = ",".join(str(s) for s, _ in ranked_skus)
                    prod_url   = f"{BB_BASE}/products(sku in({sku_filter}))"
                    prod_params = {
                        "apiKey":   self.api_key,
                        "format":   "json",
                        "show":     SHOW_FIELDS,
                        "pageSize": "10",
                    }
                    async with session.get(prod_url, params=prod_params, timeout=aiohttp.ClientTimeout(total=15)) as presp:
                        if presp.status != 200:
                            logger.warning(f"Product lookup failed for {sig_name} [{name}]: {presp.status}")
                            result[sig_name] = []
                            continue
                        pdata    = await presp.json()
                        products = [p for p in pdata.get("products", []) if is_new(p)]

                    # Attach rank and signal fields
                    rank_map = {str(sku): rank for sku, rank in ranked_skus}
                    rank_key = "trending_rank" if sig_name == "trendingViewed" else "most_viewed_rank"
                    str_key  = "trending_str"  if sig_name == "trendingViewed" else "most_viewed_str"
                    other_rank_key = "most_viewed_rank" if sig_name == "trendingViewed" else "trending_rank"
                    other_str_key  = "most_viewed_str"  if sig_name == "trendingViewed" else "trending_str"

                    for p in products:
                        sku  = str(p.get("sku", ""))
                        rank = rank_map.get(sku, 99)
                        bs   = p.get("bestSellingRank")
                        p[rank_key]       = rank
                        p[str_key]        = f"🔥 #{rank}" if sig_name == "trendingViewed" else f"👁 #{rank}"
                        p[other_rank_key] = None
                        p[other_str_key]  = "—"
                        p["best_seller_rank"] = bs
                        p["best_seller_str"]  = f"🛒 #{bs}" if bs and bs <= 50 else "—"

                    # Sort by rank
                    products.sort(key=lambda p: p.get(rank_key) or 99)
                    result[sig_name] = products
                    logger.info(f"  {name} {sig_name}: {len(products)} products with full details")

            except Exception as e:
                logger.warning(f"Signal product fetch error [{name}/{sig_name}]: {e}")
                result[sig_name] = []

        return result

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
                        name, cat_id = CATEGORIES[0]
                        cat_products = await self._fetch_category(session, name, cat_id)
                        sample       = products[0].get("name", "—")[:60]
                        return True, len(products), f"{sample} | Category [{name}]: {len(cat_products)} products"
                    return False, "API connected but no products returned", ""
            except Exception as e:
                return False, f"Connection error: {e}", ""
