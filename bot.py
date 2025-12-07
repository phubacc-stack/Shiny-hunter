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
POKETWO_ID = 716390085896962058

# ===== Intents =====
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="*", intents=intents)
bot.remove_command("help")

# ===== Config =====
lock_duration = 12
KEYWORDS = ["rare ping", "collection pings", "shiny hunt pings"]
keywords_enabled = True
blacklisted_channels = set()
blacklisted_categories = set()
lock_timers = {}

# ===== Database =====
def get_db_connection():
    conn = sqlite3.connect('bot_database.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS blacklisted_channels (channel_id INTEGER PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS blacklisted_categories (category_id INTEGER PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS config (log_channel_id INTEGER)')
    conn.close()

def load_blacklists():
    conn = get_db_connection()
    channels = {row['channel_id'] for row in conn.execute("SELECT channel_id FROM blacklisted_channels")}
    categories = {row['category_id'] for row in conn.execute("SELECT category_id FROM blacklisted_categories")}
    conn.close()
    return channels, categories

def add_to_blacklist(channel_id=None, category_id=None):
    conn = get_db_connection()
    if channel_id:
        conn.execute("INSERT OR IGNORE INTO blacklisted_channels VALUES (?)", (channel_id,))
    if category_id:
        conn.execute("INSERT OR IGNORE INTO blacklisted_categories VALUES (?)", (category_id,))
    conn.commit()
    conn.close()

def remove_from_blacklist(channel_id=None, category_id=None):
    conn = get_db_connection()
    if channel_id:
        conn.execute("DELETE FROM blacklisted_channels WHERE channel_id = ?", (channel_id,))
    if category_id:
        conn.execute("DELETE FROM blacklisted_categories WHERE category_id = ?", (category_id,))
    conn.commit()
    conn.close()

def set_log_channel(channel_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM config")
    conn.execute("INSERT INTO config (log_channel_id) VALUES (?)", (channel_id,))
    conn.commit()
    conn.close()

def get_log_channel():
    conn = get_db_connection()
    row = conn.execute("SELECT log_channel_id FROM config").fetchone()
    conn.close()
    return row['log_channel_id'] if row else None

init_db()
blacklisted_channels, blacklisted_categories = load_blacklists()

# ===== Permissions =====
def is_admin():
    async def predicate(ctx):
        if discord.utils.get(ctx.author.roles, name="Admin"):
            return True
        await ctx.send("❌ You need the **Admin** role.")
        return False
    return commands.check(predicate)

def is_owner():
    async def predicate(ctx):
        return ctx.author.id == OWNER_ID
    return commands.check(predicate)

# ===== Unlock Button =====
class UnlockView(View):
    def __init__(self, channel):
        super().__init__(timeout=None)
        self.channel = channel

    @discord.ui.button(label="Unlock Channel", style=discord.ButtonStyle.green)
    async def unlock_button(self, interaction, button):
        await unlock_channel(self.channel, interaction.user)
        await interaction.response.send_message("Channel unlocked!", ephemeral=True)
        self.stop()

# ===== Helper =====
async def set_channel_permissions(channel, view_channel=None, send_messages=None):
    guild = channel.guild
    try:
        poketwo = await guild.fetch_member(POKETWO_ID)
    except discord.NotFound:
        return

    overwrite = channel.overwrites_for(poketwo)
    overwrite.view_channel = view_channel
    overwrite.send_messages = send_messages
    await channel.set_permissions(poketwo, overwrite=overwrite)

async def lock_channel(channel):
    await set_channel_permissions(channel, view_channel=False, send_messages=False)
    lock_timers[channel.id] = datetime.now() + timedelta(hours=lock_duration)

    embed = discord.Embed(
        title="🔒 Channel Locked",
        description=f"Locked for **{lock_duration} hours**.",
        color=discord.Color.red()
    )
    await channel.send(embed=embed, view=UnlockView(channel))

async def unlock_channel(channel, user):
    await set_channel_permissions(channel, view_channel=None, send_messages=None)
    lock_timers.pop(channel.id, None)

    embed = discord.Embed(
        title="🔓 Channel Unlocked",
        description=f"Unlocked by {user.mention}",
        color=discord.Color.green()
    )
    await channel.send(embed=embed)

def contains_keyword(message):
    if not keywords_enabled:
        return False
    content = message.content.lower()
    return any(k in content for k in KEYWORDS)

# ===== Events =====
@bot.event
async def on_ready():
    global blacklisted_channels, blacklisted_categories
    blacklisted_channels, blacklisted_categories = load_blacklists()
    if not lock_timer_task.is_running():
        lock_timer_task.start()
    logging.info(f"Bot is online as {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot and contains_keyword(message):

        # Channel blacklist
        if message.channel.id in blacklisted_channels:
            return

        # Category blacklist
        if message.channel.category_id in blacklisted_categories:
            return

        await lock_channel(message.channel)

    await bot.process_commands(message)

# ===== Timer =====
@tasks.loop(seconds=60)
async def lock_timer_task():
    now = datetime.now()
    expired = [cid for cid, t in lock_timers.items() if now >= t]

    for cid in expired:
        channel = bot.get_channel(cid)
        if channel:
            await unlock_channel(channel, bot.user)
        lock_timers.pop(cid, None)

# ===== BLACKLIST COMMAND =====
@bot.command()
@is_admin()
async def blacklist(ctx, mode=None, action=None, *, target=None):
    global blacklisted_channels, blacklisted_categories

    if mode is None:
        await ctx.send(
            "Usage:\n"
            "`*blacklist channel add #channel`\n"
            "`*blacklist channel remove #channel`\n"
            "`*blacklist channel list`\n"
            "`*blacklist category add <name>`\n"
            "`*blacklist category remove <name>`\n"
            "`*blacklist category list`"
        )
        return

    # CATEGORY
    if mode.lower() == "category":
        if action is None:
            await ctx.send("Usage: `*blacklist category add/remove/list <category>`")
            return

        if action.lower() == "list":
            if not blacklisted_categories:
                await ctx.send("No categories blacklisted.")
                return
            names = []
            for cid in blacklisted_categories:
                cat = discord.utils.get(ctx.guild.categories, id=cid)
                if cat:
                    names.append(cat.name)
            await ctx.send("📜 Blacklisted Categories:\n" + "\n".join(names))
            return

        category = discord.utils.get(ctx.guild.categories, name=target)
        if not category:
            await ctx.send("❌ Category not found.")
            return

        if action.lower() == "add":
            blacklisted_categories.add(category.id)
            add_to_blacklist(category_id=category.id)
            await ctx.send(f"✅ Category `{category.name}` blacklisted.")
        elif action.lower() == "remove":
            blacklisted_categories.discard(category.id)
            remove_from_blacklist(category_id=category.id)
            await ctx.send(f"✅ Category `{category.name}` removed.")
        return

    # CHANNEL
    if mode.lower() == "channel":
        if action is None:
            await ctx.send("Usage: `*blacklist channel add/remove/list #channel`")
            return

        if action.lower() == "list":
            if not blacklisted_channels:
                await ctx.send("No channels blacklisted.")
                return
            items = [f"<#{cid}>" for cid in blacklisted_channels]
            await ctx.send("📜 Blacklisted Channels:\n" + "\n".join(items))
            return

        channel = ctx.message.channel_mentions[0] if ctx.message.channel_mentions else None

        if channel is None:
            await ctx.send("❌ You must mention a channel.")
            return

        if action.lower() == "add":
            blacklisted_channels.add(channel.id)
            add_to_blacklist(channel_id=channel.id)
            await ctx.send(f"✅ Channel {channel.mention} blacklisted.")
        elif action.lower() == "remove":
            blacklisted_channels.discard(channel.id)
            remove_from_blacklist(channel_id=channel.id)
            await ctx.send(f"✅ Channel {channel.mention} removed.")
        return

    await ctx.send("❌ Invalid mode. Use `channel` or `category`.")

# ===== OTHER COMMANDS =====
@bot.command()
@is_admin()
async def setlog(ctx, channel: discord.TextChannel):
    set_log_channel(channel.id)
    await ctx.send(f"Log channel set to {channel.mention}")

@bot.command()
@is_admin()
async def togglekeywords(ctx):
    global keywords_enabled
    keywords_enabled = not keywords_enabled
    await ctx.send(f"Keyword locking is now **{'enabled' if keywords_enabled else 'disabled'}**")

@bot.command()
@is_admin()
async def purge(ctx, amount: int):
    deleted = await ctx.channel.purge(limit=amount)
    await ctx.send(f"Purged {len(deleted)} messages.", delete_after=5)

@bot.command()
@is_admin()
async def lock(ctx, channel: discord.TextChannel):
    await lock_channel(channel)

@bot.command()
@is_admin()
async def unlock(ctx, channel: discord.TextChannel):
    await unlock_channel(channel, ctx.author)

@bot.command()
@is_owner()
async def botstatus(ctx):
    embed = discord.Embed(title="Bot Status", color=discord.Color.blue())
    embed.add_field(name="Servers", value=len(bot.guilds))
    embed.add_field(name="Server List", value="\n".join([g.name for g in bot.guilds]), inline=False)
    await ctx.send(embed=embed)

@bot.command()
async def help(ctx):
    embed = discord.Embed(title="Bot Commands", color=discord.Color.green())
    embed.add_field(
        name="Blacklist",
        value="`*blacklist channel add #channel`\n"
              "`*blacklist channel remove #channel`\n"
              "`*blacklist channel list`\n"
              "`*blacklist category add <name>`\n"
              "`*blacklist category remove <name>`\n"
              "`*blacklist category list`",
        inline=False,
    )
    embed.add_field(
        name="Admin",
        value="`*setlog #channel`\n"
              "`*togglekeywords`\n"
              "`*lock #channel`\n"
              "`*unlock #channel`\n"
              "`*purge <amount>`",
        inline=False,
    )
    embed.add_field(name="Owner", value="`*botstatus`", inline=False)
    embed.set_footer(text="Bot made by Buddy ❤️")
    await ctx.send(embed=embed)

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
