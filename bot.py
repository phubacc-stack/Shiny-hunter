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

bot = commands.Bot(command_prefix=".", intents=intents)
bot.remove_command("help")  # Remove default help

# ====== Lock Settings ======
lock_duration = 12  # Default in hours
KEYWORDS = {
    "shiny hunt pings": True,
    "collection pings": True,
    "rare ping": True,
}
blacklisted_channels = set()
lock_timers = {}

# ====== Database ======
def get_db_connection():
    conn = sqlite3.connect('bot_database.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute('CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY, name TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS blacklisted_channels (id INTEGER PRIMARY KEY, channel_id INTEGER)')
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

init_db()

# ====== Unlock Button ======
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
                "You don't have the 'unlock' role to unlock this channel. Use `.unlock` instead.",
                ephemeral=True,
            )

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

async def lock_channel(channel, hours=None):
    """Lock a channel for a given number of hours (default = lock_duration)."""
    await set_channel_permissions(channel, view_channel=False, send_messages=False)
    end_time = datetime.now() + timedelta(hours=hours if hours else lock_duration)
    lock_timers[channel.id] = end_time

async def unlock_channel(channel, user):
    await set_channel_permissions(channel, view_channel=None, send_messages=None)
    lock_timers.pop(channel.id, None)
    embed = discord.Embed(
        title="🔓 Channel Unlocked",
        description=f"The channel has been unlocked by {user.mention} (or automatically after timer expired).",
        color=discord.Color.green(),
        timestamp=datetime.now(),
    )
    embed.set_footer(text="Happy hunting! Let's see some unusual colors... ✨")
    await channel.send(embed=embed)

def is_shiny_message(message: discord.Message):
    """Detect shiny messages from content or embeds."""
    content = message.content.lower()
    if "✨" in content or "shiny" in content:
        return True
    for embed in message.embeds:
        if embed.title and ("✨" in embed.title.lower() or "shiny" in embed.title.lower()):
            return True
        if embed.description and ("✨" in embed.description.lower() or "shiny" in embed.description.lower()):
            return True
    return False

# ====== Bot Events ======
@bot.event
async def on_ready():
    logging.info(f"Bot is online as {bot.user}")
    global blacklisted_channels
    blacklisted_channels = load_blacklisted_channels()
    if not check_lock_timers.is_running():
        check_lock_timers.start()

@bot.event
async def on_message(message):
    try:
        if message.author == bot.user:
            return

        # Auto-lock shiny messages
        if message.author.bot and is_shiny_message(message):
            if message.channel.id not in blacklisted_channels:
                await lock_channel(message.channel)
                embed = discord.Embed(
                    title="🔒 Channel Locked",
                    description=f"This channel has been locked for {lock_duration} hours due to a shiny alert.",
                    color=discord.Color.red(),
                    timestamp=datetime.now(),
                )
                embed.add_field(
                    name="Lock Timer Ends At",
                    value=(datetime.now() + timedelta(hours=lock_duration)).strftime("%Y-%m-%d %H:%M:%S"),
                    inline=False
                )
                embed.set_footer(text="Use the unlock button or `.unlock` to restore access.")
                view = UnlockView(channel=message.channel)
                await message.channel.send(embed=embed, view=view)

        await bot.process_commands(message)
    except Exception as e:
        logging.error(f"Error in on_message: {e}")

# ====== Lock Timer Task ======
@tasks.loop(seconds=60)
async def check_lock_timers():
    now = datetime.now()
    expired_channels = [cid for cid, end_time in lock_timers.items() if now >= end_time]
    for cid in expired_channels:
        channel = bot.get_channel(cid)
        if channel:
            await unlock_channel(channel, bot.user)
            logging.info(f"Channel {channel.name} automatically unlocked.")
        lock_timers.pop(cid, None)

# ====== Manual Lock/Unlock Commands ======
@bot.command()
@commands.has_role("unlock")
async def lock(ctx, hours: int = None):
    """Manually lock the current channel for a given number of hours (defaults to lock_duration)."""
    await lock_channel(ctx.channel, hours)
    await ctx.send(f"🔒 Channel locked manually for {hours if hours else lock_duration} hours.")

@bot.command()
@commands.has_role("unlock")
async def unlock(ctx):
    """Manually unlock the current channel."""
    await unlock_channel(ctx.channel, ctx.author)

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
