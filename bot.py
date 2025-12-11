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
LOCK_DURATION = 12  # hours
KEYWORDS = ["rare ping", "collection pings", "shiny hunt pings"]
keywords_enabled = True
lock_timers = {}
last_shiny_catch = {}

# ===== Database =====
DB_FILE = "bot_database.db"

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.execute("CREATE TABLE IF NOT EXISTS blacklisted_channels (channel_id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS blacklisted_categories (category_id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS config (log_channel_id INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS locks (channel_id INTEGER PRIMARY KEY, unlock_time TEXT)")
    conn.commit()
    conn.close()

def load_blacklists():
    conn = get_db()
    channels = {row["channel_id"] for row in conn.execute("SELECT channel_id FROM blacklisted_channels")}
    categories = {row["category_id"] for row in conn.execute("SELECT category_id FROM blacklisted_categories")}
    conn.close()
    return channels, categories

def add_to_blacklist(channel_id=None, category_id=None):
    conn = get_db()
    if channel_id:
        conn.execute("INSERT OR IGNORE INTO blacklisted_channels VALUES (?)", (channel_id,))
    if category_id:
        conn.execute("INSERT OR IGNORE INTO blacklisted_categories VALUES (?)", (category_id,))
    conn.commit()
    conn.close()

def remove_from_blacklist(channel_id=None, category_id=None):
    conn = get_db()
    if channel_id:
        conn.execute("DELETE FROM blacklisted_channels WHERE channel_id=?", (channel_id,))
    if category_id:
        conn.execute("DELETE FROM blacklisted_categories WHERE category_id=?", (category_id,))
    conn.commit()
    conn.close()

def set_log_channel(channel_id):
    conn = get_db()
    conn.execute("DELETE FROM config")
    conn.execute("INSERT INTO config (log_channel_id) VALUES (?)", (channel_id,))
    conn.commit()
    conn.close()

def get_log_channel():
    conn = get_db()
    row = conn.execute("SELECT log_channel_id FROM config").fetchone()
    conn.close()
    return row["log_channel_id"] if row else None

def save_lock(channel_id, unlock_time):
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO locks VALUES (?, ?)", (channel_id, unlock_time.isoformat()))
    conn.commit()
    conn.close()

def remove_lock(channel_id):
    conn = get_db()
    conn.execute("DELETE FROM locks WHERE channel_id=?", (channel_id,))
    conn.commit()
    conn.close()

def load_locks():
    conn = get_db()
    rows = conn.execute("SELECT channel_id, unlock_time FROM locks").fetchall()
    conn.close()
    result = {}
    for row in rows:
        result[row["channel_id"]] = datetime.fromisoformat(row["unlock_time"])
    return result

init_db()
blacklisted_channels, blacklisted_categories = load_blacklists()
lock_timers = load_locks()

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

# ===== Helper Functions =====
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

def contains_keyword(message):
    if not keywords_enabled:
        return False
    content = message.content.lower()
    return any(k in content for k in KEYWORDS)

# ===== Lock/Unlock =====
class UnlockView(View):
    def __init__(self, channel):
        super().__init__(timeout=None)
        self.channel = channel

    @discord.ui.button(label="Unlock Channel", style=discord.ButtonStyle.green)
    async def unlock_button(self, interaction, button):
        await unlock_channel(self.channel, interaction.user)
        await interaction.response.send_message("Channel unlocked!", ephemeral=True)
        self.stop()

async def lock_channel(channel):
    await set_channel_permissions(channel, view_channel=False, send_messages=False)
    unlock_time = datetime.now() + timedelta(hours=LOCK_DURATION)
    lock_timers[channel.id] = unlock_time
    save_lock(channel.id, unlock_time)
    embed = discord.Embed(
        title="🔒 Channel Locked",
        description=f"Locked for **{LOCK_DURATION} hours**.",
        color=discord.Color.red()
    )
    await channel.send(embed=embed, view=UnlockView(channel))

async def unlock_channel(channel, user):
    await set_channel_permissions(channel, view_channel=None, send_messages=None)
    lock_timers.pop(channel.id, None)
    remove_lock(channel.id)
    embed = discord.Embed(
        title="🔓 Channel Unlocked",
        description=f"Unlocked by {user.mention}",
        color=discord.Color.green()
    )
    await channel.send(embed=embed)

# ===== Events =====
@bot.event
async def on_ready():
    global blacklisted_channels, blacklisted_categories
    blacklisted_channels, blacklisted_categories = load_blacklists()
    if not lock_timer_task.is_running():
        lock_timer_task.start()
    logging.info(f"Bot online as {bot.user}")

@bot.event
async def on_message(message):
    if message.author.bot and contains_keyword(message):
        if message.channel.id in blacklisted_channels:
            return
        if message.channel.category_id in blacklisted_categories:
            return
        await lock_channel(message.channel)

    # Shiny detection
    if message.author.id == POKETWO_ID:
        if "these colors seem unusual" in message.content.lower():
            log_channel_id = get_log_channel()
            if log_channel_id and message.channel.id not in last_shiny_catch:
                log_channel = bot.get_channel(log_channel_id)
                if log_channel:
                    embed = discord.Embed(
                        title="🌈 Shiny Detected!",
                        description=f"Pokétwo says: **These colors seem unusual...✨**",
                        color=discord.Color.purple()
                    )
                    embed.add_field(name="Auto Catch", value="`*catch` sent!", inline=False)
                    await log_channel.send(embed=embed)
                    await log_channel.send("*catch")
                    last_shiny_catch[message.channel.id] = datetime.now()
    await bot.process_commands(message)

# ===== Tasks =====
@tasks.loop(seconds=60)
async def lock_timer_task():
    now = datetime.now()
    expired = [cid for cid, t in lock_timers.items() if now >= t]
    for cid in expired:
        channel = bot.get_channel(cid)
        if channel:
            await unlock_channel(channel, bot.user)
        lock_timers.pop(cid, None)

# ===== Admin Menu Enhancements =====
class RoleSelect(Select):
    def __init__(self, roles):
        options = [discord.SelectOption(label=role.name, value=str(role.id)) for role in roles]
        super().__init__(placeholder="Select a role to assign/remove...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction):
        role_id = int(self.values[0])
        role = discord.utils.get(interaction.guild.roles, id=role_id)
        member = interaction.user
        if role in member.roles:
            await member.remove_roles(role)
            await interaction.response.send_message(f"Removed role `{role.name}` from you.", ephemeral=True)
        else:
            await member.add_roles(role)
            await interaction.response.send_message(f"Assigned role `{role.name}` to you.", ephemeral=True)

class AdminMenu(View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        # Role management
        roles = [r for r in ctx.guild.roles if r.name != "@everyone"]
        if roles:
            self.add_item(RoleSelect(roles))
        # Locked channels
        for cid in lock_timers:
            channel = ctx.guild.get_channel(cid)
            if channel:
                self.add_item(UnlockView(channel))
        # Blacklisted channels buttons
        for cid in blacklisted_channels:
            ch = ctx.guild.get_channel(cid)
            if ch:
                self.add_item(Button(label=f"Unblacklist {ch.name}", style=discord.ButtonStyle.red, custom_id=f"unblacklist_channel_{cid}"))
        # Blacklisted categories buttons
        for cid in blacklisted_categories:
            cat = discord.utils.get(ctx.guild.categories, id=cid)
            if cat:
                self.add_item(Button(label=f"Unblacklist {cat.name}", style=discord.ButtonStyle.red, custom_id=f"unblacklist_category_{cid}"))

# ===== Commands =====
@bot.command()
@is_admin()
async def admin(ctx):
    embed = discord.Embed(title="🛠 Admin Menu", color=discord.Color.purple())
    embed.add_field(name="🔒 Locked Channels", value="\n".join(f"<#{cid}>" for cid in lock_timers) or "None", inline=False)
    embed.add_field(name="🚫 Blacklisted Channels", value="\n".join(f"<#{cid}>" for cid in blacklisted_channels) or "None", inline=False)
    embed.add_field(name="🚫 Blacklisted Categories", value="\n".join(f"{discord.utils.get(ctx.guild.categories, id=cid).name}" for cid in blacklisted_categories if discord.utils.get(ctx.guild.categories, id=cid)) or "None", inline=False)
    embed.set_footer(text="Admin Panel — Buddy's Bot")
    await ctx.send(embed=embed, view=AdminMenu(ctx))

# ===== Other Commands =====
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
        value="`*blacklist channel add/remove/list #channel`\n"
              "`*blacklist category add/remove/list <name>`",
        inline=False,
    )
    embed.add_field(
        name="Admin",
        value="`*setlog #channel`\n"
              "`*togglekeywords`\n"
              "`*lock #channel`\n"
              "`*unlock #channel`\n"
              "`*purge <amount>`\n"
              "`*admin`",
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
