import aiohttp
import asyncio
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

BB_BASE = "https://api.bestbuy.com/v1"

CATEGORIES = [
    ("Gaming Desktops",  "pcmcat287600050002"),
    ("Gaming Laptops",   "pcmcat287600050003"),
    ("MacBooks",         "pcmcat247400050001"),
    ("All-in-One PCs",   "abcat0501005"),
    ("Windows Laptops",  "pcmcat247400050000"),
]

CATEGORY_FALLBACKS = {
    "Gaming Laptops":  "categoryPath.name=Gaming Laptops",
    "Gaming Desktops": "categoryPath.name=Gaming Desktops",
}

# Fixed brand list for /report filter menu
BRANDS = ["ASUS", "HP", "Lenovo", "Dell", "MSI", "Acer", "Apple", "Samsung", "LG"]

# Price range buckets for /report filter menu
PRICE_RANGES = [
    ("Under $700",    None,  700),
    ("$700-$1,000",   700,  1000),
    ("$1,000-$1,500", 1000, 1500),
    ("Over $1,500",   1500, None),
]

SHOW_FIELDS = ",".join([
    "sku", "name", "manufacturer", "salePrice", "regularPrice",
    "dollarSavings", "percentSavings", "onSale", "onlineAvailability",
    "url", "bestSellingRank", "priceUpdateDate", "offers"
])

POOL_SIZE    = 50
DISPLAY_SIZE = 10

EXCLUDE_WORDS = ("refurbished", "open-box", "open box", "pre-owned", "preowned", "renewed")

ALERT_THRESHOLD = 9    # min fresh_score to trigger alert
ALERT_MAX_HOURS = 6    # price drop must be this recent to qualify


# ── Offer parsing ──────────────────────────────────────────────────────────────

def parse_offers(p: dict) -> dict:
    """
    Parse the offers array and return the highest-priority offer info.
    Priority: Deal of the Day > Clearance > Weekly Ad > Special Offer > any
    """
    offers = p.get("offers") or []
    if not offers:
        return {"offer_type": None, "offer_label": "", "offer_note": "", "offer_score_bonus": 0}

    def norm(s):
        return (s or "").lower().strip()

    best_priority = 0
    best = {"offer_type": None, "offer_label": "", "offer_note": "", "offer_score_bonus": 0}

    for offer in offers:
        ot     = offer.get("offerType", "") or ""
        desc   = offer.get("description", "") or ""
        ot_n   = norm(ot)
        desc_n = norm(desc)

        if "deal of the day" in ot_n or "deal of the day" in desc_n:
            priority   = 5
            candidate  = {
                "offer_type":        "Deal of the Day",
                "offer_label":       "DEAL OF THE DAY",
                "offer_note":        "Deal of the Day — valid until 11:59pm CT tonight",
                "offer_score_bonus": 4,
            }
        elif "clearance" in ot_n or "clearance" in desc_n:
            priority   = 4
            candidate  = {
                "offer_type":        "Clearance",
                "offer_label":       "CLEARANCE",
                "offer_note":        desc or "Clearance item — limited units, no raincheck",
                "offer_score_bonus": 3,
            }
        elif "weekly ad" in ot_n or "weekly ad" in desc_n or "circular" in ot_n:
            priority   = 3
            candidate  = {
                "offer_type":        "Weekly Ad",
                "offer_label":       "WEEKLY AD",
                "offer_note":        desc or "Weekly Ad — runs through Saturday",
                "offer_score_bonus": 2,
            }
        elif "special" in ot_n:
            priority   = 2
            candidate  = {
                "offer_type":        "Special Offer",
                "offer_label":       "SPECIAL OFFER",
                "offer_note":        desc or "",
                "offer_score_bonus": 1,
            }
        elif ot_n or desc_n:
            priority   = 1
            candidate  = {
                "offer_type":        ot or "Offer",
                "offer_label":       (ot or "OFFER").upper(),
                "offer_note":        desc or "",
                "offer_score_bonus": 1,
            }
        else:
            continue

        if priority > best_priority:
            best_priority = priority
            best = candidate

    return best


# ── Scoring & helpers ──────────────────────────────────────────────────────────

def fresh_deal_score(p: dict) -> int:
    score = 0

    price_date = p.get("priceUpdateDate")
    if price_date:
        try:
            dt   = datetime.fromisoformat(price_date.replace("Z", "+00:00"))
            now  = datetime.now(dt.tzinfo)
            days = (now - dt).days
            if days == 0:    score += 4
            elif days <= 2:  score += 3
            elif days <= 7:  score += 1
        except Exception:
            pass

    if p.get("onSale"):
        score += 2

    pct = float(p.get("percentSavings") or 0)
    if pct >= 20:   score += 3
    elif pct >= 10: score += 2
    elif pct >= 5:  score += 1

    save_d = float(p.get("dollarSavings") or 0)
    if save_d >= 300:   score += 2
    elif save_d >= 100: score += 1

    bs = p.get("bestSellingRank")
    if bs and bs <= 500: score += 1

    score += p.get("offer_score_bonus", 0)

    return score


def deal_freshness_label(p: dict) -> str:
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


def is_fresh_enough_for_alert(p: dict) -> bool:
    price_date = p.get("priceUpdateDate")
    if not price_date:
        return False
    try:
        dt  = datetime.fromisoformat(price_date.replace("Z", "+00:00"))
        now = datetime.now(dt.tzinfo)
        return (now - dt) <= timedelta(hours=ALERT_MAX_HOURS)
    except Exception:
        return False


def is_new(p: dict) -> bool:
    return not any(w in (p.get("name") or "").lower() for w in EXCLUDE_WORDS)


def is_in_stock(p: dict) -> bool:
    return bool(p.get("onlineAvailability", True))


def annotate_product(p: dict) -> dict:
    """Attach all derived fields. parse_offers must run before fresh_deal_score."""
    offer_data = parse_offers(p)
    p.update(offer_data)
    bs = p.get("bestSellingRank")
    p["fresh_score"]      = fresh_deal_score(p)
    p["freshness_label"]  = deal_freshness_label(p)
    p["best_seller_rank"] = bs
    p["best_seller_str"]  = f"🛒 #{bs}" if bs else "—"
    p["trending_rank"]    = None
    p["most_viewed_rank"] = None
    p["trending_str"]     = "—"
    p["most_viewed_str"]  = "—"
    return p


# ── BBFetcher ──────────────────────────────────────────────────────────────────

class BBFetcher:
    def __init__(self, api_key: str):
        self.api_key = api_key

    # ── Full fetch (morning report) ────────────────────────────────────────────

    async def fetch_all(self) -> dict:
        async with aiohttp.ClientSession() as session:
            cat_tasks   = [self._fetch_category(session, n, c)    for n, c in CATEGORIES]
            fresh_tasks = [self._fetch_fresh_deals(session, n, c)  for n, c in CATEGORIES]
            cat_results   = await asyncio.gather(*cat_tasks,   return_exceptions=True)
            fresh_results = await asyncio.gather(*fresh_tasks, return_exceptions=True)

        output = {}
        for i, (name, _) in enumerate(CATEGORIES):
            cat_prods   = cat_results[i]   if not isinstance(cat_results[i],   Exception) else []
            fresh_prods = fresh_results[i] if not isinstance(fresh_results[i], Exception) else []
            if isinstance(cat_results[i],   Exception):
                logger.error(f"Category fetch failed [{name}]: {cat_results[i]}")
            if isinstance(fresh_results[i], Exception):
                logger.error(f"Fresh deals fetch failed [{name}]: {fresh_results[i]}")

            for p in cat_prods + fresh_prods:
                annotate_product(p)

            output[name] = {
                "products":       cat_prods[:DISPLAY_SIZE],
                "pool":           cat_prods,
                "fresh_products": fresh_prods,
            }
        return output

    # ── Alert poll ─────────────────────────────────────────────────────────────

    async def fetch_for_alerts(self) -> list:
        """
        Lightweight fetch for price drop alert polling.
        Returns flat list of products that qualify:
          - in stock
          - price dropped within ALERT_MAX_HOURS
          - score >= ALERT_THRESHOLD OR is Deal of the Day
        """
        async with aiohttp.ClientSession() as session:
            fresh_tasks = [self._fetch_fresh_deals(session, n, c) for n, c in CATEGORIES]
            results     = await asyncio.gather(*fresh_tasks, return_exceptions=True)

        candidates = []
        for i, (name, _) in enumerate(CATEGORIES):
            prods = results[i] if not isinstance(results[i], Exception) else []
            if isinstance(results[i], Exception):
                logger.error(f"Alert fetch failed [{name}]: {results[i]}")
                continue
            for p in prods:
                annotate_product(p)
                p["_category"] = name
                if not is_in_stock(p):
                    continue
                if not is_fresh_enough_for_alert(p):
                    continue
                is_dotd = (p.get("offer_type") == "Deal of the Day")
                if p["fresh_score"] >= ALERT_THRESHOLD or is_dotd:
                    candidates.append(p)

        candidates.sort(
            key=lambda p: (p.get("offer_type") == "Deal of the Day", p["fresh_score"]),
            reverse=True
        )
        return candidates

    # ── Filtered fetch for /report menu (Option B) ─────────────────────────────

    async def fetch_filtered(
        self,
        cat_id: str,
        brand: str = None,
        price_min: float = None,
        price_max: float = None,
    ) -> list:
        """
        API-level filtered fetch for the /report on-demand menu.
        Filters applied in query string for accuracy over post-fetch filtering.
        """
        filters = [f"categoryPath.id={cat_id}", "onSale=true"]
        if brand:
            filters.append(f"manufacturer={brand}")
        if price_min is not None:
            filters.append(f"salePrice>={price_min}")
        if price_max is not None:
            filters.append(f"salePrice<{price_max}")

        url    = f"{BB_BASE}/products({'&'.join(filters)})"
        params = {
            "apiKey":   self.api_key,
            "format":   "json",
            "show":     SHOW_FIELDS,
            "sort":     "priceUpdateDate.dsc",
            "pageSize": str(POOL_SIZE),
        }

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                    if resp.status != 200:
                        # Retry without onSale filter
                        filters2 = [f for f in filters if f != "onSale=true"]
                        url2     = f"{BB_BASE}/products({'&'.join(filters2)})"
                        async with session.get(url2, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp2:
                            if resp2.status != 200:
                                return []
                            data = await resp2.json()
                    else:
                        data = await resp.json()

                products = [p for p in data.get("products", []) if is_new(p) and is_in_stock(p)]
                for p in products:
                    annotate_product(p)
                products.sort(key=lambda p: p["fresh_score"], reverse=True)
                return products

            except Exception as e:
                logger.error(f"Filtered fetch error: {e}")
                return []

    # ── alsoViewed (More like this) ────────────────────────────────────────────

    async def fetch_also_viewed(self, sku: str) -> list:
        """BB alsoViewed recommendations for a given SKU."""
        url    = f"{BB_BASE}/products/{sku}/alsoViewed"
        params = {"apiKey": self.api_key, "format": "json", "show": "sku,rank", "pageSize": "10"}

        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning(f"alsoViewed {sku}: {resp.status}")
                        return []
                    data  = await resp.json()
                    items = data.get("results") or data.get("products") or []
                    skus  = [str(item.get("sku")) for item in items if item.get("sku")]

                if not skus:
                    return []

                prod_url    = f"{BB_BASE}/products(sku in({','.join(skus)}))"
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
                    products = [p for p in pdata.get("products", []) if is_new(p) and is_in_stock(p)]

                for p in products:
                    annotate_product(p)
                products.sort(key=lambda p: p["fresh_score"], reverse=True)
                return products

            except Exception as e:
                logger.error(f"alsoViewed fetch error [{sku}]: {e}")
                return []

    # ── Internal fetchers ──────────────────────────────────────────────────────

    async def _fetch_category(self, session, name: str, cat_id: str) -> list:
        url    = f"{BB_BASE}/products(categoryPath.id={cat_id})"
        params = {
            "apiKey":   self.api_key,
            "format":   "json",
            "show":     SHOW_FIELDS,
            "sort":     "bestSellingRank.asc",
            "pageSize": str(POOL_SIZE),
        }
        logger.info(f"Fetching category pool: {name}")
        try:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    txt = await resp.text()
                    logger.error(f"BB API {resp.status} for {name}: {txt[:200]}")
                    products = []
                else:
                    data     = await resp.json()
                    products = [p for p in data.get("products", []) if is_new(p)]

            if not products and name in CATEGORY_FALLBACKS:
                logger.warning(f"  {name}: empty via ID, trying fallback")
                fallback_url = f"{BB_BASE}/products({CATEGORY_FALLBACKS[name]})"
                async with session.get(fallback_url, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp2:
                    if resp2.status == 200:
                        data2    = await resp2.json()
                        products = [p for p in data2.get("products", []) if is_new(p)]
                        logger.info(f"  {name}: {len(products)} via fallback")

            logger.info(f"  {name}: {len(products)} in pool")
            return products
        except Exception as e:
            logger.error(f"Fetch error [{name}]: {e}")
            return []

    async def _fetch_fresh_deals(self, session, name: str, cat_id: str) -> list:
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
                    url2 = f"{BB_BASE}/products(categoryPath.id={cat_id})"
                    async with session.get(url2, params=params, timeout=aiohttp.ClientTimeout(total=20)) as resp2:
                        if resp2.status != 200:
                            return []
                        data = await resp2.json()
                else:
                    data = await resp.json()

                products = [p for p in data.get("products", []) if is_new(p)]
                logger.info(f"  {name} fresh deals: {len(products)}")
                return products
        except Exception as e:
            logger.error(f"Fresh deals fetch error [{name}]: {e}")
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
                        return True, len(products), f"{sample} | [{name}]: {len(cat_products)} products"
                    return False, "API connected but no products returned", ""
            except Exception as e:
                return False, f"Connection error: {e}", ""
