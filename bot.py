import discord
from discord.ext import commands, tasks
from discord.ui import Button, View, Select
import os
from dotenv import load_dotenv
import logging
from datetime import datetime, timedelta
import sqlite3
import threading
import random
from flask import Flask

# ====== Logging ======
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# ====== Environment Variables ======
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in environment variables.")

POKETWO_ID = 716390085896962058

# ====== Intents ======
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
bot = commands.Bot(command_prefix="*", intents=intents)
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
    conn.execute('CREATE TABLE IF NOT EXISTS blacklisted_categories (category_id INTEGER PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS log_channels (guild_id INTEGER PRIMARY KEY, channel_id INTEGER)')
    conn.execute('CREATE TABLE IF NOT EXISTS toggles (guild_id INTEGER PRIMARY KEY, keywords_enabled INTEGER)')
    conn.execute('CREATE TABLE IF NOT EXISTS keywords (guild_id INTEGER, keyword TEXT, enabled INTEGER, PRIMARY KEY(guild_id, keyword))')
    conn.commit()
    conn.close()

init_db()

DEFAULT_KEYWORDS = ["rare ping", "collection pings", "shiny hunt pings"]

# ====== Helper Functions ======
def init_guild_keywords(guild_id):
    """Ensure all default keywords exist for the guild and are enabled by default."""
    conn = get_db()
    for kw in DEFAULT_KEYWORDS:
        conn.execute(
            'INSERT OR IGNORE INTO keywords (guild_id, keyword, enabled) VALUES (?, ?, ?)',
            (guild_id, kw, 1)  # default ON
        )
    conn.commit()
    conn.close()

def is_keyword_enabled(guild_id, keyword):
    """Check if a keyword is enabled for a guild. Defaults to True."""
    conn = get_db()
    cursor = conn.execute('SELECT enabled FROM keywords WHERE guild_id=? AND keyword=?', (guild_id, keyword))
    row = cursor.fetchone()
    conn.close()
    return bool(row['enabled']) if row else True

def set_keyword_toggle(guild_id, keyword, enabled):
    """Enable or disable a keyword for a guild."""
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO keywords (guild_id, keyword, enabled) VALUES (?, ?, ?)',
        (guild_id, keyword, int(enabled))
    )
    conn.commit()
    conn.close()

def is_keyword_message(msg_content, guild_id):
    """Check if a message contains any enabled keywords."""
    msg_lower = msg_content.lower()
    for k in DEFAULT_KEYWORDS:
        if k.lower() in msg_lower and is_keyword_enabled(guild_id, k):
            return True
    return False

def add_blacklist_channel(channel_id):
    conn = get_db()
    conn.execute('INSERT OR IGNORE INTO blacklisted_channels (channel_id) VALUES (?)', (channel_id,))
    conn.commit()
    conn.close()

def remove_blacklist_channel(channel_id):
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

def add_blacklist_category(category_id):
    conn = get_db()
    conn.execute('INSERT OR IGNORE INTO blacklisted_categories (category_id) VALUES (?)', (category_id,))
    conn.commit()
    conn.close()

def remove_blacklist_category(category_id):
    conn = get_db()
    conn.execute('DELETE FROM blacklisted_categories WHERE category_id = ?', (category_id,))
    conn.commit()
    conn.close()

def get_blacklisted_categories():
    conn = get_db()
    cursor = conn.execute('SELECT category_id FROM blacklisted_categories')
    rows = cursor.fetchall()
    conn.close()
    return {row['category_id'] for row in rows}

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

# ====== Lock / Unlock ======
lock_timers = {}
lock_duration = 12

async def lock_channel(channel: discord.TextChannel):
    perms = channel.overwrites_for(channel.guild.get_member(POKETWO_ID))
    perms.view_channel = False
    perms.send_messages = False
    await channel.set_permissions(channel.guild.get_member(POKETWO_ID), overwrite=perms)
    lock_timers[channel.id] = datetime.now() + timedelta(hours=lock_duration)
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

# ====== Interactive Menu with per-keyword toggles ======
class SettingsView(View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx
        self.add_item(SettingsSelect(ctx))

class SettingsSelect(Select):
    def __init__(self, ctx):
        options = [
            discord.SelectOption(label="Manage Keywords", description="Enable or disable each keyword individually"),
            discord.SelectOption(label="Set Log Channel", description="Set the channel for logs"),
            discord.SelectOption(label="Manage Blacklist", description="Add/remove channels or categories from blacklist")
        ]
        super().__init__(placeholder="Select a setting...", min_values=1, max_values=1, options=options)
        self.ctx = ctx

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        if choice == "Manage Keywords":
            view = KeywordToggleView(self.ctx)
            await interaction.response.send_message("Toggle individual keywords:", view=view, ephemeral=True)
        elif choice == "Set Log Channel":
            await interaction.response.send_message("Please mention the channel to set as log channel (example: #logs).", ephemeral=True)
        elif choice == "Manage Blacklist":
            channels = [discord.SelectOption(label=c.name, value=str(c.id)) for c in self.ctx.guild.text_channels]
            categories = [discord.SelectOption(label=c.name, value=f"cat_{c.id}") for c in self.ctx.guild.categories]
            view = BlacklistView(channels, categories, self.ctx)
            await interaction.response.send_message("Select channels/categories to toggle blacklist:", view=view, ephemeral=True)

class KeywordToggleView(View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx
        options = []
        for kw in DEFAULT_KEYWORDS:
            enabled = is_keyword_enabled(ctx.guild.id, kw)
            options.append(discord.SelectOption(
                label=kw,
                description=f"{'Enabled' if enabled else 'Disabled'}",
                value=kw
            ))
        self.add_item(KeywordSelect(options, ctx))

class KeywordSelect(Select):
    def __init__(self, options, ctx):
        super().__init__(placeholder="Select keywords to toggle...", min_values=1, max_values=len(options), options=options)
        self.ctx = ctx

    async def callback(self, interaction: discord.Interaction):
        toggled_keywords = []
        for kw in self.values:
            current = is_keyword_enabled(self.ctx.guild.id, kw)
            set_keyword_toggle(self.ctx.guild.id, kw, not current)
            toggled_keywords.append(f"{kw} ({'ON' if not current else 'OFF'})")
        await interaction.response.send_message(
            f"Toggled keywords: {', '.join(toggled_keywords)}",
            ephemeral=True
        )

# ====== Blacklist Menu ======
class BlacklistView(View):
    def __init__(self, channels, categories, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx
        self.add_item(BlacklistSelect(channels + categories, ctx))

class BlacklistSelect(Select):
    def __init__(self, options, ctx):
        super().__init__(placeholder="Select channels/categories...", min_values=1, max_values=len(options), options=options)
        self.ctx = ctx

    async def callback(self, interaction: discord.Interaction):
        added = []
        removed = []
        for val in self.values:
            if val.startswith("cat_"):
                cat_id = int(val.split("_")[1])
                if cat_id in get_blacklisted_categories():
                    remove_blacklist_category(cat_id)
                    removed.append(f"Category {cat_id}")
                else:
                    add_blacklist_category(cat_id)
                    added.append(f"Category {cat_id}")
            else:
                ch_id = int(val)
                if ch_id in get_blacklisted_channels():
                    remove_blacklist_channel(ch_id)
                    removed.append(f"<#{ch_id}>")
                else:
                    add_blacklist_channel(ch_id)
                    added.append(f"<#{ch_id}>")
        msg = ""
        if added:
            msg += "✅ Added to blacklist: " + ", ".join(added) + "\n"
        if removed:
            msg += "❌ Removed from blacklist: " + ", ".join(removed)
        await interaction.response.send_message(msg, ephemeral=True)

# ====== Events ======
@bot.event
async def on_ready():
    logging.info(f"Bot online as {bot.user}")
    for guild in bot.guilds:
        init_guild_keywords(guild.id)  # Initialize keywords default ON
    if not check_lock_timers.is_running():
        check_lock_timers.start()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    blacklisted_channels = get_blacklisted_channels()
    blacklisted_categories = get_blacklisted_categories()
    if (message.channel.id not in blacklisted_channels
        and (message.channel.category_id not in blacklisted_categories if message.channel.category_id else True)
        and is_keyword_message(message.content, message.guild.id)):
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
    await lock_channel(ctx.channel)
    await ctx.send("🔒 Channel manually locked.")

@bot.command()
async def unlock(ctx):
    await unlock_channel(ctx.channel, ctx.author)
    await ctx.send("🔓 Channel manually unlocked.")

@bot.command()
async def blacklist(ctx, action: str, target: str = None, category: str = None):
    if action.lower() not in ["add", "remove"]:
        await ctx.send("❌ Usage: `*blacklist add/remove #channel` or `*blacklist add/remove category <name>`")
        return
    if target and target.startswith("<#") and target.endswith(">"):
        channel_id = int(target[2:-1])
        if action.lower() == "add":
            add_blacklist_channel(channel_id)
            await ctx.send(f"✅ <#{channel_id}> added to blacklist.")
        else:
            remove_blacklist_channel(channel_id)
            await ctx.send(f"✅ <#{channel_id}> removed from blacklist.")
    elif category:
        cat_obj = discord.utils.get(ctx.guild.categories, name=category)
        if not cat_obj:
            await ctx.send("❌ Category not found.")
            return
        if action.lower() == "add":
            add_blacklist_category(cat_obj.id)
            await ctx.send(f"✅ Category **{category}** added to blacklist.")
        else:
            remove_blacklist_category(cat_obj.id)
            await ctx.send(f"✅ Category **{category}** removed from blacklist.")
    else:
        await ctx.send("❌ Please specify a channel or category.")

@bot.command()
async def logchannel(ctx, channel: discord.TextChannel):
    set_log_channel(ctx.guild.id, channel.id)
    await ctx.send(f"✅ Log channel set to {channel.mention}")

@bot.command()
async def menu(ctx):
    view = SettingsView(ctx)
    await ctx.send("Select a setting to configure:", view=view)

@bot.command()
async def owner(ctx):
    await ctx.send("🤖 Bot made by Buddy. Happy hunting y'all freaks ❤️")

@bot.command()
async def roll(ctx, sides: int = 6):
    await ctx.send(f"🎲 You rolled: {random.randint(1, sides)}")

@bot.command()
async def help(ctx):
    embed = discord.Embed(title="Help Menu", color=discord.Color.blue())
    embed.add_field(name="*lock", value="Lock current channel manually", inline=False)
    embed.add_field(name="*unlock", value="Unlock current channel manually", inline=False)
    embed.add_field(name="*blacklist add/remove #channel or category <name>", value="Manage blacklist", inline=False)
    embed.add_field(name="*logchannel #channel", value="Set log channel", inline=False)
    embed.add_field(name="*menu", value="Interactive settings menu", inline=False)
    embed.add_field(name="*roll <sides>", value="Roll dice", inline=False)
    embed.add_field(name="*owner", value="Bot info", inline=False)
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
