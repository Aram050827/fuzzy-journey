import random
import time
import uuid
import os
import logging
import asyncio
import aiosqlite
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

# Set up logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Game settings
MIN_PLAYERS = 2
GAME_PAUSE = 10  # 10 seconds for private friend games
PUBLIC_GAME_PAUSE = 60  # 60 seconds for public games
MAX_NUMBER = 80
ADMIN_ID = 1878495685  # Replace with your admin user ID

# Number drawing intervals
PUBLIC_DRAW_INTERVAL = 5  # Seconds between drawn numbers in public games
PRIVATE_DRAW_INTERVAL = 5 # Seconds between drawn numbers in private games

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "7325788973:AAFX0CIPGLUVIWR10RD40Qp2IoWYFuboD2E")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://lottogram.onrender.com")
PORT = int(os.getenv("PORT", 10000))
DB_PATH = "lotto.db"  # Persistent disk path for Render

# Check token
if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable is not set. Please set it.")
    raise ValueError("BOT_TOKEN is required.")

# Database initialization
async def init_db():
    try:
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir)
            logger.info(f"Created directory for database: {db_dir}")

        async with aiosqlite.connect(DB_PATH, timeout=10) as conn:
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA synchronous = NORMAL")
            
            await conn.execute('''CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0
            )''')
            
            await conn.execute('''CREATE TABLE IF NOT EXISTS cards (
                card_id TEXT PRIMARY KEY,
                user_id INTEGER,
                numbers TEXT,
                marked_numbers TEXT DEFAULT '',
                positions TEXT DEFAULT '',
                marked_time REAL DEFAULT 0,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )''')
            
            await conn.execute('''CREATE TABLE IF NOT EXISTS games (
                game_id TEXT PRIMARY KEY,
                status TEXT,
                players TEXT,
                current_number INTEGER,
                last_message_id INTEGER,
                drawn_numbers TEXT DEFAULT '',
                start_time REAL,
                waiting_players TEXT DEFAULT '',
                invite_code TEXT DEFAULT '',
                is_private INTEGER DEFAULT 0
            )''')
            
            await conn.execute('''CREATE TABLE IF NOT EXISTS ads (
                ad_id TEXT PRIMARY KEY,
                file_id TEXT,
                description TEXT,
                created_at REAL
            )''')
            
            # Add missing columns if needed
            async with conn.execute("PRAGMA table_info(cards)") as cursor:
                columns = [col[1] for col in await cursor.fetchall()]
                if 'marked_numbers' not in columns:
                    await conn.execute("ALTER TABLE cards ADD COLUMN marked_numbers TEXT DEFAULT ''")
                if 'positions' not in columns:
                    await conn.execute("ALTER TABLE cards ADD COLUMN positions TEXT DEFAULT ''")
                if 'marked_time' not in columns:
                    await conn.execute("ALTER TABLE cards ADD COLUMN marked_time REAL DEFAULT 0")
            
            async with conn.execute("PRAGMA table_info(games)") as cursor:
                columns = [col[1] for col in await cursor.fetchall()]
                if 'start_time' not in columns:
                    await conn.execute("ALTER TABLE games ADD COLUMN start_time REAL")
                if 'waiting_players' not in columns:
                    await conn.execute("ALTER TABLE games ADD COLUMN waiting_players TEXT DEFAULT ''")
                if 'invite_code' not in columns:
                    await conn.execute("ALTER TABLE games ADD COLUMN invite_code TEXT DEFAULT ''")
                if 'is_private' not in columns:
                    await conn.execute("ALTER TABLE games ADD COLUMN is_private INTEGER DEFAULT 0")
            
            await conn.commit()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
        raise

async def verify_table(table_name):
    try:
        async with aiosqlite.connect(DB_PATH, timeout=10) as conn:
            async with conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,)) as cursor:
                result = await cursor.fetchone()
        return bool(result)
    except Exception as e:
        logger.error(f"Error verifying table {table_name}: {e}")
        return False

async def add_ad(file_id, description):
    async with aiosqlite.connect(DB_PATH) as conn:
        ad_id = str(uuid.uuid4())
        created_at = time.time()
        await conn.execute("INSERT INTO ads (ad_id, file_id, description, created_at) VALUES (?, ?, ?, ?)",
                 (ad_id, file_id, description, created_at))
        await conn.commit()
    logger.info(f"Added ad {ad_id} with file_id {file_id}")
    return ad_id

async def delete_ad(ad_id):
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute("DELETE FROM ads WHERE ad_id = ?", (ad_id,))
        affected = cursor.rowcount
        await conn.commit()
    logger.info(f"Deleted ad {ad_id}")
    return affected > 0

async def get_active_ad():
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT ad_id, file_id, description FROM ads ORDER BY created_at DESC LIMIT 1") as cursor:
            ad = await cursor.fetchone()
    return ad

async def create_user(user_id, username):
    try:
        if not await verify_table('users'):
            logger.warning("Users table missing, attempting to reinitialize database")
            await init_db()
        
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
            await conn.commit()
    except Exception as e:
        logger.error(f"Unexpected error in create_user for user {user_id}: {e}")
        raise

async def get_user_cards(user_id):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT card_id, numbers, marked_numbers, positions, marked_time FROM cards WHERE user_id = ?", (user_id,)) as cursor:
            cards = await cursor.fetchall()
    return cards

async def delete_user_cards(user_id):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM cards WHERE user_id = ?", (user_id,))
        await conn.commit()

async def delete_all_cards():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("DELETE FROM cards")
        await conn.commit()

async def generate_card(user_id):
    async with aiosqlite.connect(DB_PATH) as conn:
        card_id = str(uuid.uuid4())
        
        ranges = [
            (1, 9), (10, 19), (20, 29), (30, 39),
            (40, 49), (50, 59), (60, 69), (70, 80)
        ]
        
        numbers_per_column = [0] * 8
        total_numbers = 0
        
        while total_numbers < 15:
            for col_idx in range(8):
                if total_numbers >= 15:
                    break
                if numbers_per_column[col_idx] >= 3:
                    continue
                if random.random() < 0.5:
                    numbers_per_column[col_idx] += 1
                    total_numbers += 1
        
        while total_numbers < 15:
            available_columns = [i for i, count in enumerate(numbers_per_column) if count < 3]
            if not available_columns:
                break
            col_idx = random.choice(available_columns)
            numbers_per_column[col_idx] += 1
            total_numbers += 1
        
        numbers = []
        for col_idx, (start, end) in enumerate(ranges):
            col_numbers = random.sample(range(start, end + 1), numbers_per_column[col_idx])
            numbers.extend(col_numbers)
        
        numbers.sort()
        
        columns = [[] for _ in range(8)]
        for num in numbers:
            num_int = int(num)
            col = min((num_int - 1) // 10, 7) if num_int < 70 else 7
            columns[col].append(str(num))
        
        positions = []
        for col_idx, col_nums in enumerate(columns):
            if not col_nums:
                continue
            available_rows = list(range(3))
            random.shuffle(available_rows)
            for i, num in enumerate(col_nums):
                if i >= len(available_rows):
                    continue
                row = available_rows[i]
                positions.append(f"{num}:{row}")
        
        numbers_str = ','.join(map(str, numbers))
        positions_str = ','.join(positions)
        
        if len(numbers) != 15:
            return None
        
        await conn.execute("INSERT INTO cards (card_id, user_id, numbers, positions) VALUES (?, ?, ?, ?)",
                 (card_id, user_id, numbers_str, positions_str))
        await conn.commit()
    return card_id

async def create_game(invite_code, is_private=False):
    async with aiosqlite.connect(DB_PATH) as conn:
        game_id = str(uuid.uuid4())
        await conn.execute("INSERT INTO games (game_id, status, players, drawn_numbers, start_time, waiting_players, invite_code, is_private) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (game_id, 'waiting', '', '', None, '', invite_code, 1 if is_private else 0))
        await conn.commit()
    return game_id

async def update_game_status(game_id, status, players=None, current_number=None, last_message_id=None, drawn_numbers=None, start_time=None, waiting_players=None):
    async with aiosqlite.connect(DB_PATH) as conn:
        # Check current status first to prevent overwriting 'finished'
        async with conn.execute("SELECT status FROM games WHERE game_id = ?", (game_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0] == 'finished' and status != 'finished':
                return

        if players is not None:
            if waiting_players is not None:
                await conn.execute("UPDATE games SET status = ?, players = ?, start_time = ?, waiting_players = ? WHERE game_id = ?",
                         (status, players, start_time, waiting_players, game_id))
            else:
                await conn.execute("UPDATE games SET status = ?, players = ?, start_time = ? WHERE game_id = ?",
                         (status, players, start_time, game_id))
        elif current_number is not None:
            await conn.execute("UPDATE games SET status = ?, current_number = ?, last_message_id = ?, drawn_numbers = ? WHERE game_id = ?",
                     (status, current_number, last_message_id, drawn_numbers, game_id))
        else:
            if waiting_players is not None:
                await conn.execute("UPDATE games SET status = ?, waiting_players = ? WHERE game_id = ?",
                         (status, waiting_players, game_id))
            else:
                await conn.execute("UPDATE games SET status = ? WHERE game_id = ?", (status, game_id))
        await conn.commit()

async def get_current_public_game():
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT game_id, status, players, drawn_numbers, start_time, waiting_players, invite_code, is_private FROM games WHERE status != 'finished' AND is_private = 0 ORDER BY ROWID DESC LIMIT 1") as cursor:
            game = await cursor.fetchone()
    return game

async def get_game_by_invite_code(invite_code):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT game_id, status, players, drawn_numbers, start_time, waiting_players, invite_code, is_private FROM games WHERE invite_code = ? AND status != 'finished'", (invite_code,)) as cursor:
            game = await cursor.fetchone()
    return game

async def mark_number(card_id, number):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT marked_numbers, numbers FROM cards WHERE card_id = ?", (card_id,)) as cursor:
            result = await cursor.fetchone()
        
        if not result:
            return False
        
        marked_numbers, numbers = result
        numbers_list = numbers.split(',') if numbers else []
        number_str = str(number).strip()
        
        if number_str in numbers_list:
            marked = marked_numbers.split(',') if marked_numbers else []
            if number_str not in marked:
                marked.append(number_str)
                marked_str = ','.join(marked)
                current_time = time.time()
                await conn.execute("UPDATE cards SET marked_numbers = ?, marked_time = ? WHERE card_id = ?",
                         (marked_str, current_time, card_id))
                await conn.commit()
                return True
            else:
                return False
        else:
            return False

async def check_all_winners(context: ContextTypes.DEFAULT_TYPE, game_id):
    current_game = await get_game_by_id(game_id)
    if not current_game:
        return None, None
    
    player_ids = current_game[2].split(',')
    potential_winners = []
    
    # Optimize: Fetch all cards for all players in one query
    async with aiosqlite.connect(DB_PATH) as conn:
        placeholders = ','.join('?' * len(player_ids))
        query = f"SELECT user_id, card_id, numbers, marked_numbers, marked_time FROM cards WHERE user_id IN ({placeholders})"
        async with conn.execute(query, player_ids) as cursor:
            all_cards = await cursor.fetchall()
            
    for user_id, card_id, numbers, marked_numbers, marked_time in all_cards:
        if not marked_numbers or not numbers:
            continue
        marked = marked_numbers.split(',') if marked_numbers else []
        card_numbers = numbers.split(',')
        
        all_marked = all(num in marked for num in card_numbers)
        if all_marked:
            potential_winners.append((int(user_id), card_id, marked_time))
    
    if not potential_winners:
        return None, None
    
    potential_winners.sort(key=lambda x: x[2])
    winner_id, winner_card_id, _ = potential_winners[0]
    return winner_id, winner_card_id

async def get_game_by_id(game_id):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT game_id, status, players, drawn_numbers, start_time, waiting_players, invite_code, is_private FROM games WHERE game_id = ? AND status != 'finished'", (game_id,)) as cursor:
            game = await cursor.fetchone()
    return game

async def get_game_by_id_for_user(user_id):
    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT game_id, status, players, drawn_numbers, start_time, waiting_players, invite_code, is_private FROM games WHERE status != 'finished' AND (players LIKE ? OR waiting_players LIKE ?) LIMIT 1",
                 (f'%{user_id}%', f'%{user_id}%')) as cursor:
            game = await cursor.fetchone()
    return game

def get_main_menu():
    keyboard = [
        ["🎮 Խաղալ", "🎉 Խաղալ ընկերների հետ"],
        ["📜 Կանոններ", "❓ Օգնություն"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_waiting_menu():
    keyboard = [["⏳ Սպասել"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_game_menu():
    keyboard = [[InlineKeyboardButton("🏃 Դուրս գալ", callback_data='exit')]]
    return InlineKeyboardMarkup(keyboard)

def get_start_game_button(game_id):
    keyboard = [[InlineKeyboardButton("🚀 Սկսել խաղը", callback_data=f'start_game_{game_id[-8:]}')]]
    return InlineKeyboardMarkup(keyboard)

def build_card_grid(card_id, numbers, marked_numbers, positions):
    numbers_list = numbers.split(',')
    if len(numbers_list) != 15:
        return None

    marked = marked_numbers.split(',') if marked_numbers else []
    
    columns = [[] for _ in range(8)]
    for num in numbers_list:
        num_int = int(num)
        col = min((num_int - 1) // 10, 7) if num_int < 70 else 7
        columns[col].append(num)
    
    grid = [[None for _ in range(8)] for _ in range(3)]
    
    position_dict = {}
    if positions:
        for pos in positions.split(','):
            if pos:
                num, row = pos.split(':')
                position_dict[num] = int(row)
    
    for col_idx, col_nums in enumerate(columns):
        if not col_nums:
            continue
        for num in col_nums:
            if num in position_dict:
                row = position_dict[num]
                grid[row][col_idx] = num
    
    return grid, marked

def get_card_keyboard(card_id, numbers, marked_numbers, game_id, positions):
    grid, marked = build_card_grid(card_id, numbers, marked_numbers, positions)
    if grid is None:
        return None
    
    keyboard = []
    short_game_id = game_id[-8:]
    short_card_id = card_id[-8:]
    for row in range(3):
        row_buttons = []
        for col in range(8):
            num = grid[row][col]
            if num is None:
                row_buttons.append(InlineKeyboardButton(" ", callback_data='noop'))
            else:
                text = f"✅" if num in marked else str(num)
                callback_data = f'mark_{short_game_id}_{short_card_id}_{num}'
                row_buttons.append(InlineKeyboardButton(text, callback_data=callback_data))
        keyboard.append(row_buttons)
    keyboard.append([InlineKeyboardButton("🏃 Դուրս գալ", callback_data='exit')])
    
    return InlineKeyboardMarkup(keyboard)

def track_message(context: ContextTypes.DEFAULT_TYPE, user_id: int, message_id: int):
    try:
        # Safely get or create user_data
        if context.user_data is not None and getattr(context, '_user_id', None) == user_id:
            user_data = context.user_data
        elif hasattr(context, 'application') and hasattr(context.application, 'user_data'):
            user_data = context.application.user_data.setdefault(user_id, {})
        else:
            # Fallback if neither is available
            return
            
        user_data.setdefault('cleanup_msgs', []).append(message_id)
    except Exception as e:
        logger.error(f"Error in track_message for user {user_id}: {e}")

async def clear_tracked_messages(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    try:
        if context.user_data is not None and getattr(context, '_user_id', None) == user_id:
            user_data = context.user_data
        elif hasattr(context, 'application') and hasattr(context.application, 'user_data'):
            user_data = context.application.user_data.get(user_id, {})
        else:
            user_data = {}
            
        msgs = user_data.get('cleanup_msgs', [])
        for msg_id in msgs:
            try:
                await context.bot.delete_message(chat_id=user_id, message_id=msg_id)
            except Exception:
                pass
        user_data['cleanup_msgs'] = []
    except Exception as e:
        logger.error(f"Error in clear_tracked_messages for user {user_id}: {e}")

# Helper for concurrent message sending
async def broadcast_message(context, user_ids, text, reply_markup=None, parse_mode=None, track=False):
    async def send(uid):
        try:
            msg = await context.bot.send_message(uid, text, reply_markup=reply_markup, parse_mode=parse_mode)
            if track:
                track_message(context, int(uid), msg.message_id)
        except Exception as e:
            logger.warning(f"Failed to send message to {uid}: {e}")
    await asyncio.gather(*(send(uid) for uid in user_ids))

async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules = (
        "🎲 *Հայկական Լոտո Խաղի Կանոններ* 🎉\n\n"
        "1. **Միացեք խաղին**՝ սեղմելով «Խաղալ» (պատահական խաղացողներով) կամ «Խաղալ ընկերների հետ»։\n"
        "2. **Քարտ**։ Քանի որ սա ԴԵՄՈ խաղ է յուրաքանչյուր խաղացող ավտոմատ ստանում է մեկ քարտ՝ 15 թվով։\n"
        "3. **Խաղի մեկնարկ**։ Խաղը սկսվում է 2 կամ ավելի խաղացողներով։ Ընկերական խաղում ընկերների ժամանումից հետո պետք է սեղմել «Սկսել խաղը»։\n"
        "4. **Թվեր**։ Բոտը պատահականորեն հանում է թվեր (1-80)։\n"
        "5. **Նշեք թվերը**։ Երբ տեսնեք Ձեր թիվը, անմիջապես սեղմեք նրա վրա։\n"
        "6. **Հաղթող**։ Առաջինը, ով նշում է իր քարտի բոլոր 15 թվերը, հաղթում է։\n"
        "7. **Մրցանակ**։ Շահույթը կախված է խաղացողների քանակից, բայց քանի որ սա ԴԵՄՈ տարբերակն է, դրամական շահում չի սպասվում։\n"
        "8. **Խաղի ավարտ**։ Հաղթողի ի հայտ գալուց հետո բոլոր քարտերը ջնջվում են։\n"
        "9. **Ընկերների հետ խաղ**։ Ստեղծեք խաղ, կիսվեք հղումով և սկսեք վայելել խաղը հարազատ միջավայրում։"
    )
    msg = await update.message.reply_text(rules, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu())
    track_message(context, update.effective_user.id, msg.message_id)

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "❓ *Օգնություն* ❓\n\n"
        "Հայկական Լոտո բոտը զվարճալի խաղ է, որտեղ կարող եք խաղալ ընկերների կամ պատահական խաղացողների հետ։\n\n"
        "🔹 **Ինչպե՞ս սկսել**։\n"
        "- Սեղմեք «🎮 Խաղալ»՝ պատահական խաղացողների հետ խաղալու համար։\n"
        "- Սեղմեք «🎉 Խաղալ ընկերների հետ»՝ մասնավոր խաղյալու համար։\n"
        "- Օգտագործեք ընկերոջ հղումը՝ նրա խաղին միանալու համար։\n\n"
        "🔹 **Ինչպե՞ս խաղալ ընկերների հետ**։\n"
        "- Ստեղծեք խաղ՝ սեղմելով «Խաղալ ընկերների հետ»։ Կստանաք հղում։\n"
        "- Կիսվեք հղումով ընկերների հետ։ Նրանք ավտոմատ կմիանան խաղին։\n"
        "- Որպես ստեղծող՝ սեղմեք «🚀 Սկսել խաղը» և խաղը 10 վայրկյանից կսկսվի։\n\n"
        "🔹 **Խնդիրներ կա՞ն**։\n"
        "- Եթե քարտը չի ցուցադրվում, լքեք խաղը և նորից միացեք։\n\n"
        "🔹 **Այլ խնդիրների, առաջարկների կամ գովազդի համար ⬇️**։\n"
        "- Կապվեք մեզ հետ՝ @LottogramSupport։\n\n"
    )
    msg = await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu())
    track_message(context, update.effective_user.id, msg.message_id)

async def add_ad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Այս հրամանը միայն ադմինի համար է։")
        return
    
    description = ' '.join(context.args) if context.args else ""
    context.user_data['awaiting_ad_photo'] = description
    await update.message.reply_text(
        f"📸 Խնդրում եմ ուղարկել նկար գովազդի համար։\n"
        f"📜 Նկարագրություն՝ {description}",
        reply_markup=ReplyKeyboardRemove()
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID or 'awaiting_ad_photo' not in context.user_data:
        return
    
    if not update.message.photo:
        await update.message.reply_text("❌ Խնդրում եմ ուղարկել նկար։")
        return
    
    file_id = update.message.photo[-1].file_id
    description = context.user_data.pop('awaiting_ad_photo')
    ad_id = await add_ad(file_id, description)
    
    await update.message.reply_text(
        f"✅ Գովազդը ավելացվեց (ID: {ad_id[-8:]})\n"
        f"📜 Նկարագրություն՝ {description}",
        reply_markup=get_main_menu()
    )

async def delete_ad_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ Այս հրամանը միայն ադմինի համար է։")
        return
    
    if not context.args:
        await update.message.reply_text("❌ Խնդրում եմ նշել գովազդի ID-ն։\n"
                                       "Օրինակ՝ /delete_ad 12345678")
        return
    
    ad_id = context.args[0]
    if len(ad_id) == 8:
        async with aiosqlite.connect(DB_PATH) as conn:
            async with conn.execute("SELECT ad_id FROM ads WHERE ad_id LIKE ?", (f'%{ad_id}',)) as cursor:
                result = await cursor.fetchone()
        if result:
            ad_id = result[0]
        else:
            await update.message.reply_text("❌ Գովազդը չի գտնվել։")
            return
    
    if await delete_ad(ad_id):
        await update.message.reply_text(f"✅ Գովազդը (ID: {ad_id[-8:]}) ջնջվեց։")
    else:
        await update.message.reply_text("❌ Գովազդը չի գտնվել։")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    try:
        await create_user(user_id, user.username or user.first_name)
        await delete_user_cards(user_id)
        
        if context.args and context.args[0].startswith("game_"):
            invite_code = context.args[0][5:]
            game = await get_game_by_invite_code(invite_code)
            
            if not game:
                await update.message.reply_text(
                    "❌ Այս հղումը սխալ է կամ խաղն արդեն ավարտվել է։\n"
                    "🎮 Ստեղծեք նոր խաղ կամ միացեք այլ խաղի։",
                    reply_markup=get_main_menu()
                )
                return
            
            game_id, status, players, _, start_time, waiting_players, _, is_private = game
            player_ids = players.split(',') if players else []
            waiting_ids = waiting_players.split(',') if waiting_players else []
            
            if str(user_id) in player_ids:
                msg = await update.message.reply_text(
                    f"🎮 Դուք արդեն խաղի մեջ եք (ID: {game_id[-8:]})\n"
                    "⏳ Սպասեք խաղի մեկնարկին։",
                    reply_markup=get_main_menu()
                )
                track_message(context, user_id, msg.message_id)
                try:
                    await show_cards(context, user_id, game_id)
                except Exception as e:
                    logger.error(f"Error in show_cards for user {user_id}: {e}")
                    await context.bot.send_message(user_id, "❌ Քարտը ցուցադրելու սխալ։")
                return
            
            if status == 'running':
                if str(user_id) not in waiting_ids:
                    waiting_ids.append(str(user_id))
                    await update_game_status(game_id, status, waiting_players=','.join(waiting_ids))
                msg = await update.message.reply_text(
                    "🎮 Խաղն արդեն սկսվել է։\n"
                    "⏳ Սեղմեք «Սպասել»՝ որպեսզի տեղեկացվեք հաջորդ խաղի մասին",
                    reply_markup=get_waiting_menu()
                )
                track_message(context, user_id, msg.message_id)
                return
            
            await generate_card(user_id)
            player_ids.append(str(user_id))
            players = ','.join(player_ids)
            await update_game_status(game_id, status, players, start_time=start_time)
            
            other_players = [pid for pid in player_ids if pid and int(pid) != user_id]
            await broadcast_message(context, other_players, f"🔔 Նոր խաղացող միացավ խաղին։ Ընդհանուր՝ {len(player_ids)} խաղացող։", reply_markup=get_main_menu(), track=True)
            
            msg = await update.message.reply_text(
                f"🎉 Դուք միացաք խաղին (ID: {game_id[-8:]})\n"
                f"📜 Ձեզ տրվեց մեկ քարտ։\n"
                f"⏳ Սպասեք, մինչև խաղը սկսվի։",
                reply_markup=get_main_menu()
            )
            track_message(context, user_id, msg.message_id)
            try:
                await show_cards(context, user_id, game_id)
            except Exception as e:
                logger.error(f"Error in show_cards for user {user_id}: {e}")
                await context.bot.send_message(user_id, "❌ Քարտը ցուցադրելու սխալ։")
        else:
            welcome_message = (
                f"👋 Բարև, {user.first_name}։ Ես Հայկական Լոտո բոտն եմ (թերևս ԴԵՄՈ տարբերակը)։ 🎲\n"
                "🎮 Ծանոթացիր խաղի կանոններին, խաղա ընկերների հետ կամ միացիր պատահական խաղացողներին։\n"
                "🔽 Ընտրիր գործողություն՝ մենյուից"
            )
            msg = await update.message.reply_text(welcome_message, reply_markup=get_main_menu())
            track_message(context, user_id, msg.message_id)
    except Exception as e:
        logger.error(f"Error in start command for user {user_id}: {e}")
        await update.message.reply_text(
            "❌ Սխալ։ Կապվեք աջակցության հետ՝ @LottogramSupport։",
            reply_markup=get_main_menu()
        )

async def show_cards(context: ContextTypes.DEFAULT_TYPE, user_id, game_id):
    cards = await get_user_cards(user_id)
    if not cards:
        await context.bot.send_message(
            user_id,
            "❌ Դուք քարտ չունեք։ Կապվեք աջակցության հետ՝ @LottogramSupport:",
            reply_markup=get_main_menu()
        )
        return
    ad = await get_active_ad()
    for card_id, numbers, marked_numbers, positions, _ in cards:
        num_count = len(numbers.split(',')) if numbers else 0
        if num_count != 15:
            await context.bot.send_message(
                user_id,
                f"❌ Քարտը (ID: {card_id[-8:]}) սխալ է։ Կապվեք աջակցության հետ՝ @LottogramSupport:",
                reply_markup=get_main_menu()
            )
            continue
        try:
            keyboard = get_card_keyboard(card_id, numbers, marked_numbers, game_id, positions)
            if keyboard is None:
                await context.bot.send_message(
                    user_id,
                    f"❌ Քարտը (ID: {card_id[-8:]}) չի ցուցադրվում։ Կապվեք աջակցության հետ՝ @LottogramSupport։",
                    reply_markup=get_main_menu()
                )
                continue
            if ad:
                ad_id, file_id, description = ad
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=file_id,
                    caption=f"{description}"
                )
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📜 Ձեր քարտը (ID: {card_id[-8:]}):",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Failed to send card {card_id}: {e}")
            await context.bot.send_message(
                user_id,
                "❌ Քարտը ցուցադրելու սխալ։ Կապվեք աջակցության հետ՝ @LottogramSupport։",
                reply_markup=get_main_menu()
            )

async def handle_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    current_game = await get_game_by_id_for_user(user_id)
    game_running = False
    is_creator = False
    game_id = None
    if current_game:
        game_id, status, players, _, start_time, waiting_players, _, is_private = current_game
        current_time = time.time()
        game_actually_started = start_time is not None and current_time >= start_time
        player_ids = players.split(',') if players else []
        waiting_ids = waiting_players.split(',') if waiting_players else []
        is_creator = player_ids and player_ids[0] == str(user_id)

        if game_actually_started and status == 'running':
            game_running = True
            if text in ["🎮 Խաղալ", "🎉 Խաղալ ընկերների հետ"] and str(user_id) not in player_ids:
                if str(user_id) not in waiting_ids:
                    waiting_ids.append(str(user_id))
                    await update_game_status(game_id, status, waiting_players=','.join(waiting_ids))
                await update.message.reply_text(
                    "🎮 Խաղն ընթացքի մեջ է։\n"
                    "⏳ Սեղմեք «Սպասել»՝ որպեսզի տեղեկացվեք հաջորդ խաղի մասին։",
                    reply_markup=get_waiting_menu()
                )
                return

    if text == "📜 Կանոններ":
        await show_rules(update, context)
    elif text == "❓ Օգնություն":
        await show_help(update, context)
    elif text == "🎮 Խաղալ":
        if game_running:
            await update.message.reply_text(
                "🎮 Խաղն արդեն ընթացքի մեջ է։\n"
                "⏳ Սեղմեք «Սպասել», որպեսզի Ձեզ տեղեկացվեք հաջորդ խաղի մասին։",
                reply_markup=get_waiting_menu()
            )
        else:
            await handle_play(update, context)
    elif text == "🎉 Խաղալ ընկերների հետ":
        if game_running:
            await update.message.reply_text(
                "🎮 Դուք արդեն խաղի մեջ եք։\n"
                "⏳ Սպասեք խաղի ավարտին կամ լքեք խաղը։",
                reply_markup=get_waiting_menu()
            )
        elif current_game and current_game[7] == 1:  # is_private
            await update.message.reply_text(
                "🎮 Դուք արդեն մասնավոր խաղի մեջ եք։\n"
                "⏳ Սպասեք խաղի մեկնարկին կամ լքեք խաղը։",
                reply_markup=get_main_menu()
            )
        else:
            await handle_friends_game(update, context)
    elif text == "⏳ Սպասել":
        if current_game:
            game_id, status, players, _, _, waiting_players, _, _ = current_game
            waiting_ids = waiting_players.split(',') if waiting_players else []
            if str(user_id) not in waiting_ids:
                waiting_ids.append(str(user_id))
                await update_game_status(game_id, status, waiting_players=','.join(waiting_ids))
            await update.message.reply_text(
                "⏳ Դուք սպասման ցուցակում եք։ Կտեղեկացնենք, երբ խաղն ավարտվի։",
                reply_markup=ReplyKeyboardRemove()
            )
    else:
        await update.message.reply_text(
            "❌ Խնդրում եմ օգտագործել մենյուի կոճակները։",
            reply_markup=get_main_menu()
        )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == 'exit':
        await delete_user_cards(user_id)
        current_game = await get_game_by_id_for_user(user_id)
        if current_game:
            game_id, status, players, _, _, waiting_players, _, _ = current_game
            player_ids = players.split(',') if players else []
            waiting_ids = waiting_players.split(',') if waiting_players else []
            if str(user_id) in player_ids:
                player_ids.remove(str(user_id))
                await update_game_status(game_id, status, ','.join(player_ids), waiting_players=','.join(waiting_ids))
                if len(player_ids) < MIN_PLAYERS and status == 'running':
                    await update_game_status(game_id, 'finished')
                    await broadcast_message(context, player_ids, "🏁 Խաղն ավարտվեց, քանի որ բոլորն լքեցին այն։\n🎮 Ստեղծեք նոր խաղ կամ միացեք այլ խաղի։", reply_markup=get_main_menu())
                    valid_waiting_ids = [pid for pid in waiting_ids if pid]
                    await broadcast_message(context, valid_waiting_ids, "🏁 Խաղն ավարտվեց, քանի որ բոլորն լքեցին այն։\n🎮 Ստեղծեք նոր խաղ կամ միացեք այլ խաղի։", reply_markup=get_main_menu())
            elif str(user_id) in waiting_ids:
                waiting_ids.remove(str(user_id))
                await update_game_status(game_id, status, waiting_players=','.join(waiting_ids))
        await query.message.edit_text(
            "👋 Դուք լքեցիք խաղը։ Ձեր քարտը ջնջվեց։",
            reply_markup=None
        )
        await context.bot.send_message(
            user_id,
            "🔽 Ընտրիր գործողություն՝ մենյուից",
            reply_markup=get_main_menu()
        )
    elif query.data == 'noop':
        await query.answer("Այս վանդակը դատարկ է։")
    elif query.data.startswith('start_game_'):
        short_game_id = query.data.split('_')[-1]
        current_game = await get_game_by_id_for_user(user_id)
        if not current_game:
            await query.answer("❌ Խաղը գոյություն չունի։")
            return
        game_id, status, players, _, _, _, _, is_private = current_game
        if short_game_id != game_id[-8:]:
            await query.answer("❌ Անվավեր խաղի ID։")
            return
        player_ids = players.split(',') if players else []
        if not is_private or player_ids[0] != str(user_id):
            await query.answer("❌ Միայն խաղի ստեղծողը կարող է սկսել խաղը։")
            return
        if status != 'waiting':
            await query.answer("❌ Խաղն արդեն սկսված է կամ ավարտված է։")
            return
        if len(player_ids) < MIN_PLAYERS:
            await query.answer(f"❌ Անհրաժեշտ է առնվազն {MIN_PLAYERS} խաղացող։")
            return
        start_time = time.time() + GAME_PAUSE
        await update_game_status(game_id, 'preparing', players=','.join(player_ids), start_time=start_time)
        
        await broadcast_message(context, player_ids, f"🚀 Խաղը սկսվում է {GAME_PAUSE} վայրկյանից։\n📜 Ստուգեք Ձեր քարտը։", reply_markup=ReplyKeyboardRemove())
        
        context.job_queue.run_once(start_game, GAME_PAUSE, data={'game_id': game_id}, name=f"start_game_{game_id}")
        await query.message.edit_text(
            f"🚀 Խաղը (ID: {game_id[-8:]}) սկսվում է {GAME_PAUSE} վայրկյանից։",
            reply_markup=None
        )
    elif query.data.startswith('mark_'):
        try:
            _, short_game_id, short_card_id, number = query.data.split('_')
            current_game = await get_game_by_id_for_user(user_id)
            if not current_game:
                await query.answer("❌ Խաղը գոյություն չունի։")
                return
            game_id = current_game[0]
            if short_game_id != game_id[-8:]:
                await query.answer("❌ Անվավեր խաղի ID։")
                return
            cards = await get_user_cards(user_id)
            card_id = None
            for cid, _, _, _, _ in cards:
                if cid[-8:] == short_card_id:
                    card_id = cid
                    break
            if not card_id:
                await query.answer("❌ Անվավեր քարտի ID։")
                return
            if current_game[1] == 'running':
                drawn_numbers = current_game[3].split(',') if current_game[3] else []
                if number in drawn_numbers:
                    if await mark_number(card_id, number):
                        cards = await get_user_cards(user_id)
                        for cid, numbers, marked_numbers, positions, _ in cards:
                            if cid == card_id:
                                keyboard = get_card_keyboard(cid, numbers, marked_numbers, game_id, positions)
                                if keyboard is None:
                                    await query.message.edit_text(
                                        "❌ Քարտը ցուցադրելու սխալ։ Կապվեք աջակցության հետ՝ @LottogramSupport։"
                                    )
                                    return
                                await query.message.edit_text(
                                    f"📜 Ձեր քարտը (ID: {card_id[-8:]}):",
                                    reply_markup=keyboard
                                )
                                winner_id, winner_card_id = await check_all_winners(context, game_id)
                                if winner_id and winner_card_id:
                                    await end_game(context, game_id, winner_id, winner_card_id)
                    else:
                        await query.answer("❌ Թիվը չի նշվել։")
                else:
                    await query.answer("❌ Սխալ թիվ կամ դեռ չի հանվել։")
            else:
                await query.answer("❌ Խաղն ակտիվ չէ։")
        except Exception as e:
            logger.error(f"Error processing mark callback: {e}")
            await query.answer("❌ Թիվը նշելու սխալ։")

async def update_countdown(context: ContextTypes.DEFAULT_TYPE):
    game_id = context.job.data['game_id']
    current_game = await get_game_by_id(game_id)
    if not current_game or current_game[1] != 'preparing':
        return

    game_id, status, players, _, start_time, _, _, is_private = current_game
    if is_private:
        return

    player_ids = players.split(',') if players else []
    remaining_time = int(max(0, start_time - time.time()))

    if remaining_time <= 0:
        return

    countdown_message = (
        f"🎮 Խաղը (ID: {game_id[-8:]}) պատրաստ է։\n"
        f"📊 Խաղացողներ՝ {len(player_ids)}\n"
        f"⏳ Մնացել է {remaining_time} վայրկյան մինչև մեկնարկը։"
    )

    if game_id not in context.bot_data:
        context.bot_data[game_id] = {'countdown_message_ids': {}}

    async def update_player_countdown(pid):
        try:
            if pid in context.bot_data[game_id]['countdown_message_ids']:
                await context.bot.edit_message_text(
                    chat_id=pid,
                    message_id=context.bot_data[game_id]['countdown_message_ids'][pid],
                    text=countdown_message,
                    reply_markup=get_main_menu()
                )
            else:
                message = await context.bot.send_message(
                    pid,
                    countdown_message,
                    reply_markup=get_main_menu()
                )
                context.bot_data[game_id]['countdown_message_ids'][pid] = message.message_id
        except Exception as e:
            logger.warning(f"Failed to update countdown for player {pid}: {e}")

    await asyncio.gather(*(update_player_countdown(pid) for pid in player_ids))

async def handle_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    cards = await get_user_cards(user_id)
    
    if cards:
        await delete_user_cards(user_id)
    
    current_game = await get_current_public_game()
    if current_game and current_game[1] == 'running':
        game_id, status, players, _, _, waiting_players, _, _ = current_game
        waiting_ids = waiting_players.split(',') if waiting_players else []
        if str(user_id) not in waiting_ids:
            waiting_ids.append(str(user_id))
            await update_game_status(game_id, status, waiting_players=','.join(waiting_ids))
        await update.message.reply_text(
            "🎮 Խաղն ընթացքի մեջ է։\n"
            "⏳ Սեղմեք «Սպասել»՝ որպեսզի տեղեկացվեք նոր խաղի մասին։",
            reply_markup=get_waiting_menu()
        )
        return
    
    await generate_card(user_id)
    
    if not current_game or current_game[1] == 'finished':
        invite_code = str(uuid.uuid4())[:8]
        game_id = await create_game(invite_code, is_private=False)
        players = str(user_id)
        await update_game_status(game_id, 'waiting', players)
        current_game = (game_id, 'waiting', players, '', None, '', invite_code, 0)
    
    game_id, status, players, drawn_numbers, start_time, waiting_players, invite_code, is_private = current_game
    player_ids = players.split(',') if players else []
    
    if str(user_id) not in player_ids:
        player_ids.append(str(user_id))
        players = ','.join(player_ids)
        await update_game_status(game_id, status, players, start_time=start_time)

    player_count = len(player_ids)
    
    if status == 'waiting' and player_count < MIN_PLAYERS:
        await update.message.reply_text(
            f"⏳ Սպասում ենք խաղացողներին։\n"
            f"📊 Խաղացողներ՝ {player_count}\n"
            f"📜 Ձեզ տրվեց մեկ քարտ։\n"
            f"⏳ Խաղը կսկսվի, երբ բավարար խաղացողներ միանան։",
            reply_markup=get_main_menu()
        )
        other_players = [pid for pid in player_ids if pid and int(pid) != user_id]
        await broadcast_message(context, other_players, f"🔔 Նոր խաղացող ({user.first_name}) միացավ խաղին։\n📊 Ընդհանուր՝ {player_count} խաղացող։\n⏳ Սպասում ենք {MIN_PLAYERS - player_count} խաղացողի։", reply_markup=get_main_menu())
        await show_cards(context, user_id, game_id)
        return
    
    remaining_time = int(max(0, start_time - time.time())) if start_time else PUBLIC_GAME_PAUSE
    countdown_message = (
        f"🎮 Խաղը (ID: {game_id[-8:]}) պատրաստ է։\n"
        f"📊 Խաղացողներ՝ {player_count}\n"
        f"⏳ Մնացել է {remaining_time} վայրկյան մինչև մեկնարկը։"
    )

    if game_id not in context.bot_data:
        context.bot_data[game_id] = {'countdown_message_ids': {}}

    try:
        message = await update.message.reply_text(
            f"🎉 Դուք միացաք խաղին (ID: {game_id[-8:]})\n"
            f"📜 Ձեզ տրվեց մեկ քարտ։\n"
            f"{countdown_message}",
            reply_markup=get_main_menu()
        )
        context.bot_data[game_id]['countdown_message_ids'][str(user_id)] = message.message_id
    except Exception as e:
        logger.error(f"Failed to send countdown message to user {user_id}: {e}")
        return

    async def notify_existing_player(pid):
        try:
            if pid in context.bot_data[game_id]['countdown_message_ids']:
                await context.bot.edit_message_text(
                    chat_id=pid,
                    message_id=context.bot_data[game_id]['countdown_message_ids'][pid],
                    text=countdown_message,
                    reply_markup=get_main_menu()
                )
            else:
                message = await context.bot.send_message(
                    pid,
                    f"🔔 Նոր խաղացող միացավ խաղին։\n{countdown_message}",
                    reply_markup=get_main_menu()
                )
                context.bot_data[game_id]['countdown_message_ids'][pid] = message.message_id
        except Exception as e:
            logger.warning(f"Failed to notify player {pid}: {e}")

    other_players = [pid for pid in player_ids if pid and int(pid) != user_id]
    await asyncio.gather(*(notify_existing_player(pid) for pid in other_players))

    await show_cards(context, user_id, game_id)

    if status == 'waiting' and player_count >= MIN_PLAYERS:
        start_time = time.time() + PUBLIC_GAME_PAUSE
        await update_game_status(game_id, 'preparing', players, start_time=start_time)
        context.job_queue.run_once(start_game, PUBLIC_GAME_PAUSE, data={'game_id': game_id}, name=f"start_game_{game_id}")
        context.job_queue.run_repeating(
            update_countdown,
            interval=5,
            last=PUBLIC_GAME_PAUSE,
            data={'game_id': game_id},
            name=f"countdown_{game_id}"
        )

async def handle_friends_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    cards = await get_user_cards(user_id)
    
    if cards:
        await delete_user_cards(user_id)
    
    await generate_card(user_id)
    
    invite_code = str(uuid.uuid4())[:8]
    game_id = await create_game(invite_code, is_private=True)
    players = str(user_id)
    await update_game_status(game_id, 'waiting', players)
    
    player_ids = [str(user_id)]
    player_count = len(player_ids)
    
    invite_link = f"https://t.me/{context.bot.username}?start=game_{invite_code}"
    
    await update.message.reply_text(
        f"🎉 Դուք ստեղծեցիք մասնավոր խաղ (ID: {game_id[-8:]})\n"
        f"📊 Խաղացողներ՝ {player_count}\n"
        f"🚀 Երբ բոլոր ընկերները միանան, սեղմեք «Սկսել խաղը»։",
        reply_markup=get_start_game_button(game_id)
    )
    await update.message.reply_text(
        f"🔗 Արի լոտո խաղալու։\n{invite_link}"
    )

    await show_cards(context, user_id, game_id)

async def end_game(context: ContextTypes.DEFAULT_TYPE, game_id, winner_id, winner_card_id):
    current_game = await get_game_by_id(game_id)
    if not current_game:
        return
    player_ids = current_game[2].split(',')
    waiting_ids = current_game[5].split(',') if current_game[5] else []
    
    if game_id in context.bot_data:
        del context.bot_data[game_id]

    try:
        winner_user = await context.bot.get_chat(winner_id)
        winner_name = winner_user.first_name
        if winner_user.last_name:
            winner_name += f" {winner_user.last_name}"
    except Exception:
        winner_name = "Հաղթող"

    async with aiosqlite.connect(DB_PATH) as conn:
        async with conn.execute("SELECT numbers, marked_numbers FROM cards WHERE card_id = ?", (winner_card_id,)) as cursor:
            card_data = await cursor.fetchone()
    
    card_text = f"🏆 Հաղթողի քարտ (ID: {winner_card_id[-8:]}):\n" + ', '.join(card_data[0].split(','))
    await update_game_status(game_id, 'finished')
    
    # Delete only cards for players in this game
    async with aiosqlite.connect(DB_PATH) as conn:
        placeholders = ','.join('?' * len(player_ids))
        await conn.execute(f"DELETE FROM cards WHERE user_id IN ({placeholders})", player_ids)
        await conn.commit()
    
    async def notify_player(pid):
        if not pid: return
        try:
            await clear_tracked_messages(context, int(pid))
            if int(pid) == winner_id:
                msg = await context.bot.send_message(
                    pid,
                    f"🎉 Շնորհավորում ենք, {winner_name}։ Դուք հաղթեցիք։\n{card_text}\n"
                    "📜 Բոլոր քարտերը ջնջվեցին։ Սկսե՞լ նոր խաղ։",
                    reply_markup=get_main_menu()
                )
                track_message(context, int(pid), msg.message_id)
            else:
                msg = await context.bot.send_message(
                    pid,
                    f"🥇 Խաղն ավարտվեց։ Հաղթող՝ {winner_name}\n{card_text}\n"
                    "📜 Բոլոր քարտերը ջնջվեցին։ Սկսե՞լ նոր խաղ։",
                    reply_markup=get_main_menu()
                )
                track_message(context, int(pid), msg.message_id)
        except Exception as e:
            logger.warning(f"Failed to notify player {pid}: {e}")

    await asyncio.gather(*(notify_player(pid) for pid in player_ids))
    
    valid_waiting_ids = [pid for pid in waiting_ids if pid]
    await broadcast_message(context, valid_waiting_ids, "🏁 Խաղն ավարտվեց։\n🎮 Սկսեք նոր խաղ կամ միացեք այլ խաղի։", reply_markup=get_main_menu(), track=True)

async def start_game(context: ContextTypes.DEFAULT_TYPE):
    game_id = context.job.data['game_id']
    current_game = await get_game_by_id(game_id)
    if not current_game or current_game[0] != game_id or current_game[1] != 'preparing':
        return

    await update_game_status(game_id, 'running')
    
    if game_id in context.bot_data:
        async def delete_countdown(pid, message_id):
            try:
                await context.bot.delete_message(chat_id=pid, message_id=message_id)
            except Exception:
                pass
        
        await asyncio.gather(*(delete_countdown(pid, msg_id) for pid, msg_id in context.bot_data[game_id].get('countdown_message_ids', {}).items()))
        del context.bot_data[game_id]

    player_ids = current_game[2].split(',')
    is_private = current_game[7] == 1
    draw_interval = PRIVATE_DRAW_INTERVAL if is_private else PUBLIC_DRAW_INTERVAL
    
    # Clear tracked messages before starting
    for pid in player_ids:
        if pid:
            await clear_tracked_messages(context, int(pid))
    
    await broadcast_message(context, player_ids, "🎮 Խաղը սկսվեց։\n\n🍀 Հաջողություն եմ մաղթում Ձեզ։", reply_markup=ReplyKeyboardRemove(), track=True)
    
    await asyncio.gather(*(show_cards(context, int(pid), game_id) for pid in player_ids if pid))
    
    await asyncio.sleep(3)
    
    await broadcast_message(context, player_ids, "🎲 Սկսում եմ հանել թվերը․․․", track=True)
    
    await asyncio.sleep(3)
    
    numbers = list(range(1, MAX_NUMBER + 1))
    random.shuffle(numbers)
    drawn_numbers = []
    last_message_ids = {}
    
    for num in numbers:
        current_game = await get_game_by_id(game_id)
        if not current_game or current_game[1] != 'running':
            break
        player_ids = current_game[2].split(',')
        drawn_numbers.append(str(num))
        
        async def send_number(user_id):
            if user_id in last_message_ids:
                try:
                    await context.bot.delete_message(user_id, last_message_ids[user_id])
                except Exception:
                    pass
            try:
                message = await context.bot.send_message(
                    user_id,
                    f"🎲 ԹԻՎ՝ *{num}*",
                    parse_mode=ParseMode.MARKDOWN
                )
                last_message_ids[user_id] = message.message_id
            except Exception as e:
                logger.warning(f"Failed to send number {num} to user {user_id}: {e}")

        await asyncio.gather(*(send_number(uid) for uid in player_ids))
        
        await update_game_status(game_id, 'running', current_number=num, last_message_id=0, drawn_numbers=','.join(drawn_numbers))
        
        winner_id, winner_card_id = await check_all_winners(context, game_id)
        if winner_id and winner_card_id:
            await end_game(context, game_id, winner_id, winner_card_id)
            break
        
        await asyncio.sleep(draw_interval)
        
    # If all numbers are drawn and no one won, end the game
    current_game = await get_game_by_id(game_id)
    if current_game and current_game[1] == 'running':
        if game_id in context.bot_data:
            del context.bot_data[game_id]
        await update_game_status(game_id, 'finished')
        player_ids = current_game[2].split(',')
        await broadcast_message(context, player_ids, "🏁 Խաղն ավարտվեց։ Բոլոր թվերը հանվել են, բայց ոչ ոք չհաղթեց։", reply_markup=get_main_menu())
        for pid in player_ids:
            if pid:
                await delete_user_cards(int(pid))
        
        waiting_ids = current_game[5].split(',') if current_game[5] else []
        if waiting_ids:
            await broadcast_message(context, waiting_ids, "🔔 Նախորդ խաղն ավարտվեց։ Նոր խաղը շուտով կսկսվի։", reply_markup=get_main_menu())
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.execute("UPDATE games SET waiting_players = '' WHERE game_id = ?", (game_id,))
                await conn.commit()

async def main():
    await init_db()
    
    application = Application.builder().token(BOT_TOKEN).build()
    await application.initialize()
    
    await application.bot.delete_webhook(drop_pending_updates=True)
    await application.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
    
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", show_help))
    application.add_handler(CommandHandler("add_ad", add_ad_command))
    application.add_handler(CommandHandler("delete_ad", delete_ad_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyboard))
    application.add_handler(CallbackQueryHandler(button))
    
    await application.start()
    await application.updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="",
        webhook_url=WEBHOOK_URL
    )
    
    await asyncio.Event().wait()

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()
