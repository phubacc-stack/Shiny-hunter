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

# ===== Logging =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ===== Environment Variables =====
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in the environment variables.")

# ===== Intents & Bot =====
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="*", intents=intents)
bot.remove_command("help")

# ===== Configuration =====
lock_duration = 12  # hours
KEYWORDS = ["rare ping", "collection pings", "shiny hunt pings"]
blacklisted_channels = set()
lock_timers = {}
toggles = {}  # toggle per guild

# ===== Database =====
def get_db_connection():
    conn = sqlite3.connect("bot_database.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS blacklisted_channels (id INTEGER PRIMARY KEY, channel_id INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, log_channel_id INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS toggles (guild_id INTEGER PRIMARY KEY, enabled INTEGER)"
    )
    conn.close()

def add_to_blacklist_db(channel_id):
    conn = get_db_connection()
    conn.execute("INSERT OR IGNORE INTO blacklisted_channels (channel_id) VALUES (?)", (channel_id,))
    conn.commit()
    conn.close()

def remove_from_blacklist_db(channel_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM blacklisted_channels WHERE channel_id = ?", (channel_id,))
    conn.commit()
    conn.close()

def load_blacklisted_channels():
    conn = get_db_connection()
    cursor = conn.execute("SELECT channel_id FROM blacklisted_channels")
    rows = cursor.fetchall()
    conn.close()
    return {row['channel_id'] for row in rows}

def set_log_channel_db(channel_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM config")
    conn.execute("INSERT INTO config (log_channel_id) VALUES (?)", (channel_id,))
    conn.commit()
    conn.close()

def get_log_channel_db():
    conn = get_db_connection()
    cursor = conn.execute("SELECT log_channel_id FROM config")
    row = cursor.fetchone()
    conn.close()
    return row['log_channel_id'] if row else None

def set_toggle_db(guild_id, state):
    conn = get_db_connection()
    conn.execute("INSERT OR REPLACE INTO toggles (guild_id, enabled) VALUES (?, ?)", (guild_id, state))
    conn.commit()
    conn.close()

def get_toggle_db(guild_id):
    conn = get_db_connection()
    cursor = conn.execute("SELECT enabled FROM toggles WHERE guild_id = ?", (guild_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row['enabled']) if row else True  # default enabled

init_db()
blacklisted_channels = load_blacklisted_channels()

# ===== Admin Check =====
def is_admin():
    async def predicate(ctx):
        role = discord.utils.get(ctx.author.roles, name="Admin")
        if role:
            return True
        await ctx.send("❌ You must have the **Admin** role to use this command.")
        return False
    return commands.check(predicate)

def is_owner():
    async def predicate(ctx):
        return ctx.author.id == OWNER_ID
    return commands.check(predicate)

# ===== Helper Functions =====
async def lock_channel(channel):
    await channel.set_permissions(channel.guild.default_role, send_messages=False)
    end_time = datetime.now() + timedelta(hours=lock_duration)
    lock_timers[channel.id] = end_time
    await channel.send(f"🔒 Channel locked for {lock_duration} hours.")

async def unlock_channel(channel, user):
    await channel.set_permissions(channel.guild.default_role, send_messages=True)
    lock_timers.pop(channel.id, None)
    await channel.send(f"🔓 Channel unlocked by {user.mention}!")

def contains_keyword(message):
    content = message.content.lower()
    return any(keyword in content for keyword in KEYWORDS)

# ===== Bot Events =====
@bot.event
async def on_ready():
    logging.info(f"Bot online as {bot.user}")
    global blacklisted_channels
    blacklisted_channels = load_blacklisted_channels()
    for guild in bot.guilds:
        toggles[guild.id] = get_toggle_db(guild.id)
    if not check_lock_timers.is_running():
        check_lock_timers.start()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    guild_id = message.guild.id
    if not toggles.get(guild_id, True):
        return
    if message.author.bot and contains_keyword(message) and message.channel.id not in blacklisted_channels:
        await lock_channel(message.channel)
    await bot.process_commands(message)

# ===== Lock Timer Task =====
@tasks.loop(seconds=60)
async def check_lock_timers():
    now = datetime.now()
    expired_channels = [cid for cid, end_time in lock_timers.items() if now >= end_time]
    for cid in expired_channels:
        channel = bot.get_channel(cid)
        if channel:
            await unlock_channel(channel, bot.user)
        lock_timers.pop(cid, None)

# ===== Admin Commands =====
@bot.command()
@is_admin()
async def blacklist(ctx, action, *, target=None):
    global blacklisted_channels
    if action.lower() == "add":
        # Blacklist channel or category
        if target:
            if target.startswith("<#") and target.endswith(">"):
                channel_id = int(target[2:-1])
                add_to_blacklist_db(channel_id)
                blacklisted_channels.add(channel_id)
                await ctx.send(f"✅ Channel <#{channel_id}> added to blacklist.")
            else:
                # Assume category
                category = discord.utils.get(ctx.guild.categories, name=target)
                if category:
                    for ch in category.channels:
                        add_to_blacklist_db(ch.id)
                        blacklisted_channels.add(ch.id)
                    await ctx.send(f"✅ All channels in category '{target}' blacklisted.")
                else:
                    await ctx.send("❌ Category not found.")
        else:
            await ctx.send("❌ Please specify a channel or category.")
    elif action.lower() == "remove":
        if target:
            if target.startswith("<#") and target.endswith(">"):
                channel_id = int(target[2:-1])
                remove_from_blacklist_db(channel_id)
                blacklisted_channels.discard(channel_id)
                await ctx.send(f"✅ Channel <#{channel_id}> removed from blacklist.")
            else:
                category = discord.utils.get(ctx.guild.categories, name=target)
                if category:
                    for ch in category.channels:
                        remove_from_blacklist_db(ch.id)
                        blacklisted_channels.discard(ch.id)
                    await ctx.send(f"✅ All channels in category '{target}' removed from blacklist.")
                else:
                    await ctx.send("❌ Category not found.")
        else:
            await ctx.send("❌ Please specify a channel or category.")
    elif action.lower() == "list":
        if blacklisted_channels:
            channels = [f"<#{cid}>" for cid in blacklisted_channels if bot.get_channel(cid)]
            await ctx.send("📜 Blacklisted Channels:\n" + "\n".join(channels))
        else:
            await ctx.send("No channels are blacklisted.")
    else:
        await ctx.send("❌ Invalid action. Use add/remove/list.")

@bot.command()
@is_admin()
async def setlog(ctx, channel: discord.TextChannel):
    set_log_channel_db(channel.id)
    await ctx.send(f"✅ Log channel set to {channel.mention}")

@bot.command()
@is_admin()
async def toggle(ctx, state: str):
    """Enable or disable keyword locking per server"""
    state = state.lower()
    if state not in ["on", "off"]:
        return await ctx.send("❌ Invalid state. Use `on` or `off`.")
    toggles[ctx.guild.id] = state == "on"
    set_toggle_db(ctx.guild.id, state == "on")
    await ctx.send(f"✅ Keyword locking is now {'enabled' if state=='on' else 'disabled'}.")

# ===== Owner Secret Command =====
@bot.command()
@is_owner()
async def secret(ctx, action=None, *, server=None):
    """Shows bot status and server management options"""
    if action is None:
        embed = discord.Embed(title="🤖 Bot Status", color=discord.Color.blurple())
        embed.add_field(name="Servers", value=str(len(bot.guilds)))
        embed.add_field(name="Uptime", value=str(datetime.now() - bot.start_time))
        await ctx.send(embed=embed)
        return
    if action.lower() == "leave" and server:
        guild = discord.utils.get(bot.guilds, name=server)
        if guild:
            await guild.leave()
            await ctx.send(f"✅ Left server: {server}")
        else:
            await ctx.send("❌ Server not found.")

# ===== Owner Command =====
@bot.command()
async def owner(ctx):
    await ctx.send("Bot made by Buddy — happy hunting yall freaks ❤️")

# ===== Flask Keep-Alive =====
app = Flask("")

@app.route("/")
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_flask).start()

# ===== Run Bot =====
bot.start_time = datetime.now()
bot.run(BOT_TOKEN)
