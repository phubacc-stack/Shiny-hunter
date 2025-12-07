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
OWNER_ID = int(os.getenv("OWNER_ID"))  # Set smokingpikachu420 user ID here
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in the environment variables.")

POKETWO_ID = 716390085896962058  # Pokétwo User ID

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
blacklisted_channels = set()
blacklisted_categories = set()
lock_timers = {}
lock_enabled = True  # toggle for auto-lock

# ====== Database ======
def get_db_connection():
    conn = sqlite3.connect('bot_database.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS blacklisted_channels (id INTEGER PRIMARY KEY, channel_id INTEGER)')
    conn.execute('CREATE TABLE IF NOT EXISTS blacklisted_categories (id INTEGER PRIMARY KEY, category_id INTEGER)')
    conn.execute('CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, log_channel_id INTEGER, lock_enabled INTEGER)')
    conn.close()

def add_to_blacklist_db(channel_id=None, category_id=None):
    conn = get_db_connection()
    if channel_id:
        conn.execute('INSERT OR IGNORE INTO blacklisted_channels (channel_id) VALUES (?)', (channel_id,))
    if category_id:
        conn.execute('INSERT OR IGNORE INTO blacklisted_categories (category_id) VALUES (?)', (category_id,))
    conn.commit()
    conn.close()

def remove_from_blacklist_db(channel_id=None, category_id=None):
    conn = get_db_connection()
    if channel_id:
        conn.execute('DELETE FROM blacklisted_channels WHERE channel_id = ?', (channel_id,))
    if category_id:
        conn.execute('DELETE FROM blacklisted_categories WHERE category_id = ?', (category_id,))
    conn.commit()
    conn.close()

def load_blacklists():
    conn = get_db_connection()
    c_rows = conn.execute('SELECT channel_id FROM blacklisted_channels').fetchall()
    cat_rows = conn.execute('SELECT category_id FROM blacklisted_categories').fetchall()
    conn.close()
    return {r['channel_id'] for r in c_rows}, {r['category_id'] for r in cat_rows}

def set_log_channel_db(channel_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM config')
    conn.execute('INSERT INTO config (log_channel_id, lock_enabled) VALUES (?, ?)', (channel_id, int(lock_enabled)))
    conn.commit()
    conn.close()

def get_log_channel_db():
    conn = get_db_connection()
    row = conn.execute('SELECT log_channel_id FROM config').fetchone()
    conn.close()
    return row['log_channel_id'] if row else None

def save_lock_toggle_db():
    conn = get_db_connection()
    log_id = get_log_channel_db() or None
    conn.execute('DELETE FROM config')
    conn.execute('INSERT INTO config (log_channel_id, lock_enabled) VALUES (?, ?)', (log_id, int(lock_enabled)))
    conn.commit()
    conn.close()

init_db()
blacklisted_channels, blacklisted_categories = load_blacklists()

# ====== Admin Check ======
def is_admin():
    async def predicate(ctx):
        role = discord.utils.get(ctx.author.roles, name="Admin")
        if role:
            return True
        await ctx.send("❌ You must have the **Admin** role to use this command.")
        return False
    return commands.check(predicate)

# ====== Unlock View ======
class UnlockView(View):
    def __init__(self, channel):
        super().__init__(timeout=None)
        self.channel = channel

    @discord.ui.button(label="Unlock Channel", style=discord.ButtonStyle.green)
    async def unlock_button(self, interaction: discord.Interaction, button: Button):
        await unlock_channel(self.channel, interaction.user)
        await interaction.response.send_message("Channel unlocked!", ephemeral=True)
        self.stop()

# ====== Admin Menu View ======
class AdminMenu(View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx

    @discord.ui.button(label="Blacklist Channel", style=discord.ButtonStyle.red)
    async def blacklist_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("Use `*blacklist add #channel` or `*blacklist remove #channel`", ephemeral=True)

    @discord.ui.button(label="Blacklist Category", style=discord.ButtonStyle.gray)
    async def blacklist_cat_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("Use `*blacklist addcategory <category>` or `*blacklist removecategory <category>`", ephemeral=True)

    @discord.ui.button(label="Set Log Channel", style=discord.ButtonStyle.blurple)
    async def log_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("Use `*setlog #channel` to set the log channel.", ephemeral=True)

    @discord.ui.button(label="Toggle Lock", style=discord.ButtonStyle.green)
    async def toggle_button(self, interaction: discord.Interaction, button: Button):
        global lock_enabled
        lock_enabled = not lock_enabled
        save_lock_toggle_db()
        await interaction.response.send_message(f"🔁 Auto-lock toggled to {lock_enabled}", ephemeral=True)

    @discord.ui.button(label="Locked Channels", style=discord.ButtonStyle.secondary)
    async def locked_button(self, interaction: discord.Interaction, button: Button):
        locked = [f"<#{cid}>" for cid in lock_timers.keys()]
        await interaction.response.send_message("🔒 Locked Channels:\n" + ("\n".join(locked) if locked else "None"), ephemeral=True)

# ====== Helper Functions ======
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
    if not lock_enabled:
        return
    await set_channel_permissions(channel, view_channel=False, send_messages=False)
    end_time = datetime.now() + timedelta(hours=lock_duration)
    lock_timers[channel.id] = end_time
    embed = discord.Embed(title="🔒 Channel Locked", description=f"Locked for {lock_duration} hours", color=discord.Color.red(), timestamp=datetime.now())
    await channel.send(embed=embed, view=UnlockView(channel))

async def unlock_channel(channel, user=None):
    await set_channel_permissions(channel, view_channel=True, send_messages=True)
    lock_timers.pop(channel.id, None)
    msg = f"Channel unlocked by {user.mention}" if user else "Channel unlocked automatically"
    embed = discord.Embed(title="🔓 Channel Unlocked", description=msg, color=discord.Color.green(), timestamp=datetime.now())
    await channel.send(embed=embed)

def contains_keyword(message):
    content = message.content.lower()
    return any(keyword in content for keyword in KEYWORDS)

# ====== Bot Events ======
@bot.event
async def on_ready():
    logging.info(f"Bot online as {bot.user}")
    global blacklisted_channels, blacklisted_categories
    blacklisted_channels, blacklisted_categories = load_blacklists()
    if not check_lock_timers.is_running():
        check_lock_timers.start()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    # auto-lock keywords
    if lock_enabled and message.author.bot and contains_keyword(message):
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
        lock_timers.pop(cid, None)

# ====== Admin Commands ======
@bot.command()
@is_admin()
async def lock(ctx, channel: discord.TextChannel = None):
    if not channel:
        await ctx.send("❌ Please specify a channel to lock, e.g. `*lock #general`")
        return
    await lock_channel(channel)

@bot.command()
@is_admin()
async def unlock(ctx, channel: discord.TextChannel = None):
    if not channel:
        await ctx.send("❌ Please specify a channel to unlock, e.g. `*unlock #general`")
        return
    await unlock_channel(channel, ctx.author)

@bot.command()
@is_admin()
async def blacklist(ctx, action=None, target=None):
    if not action:
        await ctx.send("❌ Please specify an action: add/remove/list/addcategory/removecategory")
        return
    global blacklisted_channels, blacklisted_categories
    if action.lower() == "add" and target:
        channel = await commands.TextChannelConverter().convert(ctx, target)
        add_to_blacklist_db(channel_id=channel.id)
        blacklisted_channels.add(channel.id)
        await ctx.send(f"✅ {channel.mention} added to blacklist.")
    elif action.lower() == "remove" and target:
        channel = await commands.TextChannelConverter().convert(ctx, target)
        remove_from_blacklist_db(channel_id=channel.id)
        blacklisted_channels.discard(channel.id)
        await ctx.send(f"✅ {channel.mention} removed from blacklist.")
    elif action.lower() == "list":
        msg = "📜 Blacklisted Channels:\n"
        msg += "\n".join([f"<#{cid}>" for cid in blacklisted_channels]) or "None"
        msg += "\n📜 Blacklisted Categories:\n"
        msg += "\n".join([f"{cid}" for cid in blacklisted_categories]) or "None"
        await ctx.send(msg)
    elif action.lower() == "addcategory" and target:
        category = discord.utils.get(ctx.guild.categories, name=target)
        if category:
            add_to_blacklist_db(category_id=category.id)
            blacklisted_categories.add(category.id)
            await ctx.send(f"✅ Category `{category.name}` added to blacklist.")
        else:
            await ctx.send("❌ Category not found.")
    elif action.lower() == "removecategory" and target:
        category = discord.utils.get(ctx.guild.categories, name=target)
        if category:
            remove_from_blacklist_db(category_id=category.id)
            blacklisted_categories.discard(category.id)
            await ctx.send(f"✅ Category `{category.name}` removed from blacklist.")
        else:
            await ctx.send("❌ Category not found.")
    else:
        await ctx.send("❌ Invalid action or missing target.")

@bot.command()
@is_admin()
async def setlog(ctx, channel: discord.TextChannel):
    set_log_channel_db(channel.id)
    await ctx.send(f"✅ Log channel set to {channel.mention}")

@bot.command()
@is_admin()
async def giverole(ctx, member: discord.Member, *, role_name):
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"❌ Role `{role_name}` not found.")
        return
    await member.add_roles(role)
    await ctx.send(f"✅ {member.mention} given role `{role.name}`")

@bot.command()
@is_admin()
async def removerole(ctx, member: discord.Member, *, role_name):
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"❌ Role `{role_name}` not found.")
        return
    await member.remove_roles(role)
    await ctx.send(f"✅ `{role.name}` removed from {member.mention}")

@bot.command()
@is_admin()
async def listroles(ctx):
    roles = [role.name for role in ctx.guild.roles if role.name != "@everyone"]
    await ctx.send("📜 Roles in server:\n" + "\n".join(roles))

@bot.command()
@is_admin()
async def menu(ctx):
    view = AdminMenu(ctx)
    await ctx.send("Admin Menu - use buttons for options", view=view)

# ====== Owner Command ======
@bot.command()
async def owner(ctx):
    await ctx.send("Bot made by Buddy — happy hunting yall freaks ❤️")

# ====== Secret Owner Status ======
@bot.command()
async def secret(ctx):
    if ctx.author.id != OWNER_ID:
        return
    guilds = bot.guilds
    msg = f"Bot in {len(guilds)} servers:\n"
    msg += "\n".join([f"{g.name} ({g.id})" for g in guilds])
    await ctx.send(msg)

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
