# iGamer Morning Report Bot — Best Buy Market Intelligence

Daily Best Buy market intelligence for the iGamer Corp team. Sends a Fresh Deals Excel report every morning at 8am, fires real-time price drop alerts throughout the day, and lets admins pull on-demand filtered reports via Telegram.

---

## What it does

- Pulls live data from Best Buy API across 5 categories: Gaming Desktops, Gaming Laptops, MacBooks, All-in-One PCs, Windows Laptops
- Scores every product with a **Fresh Deal Score** (0–13+ pts) based on price drop recency, discount depth, and active BB promotions
- Sends a full Excel report to the team group at 8am daily (Fresh Deals filter by default)
- Polls every 3 hours for new price drops and fires instant Telegram alerts for qualifying deals
- Detects BB offer types: Deal of the Day, Clearance, Weekly Ad, Special Offer — with score bonuses and badge labels
- Admins can pull on-demand reports filtered by category, price range, and brand via a 3-step menu
- "More like this" button on alerts pulls BB's alsoViewed recommendations for that product

---

## Fresh Deal Score (0–13+ pts)

| Signal | Points |
|--------|--------|
| Price dropped today | +4 |
| Price dropped within 2 days | +3 |
| Price dropped within 7 days | +1 |
| On sale | +2 |
| Discount ≥20% | +3 |
| Discount ≥10% | +2 |
| Discount ≥5% | +1 |
| Dollar savings ≥$300 | +2 |
| Dollar savings ≥$100 | +1 |
| Has BB best seller rank | +1 |
| Deal of the Day offer | +4 bonus |
| Clearance offer | +3 bonus |
| Weekly Ad offer | +2 bonus |
| Any other offer | +1 bonus |

**HOT BUY = score ≥ 9**
**Deal of the Day always triggers an alert regardless of score**

---

## Offer Badges

| Badge | Meaning | Row color in Excel |
|-------|---------|-------------------|
| 🎯 DEAL OF THE DAY | BB editorial pick, valid 24hrs until 11:59pm CT | Warm yellow |
| 🟣 CLEARANCE | Extreme discount, limited units, no raincheck | Purple |
| 📰 WEEKLY AD | In BB Sunday circular, runs 7 days | Green |
| ⭐ SPECIAL OFFER | Bundle, gift-with-purchase, financing promo | Blue tint |

---

## Price Drop Alerts

The bot polls Best Buy every 3 hours. A deal triggers an alert when:
- Price dropped within the last **6 hours**
- Product is **in stock**
- Fresh score ≥ 9 **OR** it has a Deal of the Day offer

Alerts include: product name, price, savings, drop time, Fresh Score, offer badge, and offer note.
Each alert has two buttons: **View on Best Buy** and **More like this**.

**Deduplication:** Each SKU is only alerted once per day. The seen-SKU cache resets at midnight EST so the next day starts fresh. If a product drops in price a second time on the same day it re-qualifies as a new alert.

---

## Railway Setup

### 1. Create a new Railway project
Separate from the restock scanner bot.

### 2. Set Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_TOKEN` | ✅ | Bot token from BotFather |
| `BESTBUY_API_KEY` | ✅ | Best Buy developer API key |
| `ADMIN_TELEGRAM_ID` | ✅ | Comma-separated Telegram user IDs for admins |
| `REPORT_CHAT_ID` | ✅ | Group chat ID to send reports and alerts to (negative number) |
| `REPORT_HOUR_EST` | optional | Hour to send daily report (24h EST). Default: `8` |

### 3. Procfile
Already included: `worker: python bot.py`

---

## Getting REPORT_CHAT_ID

1. Add the bot to your Telegram group
2. Send `/setchat` in the group
3. Bot replies with the chat ID — copy to Railway as `REPORT_CHAT_ID`

---

## Bot Commands

| Command | Who | Description |
|---------|-----|-------------|
| `/start` | Anyone | Show bot info, offer badge guide, and admin commands |
| `/report` | Admin | Pull on-demand report — 3-step filter menu (category → price → brand) |
| `/setschedule` | Admin | Change daily report time and default filter |
| `/schedule` | Admin | View current schedule and alert poll status |
| `/setchat` | Admin | Set current chat as report and alert destination |
| `/test` | Admin | Test Best Buy API connection |

---

## /report Filter Menu

Step-by-step inline keyboard:

1. **Category** — Gaming Laptops / Gaming Desktops / MacBooks / Windows Laptops / All-in-One PCs / All categories
2. **Price range** — Under $700 / $700–$1,000 / $1,000–$1,500 / Over $1,500 / Any price
3. **Brand** — ASUS / HP / Lenovo / Dell / MSI / Acer / Apple / Samsung / LG / Any brand
4. **Output type** — Fresh Deals list (Telegram) / HOT BUYS list (Telegram) / Full Excel report

Filters are applied at the API level (Option B) for accuracy — not post-fetch filtering.
Menu times out after **2 minutes** of inactivity with a clean cancellation message.

---

## Scheduled Report Filters

| Filter | Description | Default |
|--------|-------------|---------|
| 🆕 Fresh Deals | Newest price drops sorted by Fresh Deal Score | ✅ 8am default |
| 🔴 HOT BUYS Only | Score ≥9 only — highest conviction buys | |
| 💰 On Sale Only | All discounted products sorted by % off | |
| 🛒 Best Sellers | Sorted by BB global best seller rank | |
| 📦 Established Deals | Top products by BB popularity rank | |

---

## Excel Report Structure

**📊 SUMMARY tab:**
- Category overview: products, on sale count, best discount, hot buy count, avg price, top brand
- Today's Top Deals: MUST ACT (score ≥9) and WORTH LOOK (score 5–8) tiers with offer context
- Signal key legend including all offer types

**5 Category tabs:**
- Rank, Brand, Product Name, Sale Price, Reg Price, Save $, Save %
- On Sale, In Stock, BB Rank, Fresh Score, Offer Type, Signal, Deal Age, Buy Now link

**Row color coding:**
- 🟡 Warm yellow = Deal of the Day
- 🟣 Purple = Clearance
- 🔴 Red = HOT BUY (score ≥9, no special offer)
- 🟢 Green = 15%+ discount
- 🟡 Yellow = 5–14% discount
- ⬜ White = full price / low score
- Grey = out of stock

---

## File Structure

```
igamer_morning_report/
├── bot.py              # Telegram bot, scheduler, alert polling, /report menu
├── bb_fetcher.py       # BB API fetcher — offers parsing, alert fetch, filtered fetch, alsoViewed
├── report_builder.py   # Excel report generator with offer columns and color coding
├── requirements.txt
├── Procfile
└── README.md
```

---

## Tuning Constants (in bb_fetcher.py)

| Constant | Default | Description |
|----------|---------|-------------|
| `ALERT_THRESHOLD` | 9 | Minimum score to trigger a price drop alert |
| `ALERT_MAX_HOURS` | 6 | Max hours since price drop to qualify for alert |
| `POOL_SIZE` | 50 | Products fetched per category per pass |
| `DISPLAY_SIZE` | 10 | Products shown per category in full report |

Alert poll interval is set in `bot.py` — default 3 hours (`3 * 60 * 60` seconds).

---

## Costs

- Railway: ~$5/month
- Best Buy API: Free tier — daily report + 3-hourly alert polls well within rate limits
