import discord
from discord.ext import commands, tasks
from discord.ui import Button, View, Select
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

POKETWO_ID = 716390085896962058  # Pokétwo User ID

# ====== Intents ======
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix=".", intents=intents)
bot.remove_command("help")

# ====== Database ======
DB_FILE = "bot_database.db"

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute('CREATE TABLE IF NOT EXISTS blacklisted_channels (channel_id INTEGER PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS log_channels (guild_id INTEGER PRIMARY KEY, channel_id INTEGER)')
    conn.execute('CREATE TABLE IF NOT EXISTS toggles (guild_id INTEGER PRIMARY KEY, keywords_enabled INTEGER)')
    conn.commit()
    conn.close()

init_db()

# ====== Helper Functions ======
def is_keyword_message(msg_content):
    keywords = ["rare ping", "collection pings", "shiny hunt pings"]
    msg_lower = msg_content.lower()
    return any(k.lower() in msg_lower for k in keywords)

def add_blacklist(channel_id):
    conn = get_db()
    conn.execute('INSERT OR IGNORE INTO blacklisted_channels (channel_id) VALUES (?)', (channel_id,))
    conn.commit()
    conn.close()

def remove_blacklist(channel_id):
    conn = get_db()
    conn.execute('DELETE FROM blacklisted_channels WHERE channel_id = ?', (channel_id,))
    conn.commit()
    conn.close()

def get_blacklisted_channels():
    conn = get_db()
    cursor = conn.execute('SELECT channel_id FROM blacklisted_channels')
    rows = cursor.fetchall()
    conn.close()
    return {row['channel_id'] for row in rows}

def set_log_channel(guild_id, channel_id):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO log_channels (guild_id, channel_id) VALUES (?, ?)', (guild_id, channel_id))
    conn.commit()
    conn.close()

def get_log_channel(guild_id):
    conn = get_db()
    cursor = conn.execute('SELECT channel_id FROM log_channels WHERE guild_id = ?', (guild_id,))
    row = cursor.fetchone()
    conn.close()
    return row['channel_id'] if row else None

def set_keywords_toggle(guild_id, enabled: bool):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO toggles (guild_id, keywords_enabled) VALUES (?, ?)', (guild_id, int(enabled)))
    conn.commit()
    conn.close()

def get_keywords_toggle(guild_id):
    conn = get_db()
    cursor = conn.execute('SELECT keywords_enabled FROM toggles WHERE guild_id = ?', (guild_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row['keywords_enabled']) if row else True

# ====== Lock / Unlock ======
lock_timers = {}
lock_duration = 12  # in hours

async def lock_channel(channel: discord.TextChannel):
    perms = channel.overwrites_for(channel.guild.get_member(POKETWO_ID))
    perms.view_channel = False
    perms.send_messages = False
    await channel.set_permissions(channel.guild.get_member(POKETWO_ID), overwrite=perms)
    lock_timers[channel.id] = datetime.now() + timedelta(hours=lock_duration)
    # Create unlock button
    embed = discord.Embed(title="🔒 Channel Locked",
                          description=f"Locked for {lock_duration}h. Click unlock button when done.",
                          color=discord.Color.red(),
                          timestamp=datetime.now())
    view = UnlockView(channel)
    await channel.send(embed=embed, view=view)

async def unlock_channel(channel: discord.TextChannel, user: discord.Member = None):
    perms = channel.overwrites_for(channel.guild.get_member(POKETWO_ID))
    perms.view_channel = None
    perms.send_messages = None
    await channel.set_permissions(channel.guild.get_member(POKETWO_ID), overwrite=perms)
    lock_timers.pop(channel.id, None)
    desc = f"Unlocked by {user.mention}" if user else "Automatically unlocked."
    embed = discord.Embed(title="🔓 Channel Unlocked", description=desc, color=discord.Color.green(), timestamp=datetime.now())
    await channel.send(embed=embed)

class UnlockView(View):
    def __init__(self, channel):
        super().__init__(timeout=None)
        self.channel = channel

    @discord.ui.button(label="Unlock Channel", style=discord.ButtonStyle.green)
    async def unlock_button(self, interaction: discord.Interaction, button: Button):
        await unlock_channel(self.channel, interaction.user)
        await interaction.response.send_message("Channel unlocked!", ephemeral=True)
        self.stop()

# ====== Events ======
@bot.event
async def on_ready():
    logging.info(f"Bot online as {bot.user}")
    if not check_lock_timers.is_running():
        check_lock_timers.start()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if message.channel.id not in get_blacklisted_channels() and is_keyword_message(message.content) and get_keywords_toggle(message.guild.id):
        await lock_channel(message.channel)
        log_channel_id = get_log_channel(message.guild.id)
        if log_channel_id:
            log_channel = bot.get_channel(log_channel_id)
            if log_channel:
                await log_channel.send(f"Locked channel {message.channel.mention} due to keyword.")

    await bot.process_commands(message)

# ====== Lock Timer Task ======
@tasks.loop(seconds=60)
async def check_lock_timers():
    now = datetime.now()
    expired = [cid for cid, end in lock_timers.items() if now >= end]
    for cid in expired:
        channel = bot.get_channel(cid)
        if channel:
            await unlock_channel(channel)
        lock_timers.pop(cid, None)

# ====== Commands ======
@bot.command()
async def lock(ctx):
    """Lock current channel."""
    await lock_channel(ctx.channel)
    await ctx.send("🔒 Channel manually locked.")

@bot.command()
async def unlock(ctx):
    """Unlock current channel."""
    await unlock_channel(ctx.channel, ctx.author)
    await ctx.send("🔓 Channel manually unlocked.")

@bot.command()
async def blacklist(ctx, action: str, channel: discord.TextChannel = None):
    """Add or remove a channel from blacklist."""
    if action.lower() == "add" and channel:
        add_blacklist(channel.id)
        await ctx.send(f"✅ {channel.mention} added to blacklist.")
    elif action.lower() == "remove" and channel:
        remove_blacklist(channel.id)
        await ctx.send(f"✅ {channel.mention} removed from blacklist.")
    else:
        await ctx.send("❌ Usage: `.blacklist add|remove #channel`")

@bot.command()
async def logchannel(ctx, channel: discord.TextChannel):
    """Set a channel to send log messages to."""
    set_log_channel(ctx.guild.id, channel.id)
    await ctx.send(f"✅ Log channel set to {channel.mention}")

@bot.command()
async def togglekeywords(ctx, enabled: str):
    """Enable or disable keyword detection."""
    if enabled.lower() in ["on", "true"]:
        set_keywords_toggle(ctx.guild.id, True)
        await ctx.send("✅ Keyword detection enabled.")
    elif enabled.lower() in ["off", "false"]:
        set_keywords_toggle(ctx.guild.id, False)
        await ctx.send("❌ Keyword detection disabled.")
    else:
        await ctx.send("❌ Usage: `.togglekeywords on|off`")

@bot.command()
async def owner(ctx):
    await ctx.send("🤖 Bot made by Buddy. Happy hunting y'all freaks ❤️")

@bot.command()
async def roll(ctx, sides: int = 6):
    import random
    await ctx.send(f"🎲 You rolled: {random.randint(1, sides)}")

@bot.command()
async def help(ctx):
    embed = discord.Embed(title="Help Menu", color=discord.Color.blue())
    embed.add_field(name=".lock", value="Lock current channel manually", inline=False)
    embed.add_field(name=".unlock", value="Unlock current channel manually", inline=False)
    embed.add_field(name=".blacklist add/remove #channel", value="Manage blacklist", inline=False)
    embed.add_field(name=".logchannel #channel", value="Set log channel", inline=False)
    embed.add_field(name=".togglekeywords on/off", value="Enable or disable keyword detection", inline=False)
    embed.add_field(name=".roll <sides>", value="Roll dice", inline=False)
    embed.add_field(name=".owner", value="Bot info", inline=False)
    await ctx.send(embed=embed)

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
