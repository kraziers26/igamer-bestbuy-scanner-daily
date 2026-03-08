import aiohttp
import asyncio
import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

BB_BASE = "https://api.bestbuy.com/v1"

CATEGORIES = [
    ("Gaming Desktops",  "pcmcat287600050002"),
    ("Gaming Laptops",   "pcmcat287600050003"),
    ("MacBooks",         "pcmcat247400050001"),
    ("All-in-One PCs",   "abcat0501005"),
    ("Windows Laptops",  "pcmcat247400050000"),
]

# Fallback search terms if category ID returns empty
CATEGORY_FALLBACKS = {
    "Gaming Laptops":  "categoryPath.name=Gaming Laptops",
    "Gaming Desktops": "categoryPath.name=Gaming Desktops",
}

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


def fresh_deal_score(p: dict) -> int:
    """
    Score products by deal freshness + discount depth.
    Replaces the old signal score which relied on trending/most-viewed.
    Goal: surface new price cuts before the crowd notices them.
    """
    score = 0

    # Deal freshness — how recently did the price drop?
    price_date = p.get("priceUpdateDate")
    if price_date:
        try:
            dt   = datetime.fromisoformat(price_date.replace("Z", "+00:00"))
            now  = datetime.now(dt.tzinfo)
            days = (now - dt).days
            if days == 0:  score += 4   # dropped today
            elif days <= 2: score += 3  # dropped in last 2 days
            elif days <= 7: score += 1  # still relatively fresh
        except Exception:
            pass

    # Must be on sale to score discount points
    if p.get("onSale"):
        score += 2

    # Discount depth
    pct = float(p.get("percentSavings") or 0)
    if pct >= 20:   score += 3
    elif pct >= 10: score += 2
    elif pct >= 5:  score += 1

    # Dollar savings — meaningful cuts on high-ticket items
    save_d = float(p.get("dollarSavings") or 0)
    if save_d >= 300:   score += 2
    elif save_d >= 100: score += 1

    # Proven product — has a global best seller rank (not obscure)
    bs = p.get("bestSellingRank")
    if bs and bs <= 500: score += 1

    return score


def deal_freshness_label(p: dict) -> str:
    """Human readable freshness label for the deal age column."""
    price_date = p.get("priceUpdateDate")
    if not price_date:
        return "—"
    try:
        dt   = datetime.fromisoformat(price_date.replace("Z", "+00:00"))
        now  = datetime.now(dt.tzinfo)
        days = (now - dt).days
        if days == 0:    return "🟢 New today"
        if days <= 2:    return f"🟢 {days}d new"
        if days <= 7:    return f"🟡 {days}d active"
        if days <= 14:   return f"🟠 {days}d aging"
        return                  f"🔴 {days}d old"
    except Exception:
        return "—"


class BBFetcher:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def fetch_all(self) -> dict:
        async with aiohttp.ClientSession() as session:
            category_tasks = [
                self._fetch_category(session, name, cat_id)
                for name, cat_id in CATEGORIES
            ]
            fresh_deal_tasks = [
                self._fetch_fresh_deals(session, name, cat_id)
                for name, cat_id in CATEGORIES
            ]
            bestseller_tasks = [
                self._fetch_category_bestsellers(session, name, cat_id)
                for name, cat_id in CATEGORIES
            ]

            cat_results  = await asyncio.gather(*category_tasks,    return_exceptions=True)
            fresh_results = await asyncio.gather(*fresh_deal_tasks, return_exceptions=True)
            bs_results   = await asyncio.gather(*bestseller_tasks,  return_exceptions=True)

        output = {}
        for i, (name, _) in enumerate(CATEGORIES):
            cat_products   = cat_results[i]   if not isinstance(cat_results[i],   Exception) else []
            fresh_products = fresh_results[i] if not isinstance(fresh_results[i], Exception) else []
            bs_products    = bs_results[i]    if not isinstance(bs_results[i],    Exception) else []

            if isinstance(cat_results[i],   Exception): logger.error(f"Category fetch failed [{name}]: {cat_results[i]}")
            if isinstance(fresh_results[i], Exception): logger.error(f"Fresh deals fetch failed [{name}]: {fresh_results[i]}")
            if isinstance(bs_results[i],    Exception): logger.error(f"Bestseller fetch failed [{name}]: {bs_results[i]}")

            # Build category bestseller rank map (category-specific, not global)
            cat_bs_rank = {p.get("sku"): idx+1 for idx, p in enumerate(bs_products)}

            # Annotate category pool with fresh deal score + category BS rank
            for p in cat_products:
                sku = p.get("sku")
                bs_global   = p.get("bestSellingRank")
                bs_cat_rank = cat_bs_rank.get(sku)
                p["fresh_score"]       = fresh_deal_score(p)
                p["freshness_label"]   = deal_freshness_label(p)
                p["best_seller_rank"]  = bs_global
                p["cat_bs_rank"]       = bs_cat_rank
                # Show category rank if available, fall back to global
                if bs_cat_rank:
                    p["best_seller_str"] = f"🛒 #{bs_cat_rank} in cat"
                elif bs_global:
                    p["best_seller_str"] = f"🛒 #{bs_global} global"
                else:
                    p["best_seller_str"] = "—"
                # Keep these for backwards compat with report_builder
                p["trending_rank"]    = None
                p["most_viewed_rank"] = None
                p["trending_str"]     = "—"
                p["most_viewed_str"]  = "—"

            # Annotate fresh deals pool
            fresh_skus = {p.get("sku") for p in cat_products}
            for p in fresh_products:
                sku = p.get("sku")
                bs_global   = p.get("bestSellingRank")
                bs_cat_rank = cat_bs_rank.get(sku)
                p["fresh_score"]      = fresh_deal_score(p)
                p["freshness_label"]  = deal_freshness_label(p)
                p["best_seller_rank"] = bs_global
                p["cat_bs_rank"]      = bs_cat_rank
                if bs_cat_rank:
                    p["best_seller_str"] = f"🛒 #{bs_cat_rank} in cat"
                elif bs_global:
                    p["best_seller_str"] = f"🛒 #{bs_global} global"
                else:
                    p["best_seller_str"] = "—"
                p["trending_rank"]    = None
                p["most_viewed_rank"] = None
                p["trending_str"]     = "—"
                p["most_viewed_str"]  = "—"

            output[name] = {
                "products":       cat_products[:DISPLAY_SIZE],  # top 10 for full report
                "pool":           cat_products,                  # full pool for on_sale/hot filters
                "fresh_products": fresh_products,                # sorted by priceUpdateDate desc
                "bs_products":    bs_products,                   # category bestseller list
            }

        return output

    async def _fetch_category(self, session, name: str, cat_id: str) -> list:
        """Fetch top POOL_SIZE products sorted by bestSellingRank."""
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
                    products = []
                else:
                    data     = await resp.json()
                    products = [p for p in data.get("products", []) if is_new(p)]

            # If empty, try fallback search term for this category
            if not products and name in CATEGORY_FALLBACKS:
                logger.warning(f"  {name}: empty via ID, trying fallback search")
                fallback_url = f"{BB_BASE}/products({CATEGORY_FALLBACKS[name]})"
                async with session.get(fallback_url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp2:
                    if resp2.status == 200:
                        data2    = await resp2.json()
                        products = [p for p in data2.get("products", []) if is_new(p)]
                        logger.info(f"  {name}: {len(products)} products via fallback")

            logger.info(f"  {name}: {len(products)} products in pool")
            return products
        except Exception as e:
            logger.error(f"Fetch error [{name}]: {e}")
            return []

    async def _fetch_fresh_deals(self, session, name: str, cat_id: str) -> list:
        """
        Fetch products sorted by priceUpdateDate descending — newest price
        changes first. This surfaces deals that just dropped before they trend.
        """
        url    = f"{BB_BASE}/products(categoryPath.id={cat_id}&onSale=true)"
        params = {
            "apiKey":   self.api_key,
            "format":   "json",
            "show":     SHOW_FIELDS,
            "sort":     "priceUpdateDate.dsc",
            "pageSize": str(POOL_SIZE),
        }
        logger.info(f"Fetching fresh deals: {name}")
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    # Fallback without onSale filter
                    url2 = f"{BB_BASE}/products(categoryPath.id={cat_id})"
                    async with session.get(url2, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp2:
                        if resp2.status != 200:
                            return []
                        data = await resp2.json()
                else:
                    data = await resp.json()

                products = [p for p in data.get("products", []) if is_new(p)]
                # Sort by fresh_deal_score descending after fetching
                for p in products:
                    p["_pre_score"] = fresh_deal_score(p)
                products.sort(key=lambda p: p["_pre_score"], reverse=True)
                logger.info(f"  {name} fresh deals: {len(products)} products")
                return products
        except Exception as e:
            logger.error(f"Fresh deals fetch error [{name}]: {e}")
            return []

    async def _fetch_category_bestsellers(self, session, name: str, cat_id: str) -> list:
        """
        Fetch the category-specific bestseller list using BB's mostPopular
        recommendations endpoint. Returns products in bestseller rank order.
        """
        url    = f"{BB_BASE}/products/mostPopular(categoryId={cat_id})"
        params = {
            "apiKey":   self.api_key,
            "format":   "json",
            "show":     "sku,rank",
            "pageSize": "10",
        }
        logger.info(f"Fetching category bestsellers: {name}")
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning(f"  {name} bestsellers: {resp.status}")
                    return []
                data  = await resp.json()
                items = data.get("results") or data.get("products") or []
                skus  = [item.get("sku") for item in items if item.get("sku")]

                if not skus:
                    return []

                # Look up full product details for these SKUs
                sku_filter  = ",".join(str(s) for s in skus)
                prod_url    = f"{BB_BASE}/products(sku in({sku_filter}))"
                prod_params = {
                    "apiKey":   self.api_key,
                    "format":   "json",
                    "show":     SHOW_FIELDS,
                    "pageSize": "10",
                }
                async with session.get(prod_url, params=prod_params, timeout=aiohttp.ClientTimeout(total=15)) as presp:
                    if presp.status != 200:
                        return []
                    pdata    = await presp.json()
                    products = [p for p in pdata.get("products", []) if is_new(p)]

                # Re-sort by original rank order from mostPopular
                sku_order = {str(sku): idx for idx, sku in enumerate(skus)}
                products.sort(key=lambda p: sku_order.get(str(p.get("sku")), 99))
                logger.info(f"  {name} bestsellers: {len(products)} products")
                return products
        except Exception as e:
            logger.warning(f"  {name} bestsellers fetch error: {e}")
            return []

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
