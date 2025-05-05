import sqlite3
import random
import time
import uuid
import os
import logging
import asyncio
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
import threading

# Set up logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Game settings
MIN_PLAYERS = 2
GAME_PAUSE = 10  # 10 seconds for private friend games
PUBLIC_GAME_PAUSE = 60  # 60 seconds for public games
MAX_NUMBER = 80
ADMIN_ID = 1878495685  # Replace with your admin user ID

# Configuration
BOT_TOKEN = os.getenv("BOT_TOKEN", "7564418813:AAECv8DC1l_6FUvO9iaLpQMZCe2VeqabcUE")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://fuzzy-journey.onrender.com")
PORT = int(os.getenv("PORT", 10000))
DB_PATH = "/var/data/lotto.db"  # Persistent disk path for Render

# Check token
if not BOT_TOKEN:
    logger.error("BOT_TOKEN environment variable is not set. Please set it.")
    raise ValueError("BOT_TOKEN is required.")

# SQLite lock for thread safety
db_lock = threading.Lock()

# Database initialization
def init_db():
    with db_lock:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA busy_timeout = 10000")  # 10 seconds timeout
        conn.execute("PRAGMA journal_mode = WAL")    # Enable WAL mode for better concurrency
        c = conn.cursor()
        
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 0
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS cards (
            card_id TEXT PRIMARY KEY,
            user_id INTEGER,
            numbers TEXT,
            marked_numbers TEXT DEFAULT '',
            positions TEXT DEFAULT '',
            marked_time REAL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS games (
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
        
        c.execute('''CREATE TABLE IF NOT EXISTS ads (
            ad_id TEXT PRIMARY KEY,
            file_id TEXT,
            description TEXT,
            created_at REAL
        )''')
        
        c.execute("PRAGMA table_info(cards)")
        columns = [col[1] for col in c.fetchall()]
        if 'marked_numbers' not in columns:
            c.execute("ALTER TABLE cards ADD COLUMN marked_numbers TEXT DEFAULT ''")
        if 'positions' not in columns:
            c.execute("ALTER TABLE cards ADD COLUMN positions TEXT DEFAULT ''")
        if 'marked_time' not in columns:
            c.execute("ALTER TABLE cards ADD COLUMN marked_time REAL DEFAULT 0")
        
        c.execute("PRAGMA table_info(games)")
        columns = [col[1] for col in c.fetchall()]
        if 'start_time' not in columns:
            c.execute("ALTER TABLE games ADD COLUMN start_time REAL")
        if 'waiting_players' not in columns:
            c.execute("ALTER TABLE games ADD COLUMN waiting_players TEXT DEFAULT ''")
        if 'invite_code' not in columns:
            c.execute("ALTER TABLE games ADD COLUMN invite_code TEXT DEFAULT ''")
        if 'is_private' not in columns:
            c.execute("ALTER TABLE games ADD COLUMN is_private INTEGER DEFAULT 0")
        
        conn.commit()
        conn.close()
    logger.info("Database initialized successfully")

# Add advertisement
def add_ad(file_id, description):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        ad_id = str(uuid.uuid4())
        created_at = time.time()
        c.execute("INSERT INTO ads (ad_id, file_id, description, created_at) VALUES (?, ?, ?, ?)",
                 (ad_id, file_id, description, created_at))
        conn.commit()
        conn.close()
    logger.info(f"Added ad {ad_id} with file_id {file_id}")
    return ad_id

# Delete advertisement
def delete_ad(ad_id):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM ads WHERE ad_id = ?", (ad_id,))
        affected = c.rowcount
        conn.commit()
        conn.close()
    logger.info(f"Deleted ad {ad_id}")
    return affected > 0

# Get active advertisement
def get_active_ad():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT ad_id, file_id, description FROM ads ORDER BY created_at DESC LIMIT 1")
        ad = c.fetchone()
        conn.close()
    logger.info(f"Retrieved active ad: {ad}")
    return ad

# Create user in database
def create_user(user_id, username):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", (user_id, username))
        conn.commit()
        conn.close()
    logger.info(f"Created/Updated user {user_id}")

# Get user's cards
def get_user_cards(user_id):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT card_id, numbers, marked_numbers, positions, marked_time FROM cards WHERE user_id = ?", (user_id,))
        cards = c.fetchall()
        conn.close()
    for card_id, numbers, marked_numbers, positions, marked_time in cards:
        num_count = len(numbers.split(','))
        if num_count != 15:
            logger.warning(f"Card {card_id} for user {user_id} has {num_count} numbers instead of 15.")
    return cards

# Delete user's cards
def delete_user_cards(user_id):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM cards WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
    logger.info(f"Deleted all cards for user {user_id}")

# Delete all cards after game ends
def delete_all_cards():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("DELETE FROM cards")
        conn.commit()
        conn.close()
    logger.info("Deleted all cards after game end")

# Generate a card for a user
def generate_card(user_id):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
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
            logger.info(f"Card {card_id} column {col_idx + 1} ({start}-{end}): {col_numbers}")
        
        numbers.sort()
        
        columns = [[] for _ in range(8)]
        for num in numbers:
            num_int = int(num)
            if 1 <= num_int <= 9:
                col = 0
            elif 10 <= num_int <= 19:
                col = 1
            elif 20 <= num_int <= 29:
                col = 2
            elif 30 <= num_int <= 39:
                col = 3
            elif 40 <= num_int <= 49:
                col = 4
            elif 50 <= num_int <= 59:
                col = 5
            elif 60 <= num_int <= 69:
                col = 6
            else:
                col = 7
            columns[col].append(str(num))
        
        positions = []
        for col_idx, col_nums in enumerate(columns):
            if not col_nums:
                continue
            available_rows = list(range(3))
            random.shuffle(available_rows)
            for i, num in enumerate(col_nums):
                if i >= len(available_rows):
                    logger.warning(f"Card {card_id}: Too many numbers in column {col_idx + 1}, skipping {num}")
                    continue
                row = available_rows[i]
                positions.append(f"{num}:{row}")
        
        numbers_str = ','.join(map(str, numbers))
        positions_str = ','.join(positions)
        logger.info(f"Generated card {card_id} with numbers: {numbers_str} (count: {len(numbers)})")
        logger.info(f"Positions for card {card_id}: {positions_str}")
        if len(numbers) != 15:
            logger.error(f"Card {card_id} generated with incorrect number count: {len(numbers)}")
            return None
        
        c.execute("INSERT INTO cards (card_id, user_id, numbers, positions) VALUES (?, ?, ?, ?)",
                 (card_id, user_id, numbers_str, positions_str))
        conn.commit()
        conn.close()
    return card_id

# Create a new game
def create_game(invite_code, is_private=False):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        game_id = str(uuid.uuid4())
        c.execute("INSERT INTO games (game_id, status, players, drawn_numbers, start_time, waiting_players, invite_code, is_private) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (game_id, 'waiting', '', '', None, '', invite_code, 1 if is_private else 0))
        conn.commit()
        conn.close()
    logger.info(f"Created new game with ID: {game_id}, Invite code: {invite_code}, Private: {is_private}")
    return game_id

# Update game status
def update_game_status(game_id, status, players=None, current_number=None, last_message_id=None, drawn_numbers=None, start_time=None, waiting_players=None):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        if players is not None:
            if waiting_players is not None:
                c.execute("UPDATE games SET status = ?, players = ?, start_time = ?, waiting_players = ? WHERE game_id = ?",
                         (status, players, start_time, waiting_players, game_id))
            else:
                c.execute("UPDATE games SET status = ?, players = ?, start_time = ? WHERE game_id = ?",
                         (status, players, start_time, game_id))
        elif current_number is not None:
            c.execute("UPDATE games SET status = ?, current_number = ?, last_message_id = ?, drawn_numbers = ? WHERE game_id = ?",
                     (status, current_number, last_message_id, drawn_numbers, game_id))
        else:
            if waiting_players is not None:
                c.execute("UPDATE games SET status = ?, waiting_players = ? WHERE game_id = ?",
                         (status, waiting_players, game_id))
            else:
                c.execute("UPDATE games SET status = ? WHERE game_id = ?", (status, game_id))
        conn.commit()
        conn.close()
    logger.info(f"Updated game {game_id} status to {status}")

# Get current public game
def get_current_public_game():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT game_id, status, players, drawn_numbers, start_time, waiting_players, invite_code, is_private FROM games WHERE status != 'finished' AND is_private = 0 ORDER BY ROWID DESC LIMIT 1")
        game = c.fetchone()
        conn.close()
    logger.info(f"Retrieved current public game: {game}")
    return game

# Get game by invite code
def get_game_by_invite_code(invite_code):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT game_id, status, players, drawn_numbers, start_time, waiting_players, invite_code, is_private FROM games WHERE invite_code = ? AND status != 'finished'", (invite_code,))
        game = c.fetchone()
        conn.close()
    logger.info(f"Retrieved game by invite code {invite_code}: {game}")
    return game

# Mark a number on a card
def mark_number(card_id, number):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT marked_numbers, numbers FROM cards WHERE card_id = ?", (card_id,))
        result = c.fetchone()
        numbers = result[1].split(',')
        number_str = str(number)
        if number_str in numbers:
            marked = result[0].split(',') if result[0] else []
            if number_str not in marked:
                marked.append(number_str)
                marked_str = ','.join(marked)
                current_time = time.time()
                c.execute("UPDATE cards SET marked_numbers = ?, marked_time = ? WHERE card_id = ?",
                         (marked_str, current_time, card_id))
                conn.commit()
                conn.close()
                logger.info(f"Marked number {number} on card {card_id}. Marked numbers: {marked_str}, Time: {current_time}")
                return True
        conn.close()
    logger.warning(f"Number {number} not in card {card_id} numbers: {','.join(numbers)}")
    return False

# Check for winners
async def check_all_winners(context: ContextTypes.DEFAULT_TYPE, game_id):
    current_game = get_game_by_id(game_id)
    if not current_game:
        return None, None
    player_ids = current_game[2].split(',')
    potential_winners = []
    for user_id in player_ids:
        cards = get_user_cards(int(user_id))
        for card_id, numbers, marked_numbers, _, marked_time in cards:
            if not marked_numbers:
                continue
            marked = marked_numbers.split(',')
            card_numbers = numbers.split(',')
            if len(marked) == len(card_numbers) and set(marked) == set(card_numbers):
                potential_winners.append((int(user_id), card_id, marked_time))
    if not potential_winners:
        return None, None
    potential_winners.sort(key=lambda x: x[2])
    winner_id, winner_card_id, _ = potential_winners[0]
    logger.info(f"Winner detected: User {winner_id} with card {winner_card_id}")
    return winner_id, winner_card_id

# Get game by ID
def get_game_by_id(game_id):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT game_id, status, players, drawn_numbers, start_time, waiting_players, invite_code, is_private FROM games WHERE game_id = ? AND status != 'finished'", (game_id,))
        game = c.fetchone()
        conn.close()
    logger.info(f"Retrieved game by ID {game_id}: {game}")
    return game

# Get game for user
def get_game_by_id_for_user(user_id):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT game_id, status, players, drawn_numbers, start_time, waiting_players, invite_code, is_private FROM games WHERE status != 'finished' AND (players LIKE ? OR waiting_players LIKE ?) LIMIT 1",
                 (f'%{user_id}%', f'%{user_id}%'))
        game = c.fetchone()
        conn.close()
    logger.info(f"Retrieved game for user {user_id}: {game}")
    return game

# Main menu
def get_main_menu():
    keyboard = [
        ["🎮 Խաղալ", "🎉 Խաղալ ընկերների հետ"],
        ["📜 Կանոններ", "❓ Օգնություն"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Waiting menu
def get_waiting_menu():
    keyboard = [
        ["⏳ Սպասել"]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# Game menu
def get_game_menu():
    keyboard = [[InlineKeyboardButton("🏃 Դուրս գալ", callback_data='exit')]]
    return InlineKeyboardMarkup(keyboard)

# Start game button for private games
def get_start_game_button(game_id):
    keyboard = [[InlineKeyboardButton("🚀 Սկսել խաղը", callback_data=f'start_game_{game_id[-8:]}')]]
    return InlineKeyboardMarkup(keyboard)

# Build 3x8 card grid
def build_card_grid(card_id, numbers, marked_numbers, positions):
    numbers_list = numbers.split(',')
    if len(numbers_list) != 15:
        logger.error(f"Card {card_id} has {len(numbers_list)} numbers instead of 15")
        return None

    marked = marked_numbers.split(',') if marked_numbers else []
    
    columns = [[] for _ in range(8)]
    for num in numbers_list:
        num_int = int(num)
        if 1 <= num_int <= 9:
            col = 0
        elif 10 <= num_int <= 19:
            col = 1
        elif 20 <= num_int <= 29:
            col = 2
        elif 30 <= num_int <= 39:
            col = 3
        elif 40 <= num_int <= 49:
            col = 4
        elif 50 <= num_int <= 59:
            col = 5
        elif 60 <= num_int <= 69:
            col = 6
        else:
            col = 7
        columns[col].append(num)
    
    ranges = [(1, 9), (10, 19), (20, 29), (30, 39), (40, 49), (50, 59), (60, 69), (70, 80)]
    for col_idx, col_nums in enumerate(columns):
        logger.info(f"Card {card_id} column {col_idx + 1} ({ranges[col_idx][0]}-{ranges[col_idx][1]}): {col_nums}")
    
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
            else:
                logger.warning(f"Card {card_id}: No position found for number {num}, skipping")
    
    logger.info(f"Card {card_id} grid:")
    for row in grid:
        logger.info(f"Row: {row}")
    
    displayed_numbers = sum(1 for row in grid for cell in row if cell is not None)
    logger.info(f"Card {card_id} displayed {displayed_numbers} numbers in grid")
    if displayed_numbers != 15:
        logger.error(f"Card {card_id} grid error: Expected 15 numbers, but displayed {displayed_numbers}")
    
    return grid, marked

# Display card as text
def display_card_as_text(card_id, grid, marked):
    if grid is None:
        return "❌ Քարտը սխալ է։ Խնդրում եմ կապվել աջակցության հետ։"
    
    card_text = f"📜 Քարտ (ID: {card_id[-8:]}):\n"
    card_text += "```\n"
    for row in grid:
        row_text = ""
        for cell in row:
            if cell is None:
                row_text += "   "
            else:
                if cell in marked:
                    row_text += f"✅{cell:2} "
                else:
                    row_text += f"{cell:2} "
        card_text += row_text + "\n"
    card_text += "```"
    return card_text

# Card keyboard
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
                if len(callback_data.encode('utf-8')) > 64:
                    logger.error(f"Callback data too long for number {num}: {callback_data}")
                    continue
                row_buttons.append(InlineKeyboardButton(text, callback_data=callback_data))
        keyboard.append(row_buttons)
    keyboard.append([InlineKeyboardButton("🏃 Դուրս գալ", callback_data='exit')])
    
    logger.info(f"Card {card_id} keyboard created with {len(keyboard)} rows")
    for row_idx, row in enumerate(keyboard[:-1]):
        logger.info(f"Keyboard row {row_idx + 1}: {[btn.text for btn in row]}")
    
    return InlineKeyboardMarkup(keyboard)

# Show game rules
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
    await update.message.reply_text(rules, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu())

# Show help
async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "❓ *Օգնություն* ❓\n\n"
        "Հայկական Լոտո բոտը զվարճալի խաղ է, որտեղ կարող եք խաղալ ընկերների կամ պատահական խաղացողների հետ։\n\n"
        "🔹 **Ինչպե՞ս սկսել**։\n"
        "- Սեղմեք «🎮 Խաղալ»՝ պատահական խաղացողների հետ խաղալու համար։\n"
        "- Սեղմեք «🎉 Խաղալ ընկերների հետ»՝ մասնավոր խաղягը ստեղծեք նոր խաղ։\n"
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
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=get_main_menu())

# Add advertisement command
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
    logger.info(f"User {user_id} initiated add_ad with description: {description}")

# Handle photo for advertisement
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID or 'awaiting_ad_photo' not in context.user_data:
        logger.info(f"Photo received from user {user_id}, but not processed (not admin or not awaiting photo)")
        return
    
    if not update.message.photo:
        await update.message.reply_text("❌ Խնդրում եմ ուղարկել նկար։")
        logger.warning(f"Non-photo message received from user {user_id} while awaiting ad photo")
        return
    
    file_id = update.message.photo[-1].file_id
    description = context.user_data.pop('awaiting_ad_photo')
    ad_id = add_ad(file_id, description)
    
    await update.message.reply_text(
        f"✅ Գովազդը ավելացվեց (ID: {ad_id[-8:]})\n"
        f"📜 Նկարագրություն՝ {description}",
        reply_markup=get_main_menu()
    )
    logger.info(f"Ad {ad_id} added by user {user_id} with file_id {file_id}")

# Delete advertisement command
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
        with db_lock:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT ad_id FROM ads WHERE ad_id LIKE ?", (f'%{ad_id}',))
            result = c.fetchone()
            conn.close()
        if result:
            ad_id = result[0]
        else:
            await update.message.reply_text("❌ Գովազդը չի գտնվել։")
            return
    
    if delete_ad(ad_id):
        await update.message.reply_text(f"✅ Գովազդը (ID: {ad_id[-8:]}) ջնջվեց։")
    else:
        await update.message.reply_text("❌ Գովազդը չի գտնվել։")

# Start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    create_user(user_id, user.username or user.first_name)
    delete_user_cards(user_id)
    
    if context.args and context.args[0].startswith("game_"):
        invite_code = context.args[0][5:]  # Extract invite code from "game_<invite_code>"
        game = get_game_by_invite_code(invite_code)
        
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
            await update.message.reply_text(
                f"🎮 Դուք արդեն խաղի մեջ եք (ID: {game_id[-8:]})\n"
                "⏳ Սպասեք խաղի մեկնարկին։",
                reply_markup=get_main_menu()
            )
            await show_cards(context, user_id, game_id)
            return
        
        if status == 'running':
            if str(user_id) not in waiting_ids:
                waiting_ids.append(str(user_id))
                update_game_status(game_id, status, waiting_players=','.join(waiting_ids))
            await update.message.reply_text(
                "🎮 Խաղն արդեն սկսվել է։\n"
                "⏳ Սեղմեք «Սպասել»՝ որպեսզի տեղեկացվեք հաջորդ խաղի մասին",
                reply_markup=get_waiting_menu()
            )
            return
        
        generate_card(user_id)
        player_ids.append(str(user_id))
        players = ','.join(player_ids)
        update_game_status(game_id, status, players, start_time=start_time)
        
        for pid in player_ids:
            if int(pid) != user_id:
                try:
                    await context.bot.send_message(
                        pid,
                        f"🔔 Նոր խաղացող միացավ խաղին։ Ընդհանուր՝ {len(player_ids)} խաղացող։",
                        reply_markup=get_main_menu()
                    )
                    await asyncio.sleep(0.05)  # Optimized rate limiting
                except Exception as e:
                    logger.warning(f"Failed to notify player {pid}: {e}")
        
        await update.message.reply_text(
            f"🎉 Դուք միացաք խաղին (ID: {game_id[-8:]})\n"
            f"📜 Ձեզ տրվեց մեկ քարտ։\n"
            f"⏳ Սպասեք, մինչև խաղը սկսվի։",
            reply_markup=get_main_menu()
        )
        await show_cards(context, user_id, game_id)
    else:
        welcome_message = (
            f"👋 Բարև, {user.first_name}։ Ես Հայկական Լոտո բոտն եմ (թերևս ԴԵՄՈ տարբերակը)։ 🎲\n"
            "🎮 Ծանոթացիր խաղի կանոններին, խաղա ընկերների հետ կամ միացիր պատահական խաղացողներին։\n"
            "🔽 Ընտրիր գործողություն՝ մենյուից"
        )
        await update.message.reply_text(welcome_message, reply_markup=get_main_menu())

# Show user's cards
async def show_cards(context: ContextTypes.DEFAULT_TYPE, user_id, game_id):
    cards = get_user_cards(user_id)
    if not cards:
        await context.bot.send_message(
            user_id,
            "❌ Դուք քարտ չունեք։ Կապվեք աջակցության հետ՝ @LottogramSupport:",
            reply_markup=get_main_menu()
        )
        return
    ad = get_active_ad()
    for card_id, numbers, marked_numbers, positions, _ in cards:
        num_count = len(numbers.split(','))
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
                await asyncio.sleep(0.05)  # Optimized rate limiting
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📜 Ձեր քարտը (ID: {card_id[-8:]}):",
                reply_markup=keyboard
            )
            await asyncio.sleep(0.05)  # Optimized rate limiting
        except Exception as e:
            logger.error(f"Failed to send card {card_id}: {e}")
            await context.bot.send_message(
                user_id,
                "❌ Քարտը ցուցադրելու սխալ։ Կապվեք աջակցության հետ՝ @LottogramSupport։",
                reply_markup=get_main_menu()
            )

# Handle keyboard inputs
async def handle_keyboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    logger.info(f"Handling keyboard input from user {user_id}: {text}")

    current_game = get_game_by_id_for_user(user_id)
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
                    update_game_status(game_id, status, waiting_players=','.join(waiting_ids))
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
                update_game_status(game_id, status, waiting_players=','.join(waiting_ids))
            await update.message.reply_text(
                "⏳ Դուք սպասման ցուցակում եք։ Կտեղեկացնենք, երբ խաղն ավարտվի։",
                reply_markup=ReplyKeyboardRemove()
            )
    else:
        logger.warning(f"Unknown keyboard input from user {user_id}: {text}")
        await update.message.reply_text(
            "❌ Խնդրում եմ օգտագործել մենյուի կոճակները։",
            reply_markup=get_main_menu()
        )

# Handle inline buttons
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == 'exit':
        delete_user_cards(user_id)
        current_game = get_game_by_id_for_user(user_id)
        if current_game:
            game_id, status, players, _, _, waiting_players, _, _ = current_game
            player_ids = players.split(',') if players else []
            waiting_ids = waiting_players.split(',') if waiting_players else []
            if str(user_id) in player_ids:
                player_ids.remove(str(user_id))
                update_game_status(game_id, status, ','.join(player_ids), waiting_players=','.join(waiting_ids))
                if len(player_ids) < MIN_PLAYERS and status == 'running':
                    update_game_status(game_id, 'finished')
                    for pid in player_ids:
                        try:
                            await context.bot.send_message(
                                pid,
                                "🏁 Խաղն ավարտվեց, քանի որ բոլորն լքեցին այն։\n"
                                "🎮 Ստեղծեք նոր խաղ կամ միացեք այլ խաղի։",
                                reply_markup=get_main_menu()
                            )
                            await asyncio.sleep(0.05)  # Optimized rate limiting
                        except Exception as e:
                            logger.warning(f"Failed to notify player {pid}: {e}")
                    for pid in waiting_ids:
                        if pid:
                            try:
                                await context.bot.send_message(
                                    pid,
                                    "🏁 Խաղն ավարտվեց, քանի որ բոլորն լքեցին այն։\n"
                                    "🎮 Ստեղծեք նոր խաղ կամ միացեք այլ խաղի։",
                                    reply_markup=get_main_menu()
                                )
                                await asyncio.sleep(0.05)  # Optimized rate limiting
                            except Exception as e:
                                logger.warning(f"Failed to notify waiting player {pid}: {e}")
            elif str(user_id) in waiting_ids:
                waiting_ids.remove(str(user_id))
                update_game_status(game_id, status, waiting_players=','.join(waiting_ids))
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
        current_game = get_game_by_id_for_user(user_id)
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
        update_game_status(game_id, 'preparing', players=','.join(player_ids), start_time=start_time)
        for pid in player_ids:
            try:
                await context.bot.send_message(
                    pid,
                    f"🚀 Խաղը սկսվում է {GAME_PAUSE} վայրկյանից։\n"
                    f"📜 Ստուգեք Ձեր քարտը։",
                    reply_markup=ReplyKeyboardRemove()
                )
                await asyncio.sleep(0.05)  # Optimized rate limiting
            except Exception as e:
                logger.warning(f"Failed to notify player {pid}: {e}")
        context.job_queue.run_once(start_game, GAME_PAUSE, data={'game_id': game_id}, name=f"start_game_{game_id}")
        await query.message.edit_text(
            f"🚀 Խաղը (ID: {game_id[-8:]}) սկսվում է {GAME_PAUSE} վայրկյանից։",
            reply_markup=None
        )
        logger.info(f"Scheduled game {game_id} to start in {GAME_PAUSE} seconds")
    elif query.data.startswith('mark_'):
        try:
            _, short_game_id, short_card_id, number = query.data.split('_')
            current_game = get_game_by_id_for_user(user_id)
            if not current_game:
                await query.answer("❌ Խաղը գոյություն չունի։")
                return
            game_id = current_game[0]
            if short_game_id != game_id[-8:]:
                await query.answer("❌ Անվավեր խաղի ID։")
                return
            cards = get_user_cards(user_id)
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
                if number in drawn_numbers and mark_number(card_id, number):
                    cards = get_user_cards(user_id)
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
                else:
                    await query.answer("❌ Սխալ թիվ կամ արդեն նշված է։")
            else:
                await query.answer("❌ Խաղն ակտիվ չէ։")
        except Exception as e:
            logger.error(f"Error processing mark callback: {e}")
            await query.answer("❌ Թիվը նշելու սխալ։")

# Handle public play
async def handle_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    cards = get_user_cards(user_id)
    
    if cards:
        delete_user_cards(user_id)
    
    current_game = get_current_public_game()
    if current_game and current_game[1] == 'running':
        game_id, status, players, _, _, waiting_players, _, _ = current_game
        waiting_ids = waiting_players.split(',') if waiting_players else []
        if str(user_id) not in waiting_ids:
            waiting_ids.append(str(user_id))
            update_game_status(game_id, status, waiting_players=','.join(waiting_ids))
        await update.message.reply_text(
            "🎮 Խաղն ընթացքի մեջ է։\n"
            "⏳ Սեղմեք «Սպասել»՝ որպեսզի տեղեկացվեք նոր խաղի մասին։",
            reply_markup=get_waiting_menu()
        )
        return
    
    generate_card(user_id)
    
    if not current_game or current_game[1] == 'finished':
        invite_code = str(uuid.uuid4())[:8]
        game_id = create_game(invite_code, is_private=False)
        players = str(user_id)
        update_game_status(game_id, 'waiting', players)
        current_game = (game_id, 'waiting', players, '', None, '', invite_code, 0)
    
    game_id, status, players, drawn_numbers, start_time, waiting_players, invite_code, is_private = current_game
    player_ids = players.split(',') if players else []
    
    if str(user_id) not in player_ids:
        player_ids.append(str(user_id))
        players = ','.join(player_ids)
        update_game_status(game_id, status, players, start_time=start_time)

    player_count = len(player_ids)
    
    if status == 'waiting' and player_count < MIN_PLAYERS:
        await update.message.reply_text(
            f"⏳ Սպասում ենք խաղացողներին։\n"
            f"📊 Խաղացողներ՝ {player_count}\n"
            f"📜 Ձեզ տրվեց մեկ քարտ։\n"
            f"⏳ Խաղը կսկսվի, երբ բավարար խաղացողներ միանան։",
            reply_markup=get_main_menu()
        )
        for pid in player_ids:
            if int(pid) != user_id:
                try:
                    await context.bot.send_message(
                        pid,
                        f"🔔 Նոր խաղացող ({user.first_name}) միացավ խաղին։\n"
                        f"📊 Ընդհանուր՝ {player_count} խաղացող։\n"
                        f"⏳ Սպասում ենք {MIN_PLAYERS - player_count} խաղացողի։",
                        reply_markup=get_main_menu()
                    )
                    await asyncio.sleep(0.05)  # Optimized rate limiting
                except Exception as e:
                    logger.warning(f"Failed to notify player {pid}: {e}")
        await show_cards(context, user_id, game_id)
        logger.info(f"Game {game_id} waiting for players: {player_count}/{MIN_PLAYERS}")
        return
    
    if status == 'waiting' and player_count >= MIN_PLAYERS:
        start_time = time.time() + PUBLIC_GAME_PAUSE
        update_game_status(game_id, 'preparing', players, start_time=start_time)
        await update.message.reply_text(
            f"🚀 Խաղը սկսվում է {PUBLIC_GAME_PAUSE} վայրկյանից։\n"
            f"📊 Խաղացողներ՝ {player_count}",
            reply_markup=get_main_menu()
        )
        for pid in player_ids:
            if int(pid) != user_id:
                try:
                    await context.bot.send_message(
                        pid,
                        f"🔔 Նոր խաղացող միացավ խաղին։\n"
                        f"📊 Ընդհանուր՝ {player_count} խաղացող։\n"
                        f"⏳ Խաղը սկսվում է {PUBLIC_GAME_PAUSE} վայրկյանից։",
                        reply_markup=get_main_menu()
                    )
                    await asyncio.sleep(0.05)  # Optimized rate limiting
                except Exception as e:
                    logger.warning(f"Failed to notify player {pid}: {e}")
        context.job_queue.run_once(start_game, PUBLIC_GAME_PAUSE, data={'game_id': game_id}, name=f"start_game_{game_id}")
        logger.info(f"Scheduled public game {game_id} to start in {PUBLIC_GAME_PAUSE} seconds")
    else:  # status == 'preparing'
        remaining_time = int(max(0, start_time - time.time())) if start_time else 0
        await update.message.reply_text(
            f"🎮 Խաղը (ID: {game_id[-8:]}) պատրաստ է։\n"
            f"📊 Խաղացողներ՝ {player_count}\n"
            f"📜 Ձեզ տրվեց մեկ քարտ։\n"
            f"⏳ Մնացել է {remaining_time} վայրկյան մինչև մեկնարկը։",
            reply_markup=get_main_menu()
        )
        for pid in player_ids:
            if int(pid) != user_id:
                try:
                    await context.bot.send_message(
                        pid,
                        f"🔔 Նոր խաղացող միացավ խաղին։\n"
                        f"📊 Ընդհանուր՝ {player_count} խաղացող։\n"
                        f"⏳ Մնացել է {remaining_time} վայրկյան մինչև մեկնարկը։",
                        reply_markup=get_main_menu()
                    )
                    await asyncio.sleep(0.05)  # Optimized rate limiting
                except Exception as e:
                    logger.warning(f"Failed to notify player {pid}: {e}")
        await show_cards(context, user_id, game_id)

# Handle friends game
async def handle_friends_game(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    cards = get_user_cards(user_id)
    
    if cards:
        delete_user_cards(user_id)
    
    generate_card(user_id)
    
    invite_code = str(uuid.uuid4())[:8]
    game_id = create_game(invite_code, is_private=True)
    players = str(user_id)
    update_game_status(game_id, 'waiting', players)
    
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

# End game
async def end_game(context: ContextTypes.DEFAULT_TYPE, game_id, winner_id, winner_card_id):
    current_game = get_game_by_id(game_id)
    if not current_game:
        logger.warning(f"Attempted to end non-existent or finished game {game_id}")
        return
    player_ids = current_game[2].split(',')
    waiting_ids = current_game[5].split(',') if current_game[5] else []
    
    # Fetch winner's name from Telegram
    try:
        winner_user = await context.bot.get_chat(winner_id)
        winner_name = winner_user.first_name
        if winner_user.last_name:
            winner_name += f" {winner_user.last_name}"
    except Exception as e:
        logger.warning(f"Failed to fetch winner name for {winner_id}: {e}")
        winner_name = "Հաղթող"

    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT numbers, marked_numbers FROM cards WHERE card_id = ?", (winner_card_id,))
        card_data = c.fetchone()
        conn.close()
    
    card_text = f"🏆 Հաղթողի քարտ (ID: {winner_card_id[-8:]}):\n" + ', '.join(card_data[0].split(','))
    update_game_status(game_id, 'finished')
    
    delete_all_cards()
    
    for pid in player_ids:
        try:
            if int(pid) == winner_id:
                await context.bot.send_message(
                    pid,
                    f"🎉 Շնորհավորում ենք, {winner_name}։ Դուք հաղթեցիք։\n{card_text}\n"
                    "📜 Բոլոր քարտերը ջնջվեցին։ Սկսե՞լ նոր խաղ։",
                    reply_markup=get_main_menu()
                )
            else:
                await context.bot.send_message(
                    pid,
                    f"🥇 Խաղն ավարտվեց։ Հաղթող՝ {winner_name}\n{card_text}\n"
                    "📜 Բոլոր քարտերը ջնջվեցին։ Սկսե՞լ նոր խաղ։",
                    reply_markup=get_main_menu()
                )
            await asyncio.sleep(0.05)  # Optimized rate limiting
        except Exception as e:
            logger.warning(f"Failed to notify player {pid}: {e}")
    
    for pid in waiting_ids:
        if pid:
            try:
                await context.bot.send_message(
                    pid,
                    "🏁 Խաղն ավարտվեց։\n"
                    "🎮 Սկսեք նոր խաղ կամ միացեք այլ խաղի։",
                    reply_markup=get_main_menu()
                )
                await asyncio.sleep(0.05)  # Optimized rate limiting
            except Exception as e:
                logger.warning(f"Failed to notify waiting player {pid}: {e}")
    
    logger.info(f"Game {game_id} ended with winner {winner_name}")

# Start game
async def start_game(context: ContextTypes.DEFAULT_TYPE):
    game_id = context.job.data['game_id']
    current_game = get_game_by_id(game_id)
    if not current_game or current_game[0] != game_id or current_game[1] != 'preparing':
        logger.warning(f"Failed to start game {game_id}: Invalid game or not preparing")
        return

    update_game_status(game_id, 'running')
    logger.info(f"Starting game {game_id}")
    
    player_ids = current_game[2].split(',')
    
    # Step 1: Announce game start
    for pid in player_ids:
        try:
            await context.bot.send_message(
                pid,
                "🎮 Խաղը սկսվեց։\n\n"
                "🍀 Հաջողություն եմ մաղթում Ձեզ։",
                reply_markup=ReplyKeyboardRemove()
            )
            await asyncio.sleep(0.05)  # Optimized rate limiting
        except Exception as e:
            logger.warning(f"Failed to notify player {pid}: {e}")
    
    # Step 2: Send each player's card
    for pid in player_ids:
        await show_cards(context, int(pid), game_id)
    
    # Step 3: Wait 3 seconds
    await asyncio.sleep(3)
    
    # Step 4: Announce drawing numbers
    for pid in player_ids:
        try:
            await context.bot.send_message(
                pid,
                "🎲 Սկսում եմ հանել թվերը․․․"
            )
            await asyncio.sleep(0.05)  # Optimized rate limiting
        except Exception as e:
            logger.warning(f"Failed to notify player {pid}: {e}")
    
    # Step 5: Wait another 3 seconds
    await asyncio.sleep(3)
    
    # Step 6: Start drawing numbers
    numbers = list(range(1, MAX_NUMBER + 1))
    random.shuffle(numbers)
    drawn_numbers = []
    last_message_ids = {}
    
    for num in numbers:
        current_game = get_game_by_id(game_id)
        if not current_game or current_game[1] != 'running':
            logger.info(f"Game {game_id} stopped or finished")
            break
        player_ids = current_game[2].split(',')
        drawn_numbers.append(str(num))
        for user_id in player_ids:
            if user_id in last_message_ids:
                try:
                    await context.bot.delete_message(user_id, last_message_ids[user_id])
                except Exception as e:
                    logger.warning(f"Failed to delete message for user {user_id}: {e}")
            try:
                message = await context.bot.send_message(
                    user_id,
                    f"🎲 ԹԻՎ՝ *{num}*",
                    parse_mode=ParseMode.MARKDOWN
                )
                last_message_ids[user_id] = message.message_id
                await asyncio.sleep(0.05)  # Optimized rate limiting
            except Exception as e:
                logger.warning(f"Failed to send number {num} to user {user_id}: {e}")
        update_game_status(game_id, 'running', current_number=num, last_message_id=0, drawn_numbers=','.join(drawn_numbers))
        logger.info(f"Game {game_id}: Drew number {num}")
        
        winner_id, winner_card_id = await check_all_winners(context, game_id)
        if winner_id and winner_card_id:
            await end_game(context, game_id, winner_id, winner_card_id)
            break
        
        await asyncio.sleep(5)

# Main function with webhook
async def main():
    # Initialize database
    init_db()
    
    # Create bot application
    application = Application.builder().token(BOT_TOKEN).build()
    await application.initialize()
    logger.info("Application initialized")
    
    # Delete old webhook and set new one
    await application.bot.delete_webhook(drop_pending_updates=True)
    logger.info("Old webhook deleted")
    await application.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
    logger.info(f"Webhook set to {WEBHOOK_URL}")
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", show_help))
    application.add_handler(CommandHandler("add_ad", add_ad_command))
    application.add_handler(CommandHandler("delete_ad", delete_ad_command))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_keyboard))
    application.add_handler(CallbackQueryHandler(button))
    
    # Start webhook
    await application.start()
    await application.updater.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="",
        webhook_url=WEBHOOK_URL
    )
    logger.info(f"Application running on port {PORT}")
    
    # Keep the application running
    await asyncio.Event().wait()

if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        loop.close()
