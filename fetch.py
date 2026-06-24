import json
import os
import time
import requests
import logging
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from py_clob_client_v2 import (
    ClobClient,
    MarketOrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    Side,
)

# ==========================
# CONFIG
# ==========================
load_dotenv()

PRIVATE_KEY       = os.getenv("PRIVATE_KEY")
FUNDER            = os.getenv("FUNDER")
TELEGRAM_TOKEN    = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID  = int(os.getenv("TELEGRAM_CHAT_ID"))

if not all([PRIVATE_KEY, FUNDER, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID]):
    raise ValueError("Missing values in .env — check PRIVATE_KEY, FUNDER, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID")

HOST     = "https://clob.polymarket.com"
CHAIN_ID = 137

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ==========================
# CONVERSATION STATES
# ==========================
SLUG, AMOUNT, OUTCOME = range(3)

# ==========================
# POLYMARKET CLIENT
# ==========================
def build_client(creds=None):
    return ClobClient(
        host=HOST,
        chain_id=CHAIN_ID,
        key=PRIVATE_KEY,
        creds=creds,
        signature_type=1,
        funder=FUNDER,
        timeout=60,
    )

print("Authenticating with Polymarket...")
try:
    tmp    = build_client()
    creds  = tmp.create_or_derive_api_key()
    client = build_client(creds)
    print("Authenticated OK")
except Exception as e:
    print(f"Auth error: {e}")
    exit(1)

# ==========================
# POLYMARKET FUNCTIONS
# ==========================
def get_event(slug):
    r = requests.get(
        "https://gamma-api.polymarket.com/events",
        params={"slug": slug},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if not data:
        raise Exception(f"No event found for slug: '{slug}'")
    return data[0]


def list_outcomes(event_data):
    outcomes = []
    for market in event_data["markets"]:
        question = market.get("question") or market.get("title") or "Unknown"
        names    = json.loads(market["outcomes"])
        tok_ids  = json.loads(market["clobTokenIds"])
        tick     = str(
            market.get("minimumTickSize")
            or market.get("minTickSize")
            or "0.01"
        )
        for name, tid in zip(names, tok_ids):
            outcomes.append({
                "label":    f"{question}  →  {name}",
                "token_id": tid,
                "tick":     tick,
            })
    return outcomes


def get_best_ask(token_id):
    book = client.get_order_book(token_id)
    if not book.asks:
        raise Exception("No sellers in the order book right now.")
    return float(min(float(a.price) for a in book.asks))


def market_buy(token_id, usd_amount, tick_size, retries=3):
    best_ask = get_best_ask(token_id)
    for attempt in range(1, retries + 1):
        try:
            resp = client.create_and_post_market_order(
                order_args=MarketOrderArgs(
                    token_id=token_id,
                    amount=usd_amount,
                    side=Side.BUY,
                    order_type=OrderType.FAK,
                ),
                options=PartialCreateOrderOptions(tick_size=tick_size),
                order_type=OrderType.FAK,
            )
            return best_ask, resp
        except Exception as e:
            if attempt < retries:
                time.sleep(3)
            else:
                raise e


# ==========================
# SECURITY: only allow your chat
# ==========================
def is_authorized(update: Update) -> bool:
    return update.effective_chat.id == TELEGRAM_CHAT_ID


# ==========================
# BOT HANDLERS
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Welcome to your Polymarket trading bot!\n\n"
        "Commands:\n"
        "  /buy  — place a market buy order\n"
        "  /cancel — cancel current operation\n"
    )


async def buy_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        await update.message.reply_text("⛔ Unauthorized.")
        return ConversationHandler.END

    await update.message.reply_text(
        "📌 Send me the market slug.\n\n"
        "Example: `highest-temperature-in-lucknow-on-june-24-2026`\n\n"
        "You can find the slug in the Polymarket URL after `/event/`",
        parse_mode="Markdown",
    )
    return SLUG


async def receive_slug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    slug = update.message.text.strip()
    await update.message.reply_text(f"🔍 Looking up market: `{slug}`...", parse_mode="Markdown")

    try:
        event    = get_event(slug)
        outcomes = list_outcomes(event)

        if not outcomes:
            await update.message.reply_text("❌ No tradeable outcomes found for this market.")
            return ConversationHandler.END

        # store in context
        context.user_data["outcomes"] = outcomes
        context.user_data["event_title"] = event.get("title", slug)

        # build numbered list message
        lines = [f"📊 *{context.user_data['event_title']}*\n\nAvailable outcomes:\n"]
        for i, o in enumerate(outcomes, 1):
            lines.append(f"  `[{i:>2}]` {o['label']}")
        lines.append(f"\nReply with a number (1–{len(outcomes)})")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return OUTCOME

    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
        return ConversationHandler.END


async def receive_outcome(update: Update, context: ContextTypes.DEFAULT_TYPE):
    outcomes = context.user_data.get("outcomes", [])
    try:
        choice = int(update.message.text.strip())
        if not (1 <= choice <= len(outcomes)):
            await update.message.reply_text(f"Please enter a number between 1 and {len(outcomes)}.")
            return OUTCOME
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")
        return OUTCOME

    chosen = outcomes[choice - 1]
    context.user_data["token_id"]  = chosen["token_id"]
    context.user_data["tick_size"] = chosen["tick"]
    context.user_data["label"]     = chosen["label"]

    await update.message.reply_text(
        f"✅ Selected: *{chosen['label']}*\n\n"
        f"💵 How much USDC do you want to spend? (e.g. `5` or `10.50`)",
        parse_mode="Markdown",
    )
    return AMOUNT


async def receive_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        if amount <= 0:
            await update.message.reply_text("Amount must be greater than 0.")
            return AMOUNT
    except ValueError:
        await update.message.reply_text("Please enter a valid number (e.g. 5 or 10.50).")
        return AMOUNT

    token_id  = context.user_data["token_id"]
    tick_size = context.user_data["tick_size"]
    label     = context.user_data["label"]

    await update.message.reply_text(
        f"⏳ Placing order...\n\n"
        f"  Outcome : {label}\n"
        f"  Amount  : ${amount:.2f} USDC",
    )

    try:
        best_ask, resp = market_buy(token_id, amount, tick_size)

        success = resp.get("success", False) if isinstance(resp, dict) else True
        order_id = resp.get("orderID", "N/A") if isinstance(resp, dict) else str(resp)

        if success:
            await update.message.reply_text(
                f"✅ *Order placed!*\n\n"
                f"  Outcome  : {label}\n"
                f"  Spent    : ${amount:.2f} USDC\n"
                f"  Ask price: ${best_ask:.4f}\n"
                f"  Order ID : `{order_id}`",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                f"⚠️ Order submitted but may not have filled fully.\n"
                f"Response: `{resp}`",
                parse_mode="Markdown",
            )

    except Exception as e:
        await update.message.reply_text(
            f"❌ Order failed: {e}\n\n"
            f"Make sure your VPN is connected to USA/UK and try again."
        )

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❌ Cancelled.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command. Use /buy to place an order.")


# ==========================
# MAIN
# ==========================
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("buy", buy_start)],
        states={
            SLUG:    [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_slug)],
            OUTCOME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_outcome)],
            AMOUNT:  [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_amount)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    print("Bot is running... Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()