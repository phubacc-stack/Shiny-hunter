import discord
from discord.ext import commands, tasks
from discord.ui import Button, View
import os
from dotenv import load_dotenv
import logging
from datetime import datetime, timedelta
import sqlite3
import threading
from flask import Flask

# ====== Logging ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ====== Environment Variables ======
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in environment variables.")

SECRET_OWNER_ID = 123456789012345678  # Replace with @smokingpikachu420's actual Discord ID

# ====== Intents ======
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="*", intents=intents)
bot.remove_command("help")

# ====== Configuration ======
lock_duration = 12  # hours
KEYWORDS = ["rare ping", "collection pings", "shiny hunt pings"]
lock_timers = {}

# ====== Database ======
DB_FILE = 'bot_database.db'

def get_db_connection():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS blacklisted_channels (id INTEGER PRIMARY KEY, channel_id INTEGER)')
    conn.execute('CREATE TABLE IF NOT EXISTS blacklisted_categories (id INTEGER PRIMARY KEY, category_id INTEGER)')
    conn.execute('CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, log_channel_id INTEGER, guild_id INTEGER, toggle INTEGER)')
    conn.close()

def add_to_blacklist_db(channel_id):
    conn = get_db_connection()
    conn.execute('INSERT OR IGNORE INTO blacklisted_channels (channel_id) VALUES (?)', (channel_id,))
    conn.commit()
    conn.close()

def remove_from_blacklist_db(channel_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM blacklisted_channels WHERE channel_id = ?', (channel_id,))
    conn.commit()
    conn.close()

def add_category_blacklist(category_id):
    conn = get_db_connection()
    conn.execute('INSERT OR IGNORE INTO blacklisted_categories (category_id) VALUES (?)', (category_id,))
    conn.commit()
    conn.close()

def remove_category_blacklist(category_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM blacklisted_categories WHERE category_id = ?', (category_id,))
    conn.commit()
    conn.close()

def load_blacklisted_channels():
    conn = get_db_connection()
    cursor = conn.execute('SELECT channel_id FROM blacklisted_channels')
    rows = cursor.fetchall()
    conn.close()
    return {row['channel_id'] for row in rows}

def load_blacklisted_categories():
    conn = get_db_connection()
    cursor = conn.execute('SELECT category_id FROM blacklisted_categories')
    rows = cursor.fetchall()
    conn.close()
    return {row['category_id'] for row in rows}

def set_log_channel_db(guild_id, channel_id):
    conn = get_db_connection()
    conn.execute('INSERT OR REPLACE INTO config (guild_id, log_channel_id, toggle) VALUES (?, ?, COALESCE((SELECT toggle FROM config WHERE guild_id=?), 1))', (guild_id, channel_id, guild_id))
    conn.commit()
    conn.close()

def get_log_channel_db(guild_id):
    conn = get_db_connection()
    cursor = conn.execute('SELECT log_channel_id FROM config WHERE guild_id = ?', (guild_id,))
    row = cursor.fetchone()
    conn.close()
    return row['log_channel_id'] if row else None

def set_toggle_db(guild_id, value: int):
    conn = get_db_connection()
    conn.execute('INSERT OR REPLACE INTO config (guild_id, log_channel_id, toggle) VALUES (?, COALESCE((SELECT log_channel_id FROM config WHERE guild_id=?), NULL), ?)', (guild_id, guild_id, value))
    conn.commit()
    conn.close()

def get_toggle_db(guild_id):
    conn = get_db_connection()
    cursor = conn.execute('SELECT toggle FROM config WHERE guild_id = ?', (guild_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row['toggle']) if row else True

# ====== Load Data ======
init_db()
blacklisted_channels = load_blacklisted_channels()
blacklisted_categories = load_blacklisted_categories()

# ====== Admin Check ======
def is_admin():
    async def predicate(ctx):
        role = discord.utils.get(ctx.author.roles, name="Admin")
        if role:
            return True
        await ctx.send("❌ You must have the **Admin** role to use this command.")
        return False
    return commands.check(predicate)

# ====== Helper Functions ======
async def lock_channel(channel):
    await channel.set_permissions(channel.guild.default_role, send_messages=False)
    end_time = datetime.now() + timedelta(hours=lock_duration)
    lock_timers[channel.id] = end_time
    await channel.send(f"🔒 Channel locked for {lock_duration} hours.")

async def unlock_channel(channel):
    await channel.set_permissions(channel.guild.default_role, send_messages=True)
    lock_timers.pop(channel.id, None)
    await channel.send(f"🔓 Channel unlocked!")

def contains_keyword(message):
    content = message.content.lower()
    return any(keyword in content for keyword in KEYWORDS)

# ====== Bot Events ======
@bot.event
async def on_ready():
    logging.info(f"Bot online as {bot.user}")
    if not check_lock_timers.is_running():
        check_lock_timers.start()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Auto-lock if keyword detected
    if get_toggle_db(message.guild.id) and message.author.bot and contains_keyword(message):
        if message.channel.id not in blacklisted_channels and (message.channel.category_id not in blacklisted_categories if message.channel.category_id else True):
            await lock_channel(message.channel)

    await bot.process_commands(message)

# ====== Lock Timer Task ======
@tasks.loop(seconds=60)
async def check_lock_timers():
    now = datetime.now()
    expired_channels = [cid for cid, end_time in lock_timers.items() if now >= end_time]
    for cid in expired_channels:
        channel = bot.get_channel(cid)
        if channel:
            await unlock_channel(channel)

# ====== Admin Commands ======
@bot.command()
@is_admin()
async def blacklist(ctx, action, target=None):
    if action.lower() == "add" and isinstance(target, discord.TextChannel):
        add_to_blacklist_db(target.id)
        blacklisted_channels.add(target.id)
        await ctx.send(f"✅ Channel {target.mention} blacklisted.")
    elif action.lower() == "remove" and isinstance(target, discord.TextChannel):
        remove_from_blacklist_db(target.id)
        blacklisted_channels.discard(target.id)
        await ctx.send(f"✅ Channel {target.mention} removed from blacklist.")
    elif action.lower() == "list":
        if blacklisted_channels:
            channels = [bot.get_channel(cid).mention for cid in blacklisted_channels if bot.get_channel(cid)]
            await ctx.send("📜 Blacklisted Channels:\n" + "\n".join(channels))
        else:
            await ctx.send("No channels are blacklisted.")
    elif action.lower() == "addcategory" and isinstance(target, discord.CategoryChannel):
        add_category_blacklist(target.id)
        blacklisted_categories.add(target.id)
        await ctx.send(f"✅ Category {target.name} blacklisted.")
    elif action.lower() == "removecategory" and isinstance(target, discord.CategoryChannel):
        remove_category_blacklist(target.id)
        blacklisted_categories.discard(target.id)
        await ctx.send(f"✅ Category {target.name} removed from blacklist.")
    else:
        await ctx.send("❌ Invalid action or target.")

@bot.command()
@is_admin()
async def setlog(ctx, channel: discord.TextChannel):
    set_log_channel_db(ctx.guild.id, channel.id)
    await ctx.send(f"✅ Log channel set to {channel.mention}")

@bot.command()
@is_admin()
async def toggle(ctx, option: str):
    if option.lower() == "on":
        set_toggle_db(ctx.guild.id, 1)
        await ctx.send("✅ Keyword detection enabled.")
    elif option.lower() == "off":
        set_toggle_db(ctx.guild.id, 0)
        await ctx.send("✅ Keyword detection disabled.")
    else:
        await ctx.send("❌ Use `on` or `off`.")

@bot.command()
@is_admin()
async def lock(ctx, channel: discord.TextChannel):
    await lock_channel(channel)

@bot.command()
@is_admin()
async def unlock(ctx, channel: discord.TextChannel):
    await unlock_channel(channel)

@bot.command()
@is_admin()
async def giverole(ctx, member: discord.Member, *, role_name):
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"❌ Role `{role_name}` not found.")
        return
    await member.add_roles(role)
    await ctx.send(f"✅ {member.mention} given `{role.name}` role.")

@bot.command()
@is_admin()
async def removerole(ctx, member: discord.Member, *, role_name):
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"❌ Role `{role_name}` not found.")
        return
    await member.remove_roles(role)
    await ctx.send(f"✅ `{role.name}` removed from {member.mention}.")

@bot.command()
@is_admin()
async def listroles(ctx):
    roles = [role.name for role in ctx.guild.roles if role.name != "@everyone"]
    await ctx.send("📜 Roles in server:\n" + "\n".join(roles))

@bot.command()
@is_admin()
async def menu(ctx):
    """Admin menu placeholder"""
    await ctx.send("Admin menu placeholder — buttons can be implemented here.")

# ====== Owner Command ======
@bot.command()
async def owner(ctx):
    await ctx.send("Bot made by Buddy — happy hunting yall freaks ❤️")

# ====== Secret Owner-Only Command ======
@bot.command()
async def botstatus(ctx):
    if ctx.author.id != SECRET_OWNER_ID:
        return
    guilds = bot.guilds
    info = [f"{g.name} ({g.id}) — {len(g.members)} members" for g in guilds]
    await ctx.send(f"Bot is in {len(guilds)} servers:\n" + "\n".join(info))

# ====== Flask Keep-Alive ======
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run_flask).start()

# ====== Run Bot ======
bot.run(BOT_TOKEN)
