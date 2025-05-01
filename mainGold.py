import os
import sqlite3
import logging
import asyncio
import random
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Կարգավորել logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Կարգավորումներ
BOT_TOKEN = os.getenv("BOT_TOKEN", "7325788973:AAFX0CIPGLUVIWR10RD40Qp2IoWYFuboD2E")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://fuzzy-journey.onrender.com")
PORT = int(os.getenv("PORT", 10000))

# Ստուգել տոկենը
if not BOT_TOKEN:
    logger.error("BOT_TOKEN միջավայրի փոփոխականը սահմանված չէ։ Խնդրում եմ սահմանեք այն։")
    raise ValueError("BOT_TOKEN-ը պարտադիր է։")

# Տվյալների բազայի սկզբնավորում
def init_db():
    db_path = 'lotto.db'
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        first_name TEXT,
        balance INTEGER DEFAULT 0
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS games (
        game_id INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_id INTEGER,
        is_private INTEGER,
        status TEXT,
        start_time REAL
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS game_players (
        game_id INTEGER,
        user_id INTEGER,
        card TEXT,
        PRIMARY KEY (game_id, user_id)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS waiting_list (
        game_id INTEGER,
        user_id INTEGER,
        PRIMARY KEY (game_id, user_id)
    )''')
    conn.commit()
    conn.close()

# Օգտատիրոջ ստեղծում
def create_user(user_id, first_name):
    db_path = 'lotto.db'
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, first_name) VALUES (?, ?)", (user_id, first_name))
    conn.commit()
    conn.close()

# Քարտի գեներացում
def generate_card():
    numbers = random.sample(range(1, 91), 15)
    return ','.join(map(str, numbers))

# Խաղի ստեղծում
def create_game(creator_id, is_private):
    db_path = 'lotto.db'
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("INSERT INTO games (creator_id, is_private, status, start_time) VALUES (?, ?, ?, ?)",
              (creator_id, is_private, 'waiting', time.time()))
    game_id = c.lastrowid
    conn.commit()
    conn.close()
    return game_id

# Խաղացողի ավելացում
def add_player(game_id, user_id, card):
    db_path = 'lotto.db'
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO game_players (game_id, user_id, card) VALUES (?, ?, ?)",
              (game_id, user_id, card))
    conn.commit()
    conn.close()

# Start հրաման
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    create_user(user.id, user.first_name)
    
    keyboard = [
        [InlineKeyboardButton("🎮 Խաղալ", callback_data='play')],
        [InlineKeyboardButton("🎉 Խաղալ ընկերների հետ", callback_data='play_friends')],
        [InlineKeyboardButton("⏳ Սպասել", callback_data='wait')],
        [InlineKeyboardButton("❓ Օգնություն", callback_data='help')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        f"Բարև, {user.first_name}!\nԲարի գալուստ խաղ։ Ընտրեք տարբերակ՝",
        reply_markup=reply_markup
    )

# Օգնության հրաման
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📜 Օգնություն:\n"
        "🎮 Խաղալ — Միանալ հանրային խաղին (60 վրկ սպասում, 2+ խաղացող)\n"
        "🎉 Խաղալ ընկերների հետ — Ստեղծել մասնավոր խաղ (10 վրկ սպասում)\n"
        "⏳ Սպասել — Ծանուցում ստանալ խաղի ավարտի մասին\n"
        "❓ Օգնություն — Ցույց տալ այս տեքստը"
    )

# Խաղի ավարտ
async def end_game(context: ContextTypes.DEFAULT_TYPE, game_id, winner_id):
    db_path = 'lotto.db'
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    c.execute("SELECT user_id, card FROM game_players WHERE game_id = ?", (game_id,))
    players = c.fetchall()
    
    c.execute("SELECT first_name FROM users WHERE user_id = ?", (winner_id,))
    winner_name = c.fetchone()[0]
    
    card_text = "\n".join([f"🎴 {context.bot.get_user(p[0]).first_name}: {p[1]}" for p in players])
    
    for player_id, _ in players:
        try:
            if int(player_id) == winner_id:
                await context.bot.send_message(
                    player_id,
                    f"🎉 Շնորհավորում ենք, {winner_name}։ Դուք հաղթեցիք։\n{card_text}\n"
                    "📜 Բոլոր քարտերը ջնջվեցին։ Ստեղծե՞լ նոր խաղ։"
                )
            else:
                await context.bot.send_message(
                    player_id,
                    f"🏁 Խաղն ավարտվեց։ Հաղթող՝ {winner_name}\n{card_text}\n"
                    "📜 Բոլոր քարտերը ջնջվեցին։ Ստեղծե՞լ նոր խաղ։"
                )
        except Exception as e:
            logger.error(f"Սխալ խաղացող {player_id}-ին ծանուցելիս: {e}")
    
    c.execute("SELECT user_id FROM waiting_list WHERE game_id = ?", (game_id,))
    waiting_users = c.fetchall()
    for user_id in waiting_users:
        try:
            await context.bot.send_message(
                user_id[0],
                f"🏁 Խաղ #{game_id} ավարտվեց։ Հաղթող՝ {winner_name}\n"
                "🎮 Ստեղծե՞լ նոր խաղ։"
            )
        except Exception as e:
            logger.error(f"Սխալ սպասող {user_id[0]}-ին ծանուցելիս: {e}")
    
    c.execute("DELETE FROM game_players WHERE game_id = ?", (game_id,))
    c.execute("DELETE FROM waiting_list WHERE game_id = ?", (game_id,))
    c.execute("DELETE FROM games WHERE game_id = ?", (game_id,))
    conn.commit()
    conn.close()

# Կոճակների մշակում
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    
    if query.data == 'play':
        db_path = 'lotto.db'
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT game_id FROM games WHERE status = 'waiting' AND is_private = 0")
        game = c.fetchone()
        
        if game:
            game_id = game[0]
            card = generate_card()
            add_player(game_id, user.id, card)
            await query.message.reply_text(f"✅ Դուք միացաք խաղ #{game_id}-ին։ Ձեր քարտը՝ {card}")
        else:
            game_id = create_game(user.id, is_private=0)
            card = generate_card()
            add_player(game_id, user.id, card)
            await query.message.reply_text(f"🎲 Նոր խաղ #{game_id} ստեղծվեց։ Ձեր քարտը՝ {card}")
            context.job_queue.run_once(start_public_game, 60, data={'game_id': game_id}, name=f"start_game_{game_id}")
        
        conn.close()
    
    elif query.data == 'play_friends':
        game_id = create_game(user.id, is_private=1)
        card = generate_card()
        add_player(game_id, user.id, card)
        
        keyboard = [[InlineKeyboardButton("🚀 Սկսել խաղը", callback_data=f'start_private_{game_id}')]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(
            f"🎉 Մասնավոր խաղ #{game_id} ստեղծվեց։ Ձեր քարտը՝ {card}\n"
            "Հրավիրեք ընկերներին ստորև նշված հղումով։",
            reply_markup=reply_markup
        )
        await query.message.reply_text(f"🔗 Հրավերի հղում: https://t.me/{context.bot.username}?start=game_{game_id}")
        context.job_queue.run_once(start_private_game, 10, data={'game_id': game_id}, name=f"start_game_{game_id}")
    
    elif query.data.startswith('start_private_'):
        game_id = int(query.data.split('_')[-1])
        await start_private_game(context, {'game_id': game_id})
    
    elif query.data == 'wait':
        db_path = 'lotto.db'
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        c.execute("SELECT game_id FROM games WHERE status = 'waiting' AND is_private = 0")
        game = c.fetchone()
        
        if game:
            game_id = game[0]
            c.execute("INSERT OR IGNORE INTO waiting_list (game_id, user_id) VALUES (?, ?)", (game_id, user.id))
            conn.commit()
            await query.message.reply_text(f"⏳ Դուք ավելացվեցիք խաղ #{game_id}-ի սպասման ցուցակում։")
        else:
            await query.message.reply_text("❌ Ներկայումս հանրային խաղեր չկան։ Ստեղծե՞լ նոր խաղ։")
        
        conn.close()
    
    elif query.data == 'help':
        await query.message.reply_text(
            "📜 Օգնություն:\n"
            "🎮 Խաղալ — Միանալ հանրային խաղին\n"
            "🎉 Խաղալ ընկերների հետ — Ստեղծել մասնավոր խաղ\n"
            "⏳ Սպասել — Ծանուցում ստանալ խաղի ավարտի մասին\n"
            "❓ Օգնություն — Ցույց տալ այս տեքստը"
        )

# Հանրային խաղի մեկնարկ
async def start_public_game(context: ContextTypes.DEFAULT_TYPE, job):
    game_id = job.data['game_id']
    db_path = 'lotto.db'
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    c.execute("SELECT user_id FROM game_players WHERE game_id = ?", (game_id,))
    players = c.fetchall()
    
    if len(players) < 2:
        for player_id in players:
            try:
                await context.bot.send_message(
                    player_id[0],
                    f"❌ Խաղ #{game_id} չսկսվեց, քանի որ բավարար խաղացողներ չկան։"
                )
            except Exception as e:
                logger.error(f"Սխալ խաղացող {player_id[0]}-ին ծանուցելիս: {e}")
        
        c.execute("DELETE FROM games WHERE game_id = ?", (game_id,))
        c.execute("DELETE FROM game_players WHERE game_id = ?", (game_id,))
        conn.commit()
        conn.close()
        return
    
    c.execute("UPDATE games SET status = 'started' WHERE game_id = ?", (game_id,))
    conn.commit()
    
    await context.bot.send_message(
        players[0][0],
        f"🎲 Խաղ #{game_id} սկսվեց։ {len(players)} խաղացող։"
    )
    
    numbers = random.sample(range(1, 91), 5)
    for num in numbers:
        await context.bot.send_message(
            players[0][0],
            f"🎰 ԹԻՎ՝ {num}"
        )
        await asyncio.sleep(3)
    
    winner_id = players[0][0]
    await end_game(context, game_id, winner_id)
    
    conn.close()

# Մասնավոր խաղի մեկնարկ
async def start_private_game(context: ContextTypes.DEFAULT_TYPE, job_or_data):
    game_id = job_or_data['game_id'] if isinstance(job_or_data, dict) else job_or_data.data['game_id']
    db_path = 'lotto.db'
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    
    c.execute("SELECT user_id FROM game_players WHERE game_id = ?", (game_id,))
    players = c.fetchall()
    
    if len(players) < 1:
        for player_id in players:
            try:
                await context.bot.send_message(
                    player_id[0],
                    f"❌ Խաղ #{game_id} չսկսվեց, քանի որ խաղացողներ չկան։"
                )
            except Exception as e:
                logger.error(f"Սխալ խաղացող {player_id[0]}-ին ծանուցելիս: {e}")
        
        c.execute("DELETE FROM games WHERE game_id = ?", (game_id,))
        c.execute("DELETE FROM game_players WHERE game_id = ?", (game_id,))
        conn.commit()
        conn.close()
        return
    
    c.execute("UPDATE games SET status = 'started' WHERE game_id = ?", (game_id,))
    conn.commit()
    
    await context.bot.send_message(
        players[0][0],
        f"🎲 Մասնավոր խաղ #{game_id} սկսվեց։ {len(players)} խաղացող։"
    )
    
    numbers = random.sample(range(1, 91), 5)
    for num in numbers:
        await context.bot.send_message(
            players[0][0],
            f"🎰 Հանված թիվ՝ {num}"
        )
        await asyncio.sleep(3)
    
    winner_id = players[0][0]
    await end_game(context, game_id, winner_id)
    
    conn.close()

# Տեքստային հաղորդագրությունների մշակում
async def handle_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text.startswith('/start game_'):
        game_id = int(update.message.text.split('_')[-1])
        user = update.effective_user
        db_path = 'lotto.db'
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
        
        c.execute("SELECT status, is_private FROM games WHERE game_id = ?", (game_id,))
        game = c.fetchone()
        
        if game and game[1] == 1 and game[0] == 'waiting':
            card = generate_card()
            add_player(game_id, user.id, card)
            await update.message.reply_text(f"✅ Դուք միացաք մասնավոր խաղ #{game_id}-ին։ Ձեր քարտը՝ {card}")
        else:
            await update.message.reply_text("❌ Խաղը գոյություն չունի կամ ավարտվել է։")
        
        conn.close()
    else:
        await update.message.reply_text("Խնդրում եմ օգտագործել կոճակները կամ հրամանները։")

# Հիմնական ֆունկցիա
async def main():
    # Սկզբնավորել տվյալների բազան
    init_db()
    
    # Ստեղծել բոտի application
    application = None
    try:
        application = Application.builder().token(BOT_TOKEN).build()
        await application.initialize()
        logger.info("Application սկզբնավորված է")
    except Exception as e:
        logger.error(f"Application ստեղծման սխալ: {e}")
        return
    
    # Ջնջել հին webhook-ը և կարգավորել նորը
    try:
        await application.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Հին webhook ջնջված է")
        await application.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
        logger.info(f"Webhook կարգավորված է՝ {WEBHOOK_URL}")
    except Exception as e:
        logger.error(f"Webhook-ի կարգավորման սխալ: {e}")
        await application.shutdown()
        return
    
    # Ավելացնել handler-ներ
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", show_help))
    application.add_handler(CallbackQueryHandler(button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyboard))
    
    # Գործարկել webhook
    try:
        logger.info(f"Starting webhook on port {PORT}")
        await application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path="",
            webhook_url=WEBHOOK_URL,
            drop_pending_updates=True
        )
    except Exception as e:
        logger.error(f"Webhook-ի գործարկման սխալ: {e}")
    finally:
        try:
            if application:
                if application.updater:
                    await application.updater.stop()
                await application.stop()
                await application.shutdown()
                logger.info("Application կանգնեցված է")
        except Exception as e:
            logger.error(f"Application-ի փակման սխալ: {e}")

if __name__ == '__main__':
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Բոտը կանգնեցված է օգտատիրոջ կողմից")
    except Exception as e:
        logger.error(f"Հիմնական սխալ: {e}")
    finally:
        if not loop.is_closed():
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()
            logger.info("Event loop փակված է")
