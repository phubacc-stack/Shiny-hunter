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
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in environment variables.")

POKETWO_ID = 716390085896962058  # Pokétwo ID

# ===== Intents =====
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix=".", intents=intents)
bot.remove_command("help")

# ===== Lock Settings =====
lock_duration = 12  # Default in hours
KEYWORDS = {
    "rare ping": True,
    "collection pings": True,
    "shiny hunt pings": True,
}
blacklisted_channels = set()
lock_timers = {}
log_channels = {}  # guild_id -> channel_id

# ===== Database =====
def get_db_connection():
    conn = sqlite3.connect('bot_database.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS blacklisted_channels (channel_id INTEGER PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS log_channels (guild_id INTEGER PRIMARY KEY, channel_id INTEGER)')
    conn.commit()
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

def load_blacklisted_channels():
    conn = get_db_connection()
    rows = conn.execute('SELECT channel_id FROM blacklisted_channels').fetchall()
    conn.close()
    return {row['channel_id'] for row in rows}

def set_log_channel_db(guild_id, channel_id):
    conn = get_db_connection()
    conn.execute('INSERT OR REPLACE INTO log_channels (guild_id, channel_id) VALUES (?, ?)', (guild_id, channel_id))
    conn.commit()
    conn.close()

def load_log_channels():
    conn = get_db_connection()
    rows = conn.execute('SELECT guild_id, channel_id FROM log_channels').fetchall()
    conn.close()
    return {row['guild_id']: row['channel_id'] for row in rows}

init_db()
blacklisted_channels = load_blacklisted_channels()
log_channels = load_log_channels()

# ===== Unlock Button (anyone can click) =====
class UnlockView(View):
    def __init__(self, channel):
        super().__init__(timeout=None)
        self.channel = channel

    @discord.ui.button(label="Unlock Channel", style=discord.ButtonStyle.green)
    async def unlock_button(self, interaction: discord.Interaction, button: Button):
        await unlock_channel(self.channel, interaction.user)
        await interaction.response.send_message("Channel unlocked!", ephemeral=True)
        self.stop()

# ===== Helper Functions =====
async def set_channel_permissions(channel, view_channel=None, send_messages=None):
    guild = channel.guild
    try:
        poketwo = await guild.fetch_member(POKETWO_ID)
    except discord.NotFound:
        logging.warning("Pokétwo bot not found in this server.")
        return

    overwrite = channel.overwrites_for(poketwo)
    overwrite.view_channel = view_channel if view_channel is not None else True
    overwrite.send_messages = send_messages if send_messages is not None else True
    await channel.set_permissions(poketwo, overwrite=overwrite)

async def lock_channel(channel):
    await set_channel_permissions(channel, view_channel=False, send_messages=False)
    end_time = datetime.now() + timedelta(hours=lock_duration)
    lock_timers[channel.id] = end_time

async def unlock_channel(channel, user):
    await set_channel_permissions(channel, view_channel=None, send_messages=None)
    lock_timers.pop(channel.id, None)
    remove_from_blacklist_db(channel.id)
    embed = discord.Embed(
        title="🔓 Channel Unlocked",
        description=f"Channel unlocked by {user.mention} (or automatically after timer expired).",
        color=discord.Color.green(),
        timestamp=datetime.now(),
    )
    embed.set_footer(text="Happy hunting!")
    await channel.send(embed=embed)

def is_keyword_message(message: discord.Message):
    content = message.content.lower()
    return any(k.lower() in content for k in KEYWORDS)

def is_unusual_colors(message: discord.Message):
    return "these colors seem unusual" in message.content.lower()

# ===== Bot Events =====
@bot.event
async def on_ready():
    logging.info(f"Bot online as {bot.user}")
    global blacklisted_channels, log_channels
    blacklisted_channels = load_blacklisted_channels()
    log_channels = load_log_channels()
    if not check_lock_timers.is_running():
        check_lock_timers.start()

@bot.event
async def on_message(message):
    try:
        if message.author == bot.user:
            return

        # Keyword lock
        if message.author.bot and is_keyword_message(message):
            if message.channel.id not in blacklisted_channels:
                await lock_channel(message.channel)
                add_to_blacklist_db(message.channel.id)
                embed = discord.Embed(
                    title="🔒 Channel Locked",
                    description=f"This channel has been locked for {lock_duration} hours due to a Pokétwo alert.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(),
                )
                embed.add_field(name="Lock Ends At", value=(datetime.now() + timedelta(hours=lock_duration)).strftime("%Y-%m-%d %H:%M:%S"))
                embed.set_footer(text="Click the button below to unlock the channel manually.")
                view = UnlockView(channel=message.channel)
                await message.channel.send(embed=embed, view=view)

        # Unusual colors detection
        if message.author.id == POKETWO_ID and is_unusual_colors(message):
            embed = discord.Embed(
                title="🎉 Shiny Catch!",
                description=f"Congratulations! Something unusual was caught! ✨",
                color=discord.Color.gold(),
                timestamp=datetime.now(),
            )
            embed.add_field(name="Pokétwo Message", value=message.content, inline=False)
            # Send to log channel if set
            guild_id = message.guild.id
            log_channel_id = log_channels.get(guild_id)
            if log_channel_id:
                log_channel = bot.get_channel(log_channel_id)
                if log_channel:
                    await log_channel.send(embed=embed)
                    return
            await message.channel.send(embed=embed)

        await bot.process_commands(message)
    except Exception as e:
        logging.error(f"Error in on_message: {e}")

# ===== Lock Timer Task =====
@tasks.loop(seconds=60)
async def check_lock_timers():
    now = datetime.now()
    expired = [cid for cid, end in lock_timers.items() if now >= end]
    for cid in expired:
        channel = bot.get_channel(cid)
        if channel:
            await unlock_channel(channel, bot.user)
            logging.info(f"Channel {channel.name} automatically unlocked.")
        lock_timers.pop(cid, None)

# ===== Commands =====
def is_admin():
    async def predicate(ctx):
        role = discord.utils.get(ctx.author.roles, name="Admin")
        if role:
            return True
        await ctx.send("❌ You must have the Admin role to use this command.")
        return False
    return commands.check(predicate)

@bot.command()
@is_admin()
async def lock(ctx):
    await lock_channel(ctx.channel)
    await ctx.send(f"🔒 Channel locked manually for {lock_duration} hours.")

@bot.command()
@is_admin()
async def unlock(ctx):
    await unlock_channel(ctx.channel, ctx.author)
    await ctx.send("🔓 Channel unlocked manually.")

@bot.command()
@is_admin()
async def setlog(ctx, channel: discord.TextChannel):
    log_channels[ctx.guild.id] = channel.id
    set_log_channel_db(ctx.guild.id, channel.id)
    await ctx.send(f"✅ Log channel set to {channel.mention}")

# ===== Flask Keep-Alive =====
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run_flask).start()

# ===== Run Bot =====
bot.run(BOT_TOKEN)
