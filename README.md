# iGamer Morning Report Bot

Sends your team a daily Best Buy Market Intelligence Excel report every morning at a scheduled time.

## What it does
- Pulls live data from Best Buy API across 5 categories (Gaming Desktops, Gaming Laptops, MacBooks, All-in-One, Windows Laptops)
- Fetches trending, most viewed, and best seller signals per category
- Builds a fully formatted Excel report with:
  - 📊 Summary tab with Top Deals ranked by signal score
  - 5 category tabs with live prices, discounts, signals, deal age, and Buy links
- Sends to a Telegram group/channel automatically every morning

---

## Railway Setup

### 1. Create a new Railway project
This is a **separate project** from the restock scanner bot.

### 2. Connect your GitHub repo (or deploy from local)

### 3. Set these Environment Variables in Railway:

| Variable            | Required | Description |
|---------------------|----------|-------------|
| `TELEGRAM_TOKEN`    | ✅        | Full bot token from BotFather (e.g. `1234567890:AAFxxx...`) |
| `BESTBUY_API_KEY`   | ✅        | Your Best Buy developer API key |
| `ADMIN_TELEGRAM_ID` | ✅        | Your personal Telegram user ID (get from @userinfobot) |
| `REPORT_CHAT_ID`    | ✅        | Chat/group ID to send the daily report to (see below) |
| `REPORT_HOUR_EST`   | ⚙️ optional | Hour to send (24h, EST). Default: `8` (8am EST) |

### 4. Set Procfile
Already included: `worker: python bot.py`

---

## Getting REPORT_CHAT_ID

**Option A — Use /setchat command:**
1. Add the bot to your Telegram group
2. Send `/start` then `/setchat` in the group
3. The bot will reply with the chat ID
4. Copy that ID into Railway as `REPORT_CHAT_ID`

**Option B — Manual:**
1. Add @userinfobot to your group
2. It will report the group's chat ID (negative number for groups, e.g. `-1001234567890`)

---

## Commands

| Command      | Who        | Description |
|--------------|------------|-------------|
| `/start`     | Anyone     | Show bot info |
| `/report`    | Admin only | Trigger the report right now (for testing) |
| `/schedule`  | Admin only | Show current schedule and next send time |
| `/setchat`   | Admin only | Set the current chat as report destination |
| `/settime`   | Admin only | Change the daily send time |
| `/test`      | Admin only | Test Best Buy API connection |

---

## File structure

```
igamer_morning_report/
├── bot.py              # Telegram bot + scheduler
├── bb_fetcher.py       # Best Buy API fetcher (categories + signals)
├── report_builder.py   # Excel report generator
├── requirements.txt
├── Procfile
└── README.md
```

---

## Report structure

**📊 SUMMARY tab:**
- Category overview (on sale counts, best discounts, hot buys)
- 🏆 Today's Top Deals — 🔴 MUST ACT and 🟠 WORTH LOOK tiers
- Signal key legend

**Category tabs (5 total):**
- Rank, Brand, Product Name, Sale Price, Reg Price, Save $, Save %
- On Sale, In Stock, 🔥 Trending, 👁 Most Viewed, 🛒 Best Seller
- Signal label, Deal Age, 🛒 Buy Now (clickable link)

**Deal Age column uses `priceUpdateDate` from BB API:**
- 🟢 New (0-2 days)
- 🟡 Active (3-7 days)  
- 🟠 Aging (8-14 days)
- 🔴 Ending? (15+ days)

---

## Costs
- Railway: ~$5/month (same as restock bot if on same account)
- Best Buy API: Free tier, no rate limit concerns for daily use
