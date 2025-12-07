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
lock_timers = {}

# ====== Database ======
def get_db_connection():
    conn = sqlite3.connect('bot_database.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS blacklisted_channels (id INTEGER PRIMARY KEY, channel_id INTEGER)')
    conn.execute('CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY, log_channel_id INTEGER)')
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
    cursor = conn.execute('SELECT channel_id FROM blacklisted_channels')
    rows = cursor.fetchall()
    conn.close()
    return {row['channel_id'] for row in rows}

def set_log_channel_db(channel_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM config')
    conn.execute('INSERT INTO config (log_channel_id) VALUES (?)', (channel_id,))
    conn.commit()
    conn.close()

def get_log_channel_db():
    conn = get_db_connection()
    cursor = conn.execute('SELECT log_channel_id FROM config')
    row = cursor.fetchone()
    conn.close()
    return row['log_channel_id'] if row else None

init_db()
blacklisted_channels = load_blacklisted_channels()

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
        unlock_role = discord.utils.get(interaction.guild.roles, name="unlock")
        if unlock_role in interaction.user.roles:
            await unlock_channel(self.channel, interaction.user)
            await interaction.response.send_message("Channel unlocked!", ephemeral=True)
            self.stop()
        else:
            await interaction.response.send_message(
                "You don't have the 'unlock' role. Use `*unlock` instead.",
                ephemeral=True,
            )

# ====== Admin Menu View ======
class AdminMenu(View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx

    @discord.ui.button(label="Blacklist Channel", style=discord.ButtonStyle.red)
    async def blacklist_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "Use `*blacklist add #channel` or `*blacklist remove #channel`.", ephemeral=True
        )

    @discord.ui.button(label="Set Log Channel", style=discord.ButtonStyle.blurple)
    async def log_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "Use `*setlog #channel` to set the log channel.", ephemeral=True
        )

    @discord.ui.button(label="Manage Roles", style=discord.ButtonStyle.green)
    async def roles_button(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "Use `*giverole @user <role>` or `*removerole @user <role>`.", ephemeral=True
        )

    @discord.ui.button(label="Locked Channels", style=discord.ButtonStyle.gray)
    async def locked_button(self, interaction: discord.Interaction, button: Button):
        locked = [f"<#{cid}>" for cid in lock_timers.keys()]
        if locked:
            await interaction.response.send_message(
                "🔒 Locked Channels:\n" + "\n".join(locked), ephemeral=True
            )
        else:
            await interaction.response.send_message("No channels are currently locked.", ephemeral=True)

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
    await set_channel_permissions(channel, view_channel=False, send_messages=False)
    end_time = datetime.now() + timedelta(hours=lock_duration)
    lock_timers[channel.id] = end_time
    unlock_role = discord.utils.get(channel.guild.roles, name="unlock")
    if unlock_role:
        for member in channel.guild.members:
            if unlock_role not in member.roles:
                await member.add_roles(unlock_role)
    embed = discord.Embed(
        title="🔒 Channel Locked",
        description=f"Channel locked for {lock_duration} hours.",
        color=discord.Color.red(),
        timestamp=datetime.now()
    )
    await channel.send(embed=embed, view=UnlockView(channel))

async def unlock_channel(channel, user):
    await set_channel_permissions(channel, view_channel=None, send_messages=None)
    lock_timers.pop(channel.id, None)
    unlock_role = discord.utils.get(channel.guild.roles, name="unlock")
    if unlock_role:
        for member in channel.guild.members:
            if unlock_role in member.roles:
                await member.remove_roles(unlock_role)
    embed = discord.Embed(
        title="🔓 Channel Unlocked",
        description=f"Channel unlocked by {user.mention}!",
        color=discord.Color.green(),
        timestamp=datetime.now(),
    )
    await channel.send(embed=embed)

def contains_keyword(message):
    content = message.content.lower()
    return any(keyword in content for keyword in KEYWORDS)

# ====== Bot Events ======
@bot.event
async def on_ready():
    logging.info(f"Bot online as {bot.user}")
    global blacklisted_channels
    blacklisted_channels = load_blacklisted_channels()
    if not check_lock_timers.is_running():
        check_lock_timers.start()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Keyword detection
    if message.author.bot and contains_keyword(message) and message.channel.id not in blacklisted_channels:
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
            await unlock_channel(channel, bot.user)
        lock_timers.pop(cid, None)

# ====== Admin Commands ======
@bot.command()
@is_admin()
async def blacklist(ctx, action, channel: discord.TextChannel = None):
    global blacklisted_channels
    if action.lower() == "add":
        if channel:
            add_to_blacklist_db(channel.id)
            blacklisted_channels.add(channel.id)
            await ctx.send(f"✅ Channel {channel.mention} added to blacklist.")
        else:
            await ctx.send("❌ Please specify a channel to add.")
    elif action.lower() == "remove":
        if channel:
            remove_from_blacklist_db(channel.id)
            blacklisted_channels.discard(channel.id)
            await ctx.send(f"✅ Channel {channel.mention} removed from blacklist.")
        else:
            await ctx.send("❌ Please specify a channel to remove.")
    elif action.lower() == "list":
        if blacklisted_channels:
            channels = [bot.get_channel(cid).mention for cid in blacklisted_channels if bot.get_channel(cid)]
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
async def giverole(ctx, member: discord.Member, *, role_name):
    role = discord.utils.get(ctx.guild.roles, name=role_name)
    if not role:
        await ctx.send(f"❌ Role `{role_name}` not found.")
        return
    await member.add_roles(role)
    await ctx.send(f"✅ {member.mention} has been given the `{role.name}` role.")

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
    """Show the interactive admin menu"""
    view = AdminMenu(ctx)
    await ctx.send("Admin Menu - use buttons for options", view=view)

# ====== Owner Command ======
@bot.command()
async def owner(ctx):
    """Shows the owner credit message."""
    await ctx.send("Bot made by Buddy — happy hunting yall freaks ❤️")

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
