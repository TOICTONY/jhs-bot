import os
import re
import json
import time
import asyncio
from pathlib import Path
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ============================================================================
# CONFIGURATION & CREDENTIALS (Set these in your VPS/Heroku Environment Variables)
# ============================================================================
API_ID = os.environ.get("API_ID", "YOUR_API_ID_HERE")
API_HASH = os.environ.get("API_HASH", "YOUR_API_HASH_HERE")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# SECURITY: Replace with your actual Telegram User ID (Get it from @userinfobot)
# If using Heroku, add OWNER_ID to your Config Vars.
OWNER_ID = int(os.environ.get("OWNER_ID", "123456789")) 

OUTPUT_DIR = Path("downloads")
OUTPUT_DIR.mkdir(exist_ok=True)

# THE MOST IMPORTANT SERVER CHANGE: We must use a cookies.txt file
COOKIES_FILE = "cookies.txt"

app = Client("hotstar_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Temporary memory to store user choices
user_states = {}

# Smart language map
CODE_MAP = {
    'hi': 'hin', 'ta': 'tam', 'te': 'tel', 'ml': 'mal', 'kn': 'kan',
    'bn': 'ben', 'mr': 'mar', 'pa': 'pan', 'gu': 'guj', 'en': 'eng'
}

# ============================================================================
# CORE PRO LOGIC
# ============================================================================

def safe_filename(s: str) -> str:
    for c in r'<>:"/\|?*': s = s.replace(c, '')
    return re.sub(r'\s+', ' ', s).strip()[:100]

def build_scene_name(title, is_movie=False, season=0, episode=0, ep_title="", quality=None, acodec=None):
    name = safe_filename(title).replace(' ', '.')
    if not is_movie: name += f".S{season:02d}E{episode:02d}"
    res = quality.split(',')[0].replace('p', '') + "p" if quality else "MAX-RES"
    name += f".{res}"
    if not is_movie and ep_title:
        name += f".{safe_filename(ep_title).replace(' ', '.')}"
        
    name += ".AVC.Multi-Audio" # Defaulting to AVC/Multi for Bot simplicity
    
    if acodec:
        ac_lower = acodec.lower()
        if 'ddp' in ac_lower: name += ".DDP5.1"
        else: name += ".AAC2.0"
    else:
        name += ".AAC2.0" 
        
    name += ".Esub"
    return name

def build_ytdlp_format(quality, acodec):
    v_format = f"bestvideo[height<={quality}]" if quality else "bestvideo"
    v_format += "[vcodec~='^avc|^h264']" # Force AVC for best Telegram playback
    
    ytdlp_args = [
        "--embed-metadata",
        "--write-subs", "--sub-langs", "en,eng", "--convert-subs", "srt", "--embed-subs",
        "--compat-options", "no-keep-subs"
    ]
    
    acodec_filter = "[acodec~='^ec-3|^eac3']" if acodec == 'ddp' else "[acodec~='^mp4a|^aac']"
    
    # Target all languages with the specific codec, fallback to any codec
    langs = ['hin', 'tam', 'tel', 'mal', 'kan']
    a_formats = []
    for lang in langs:
        b_lang = f"bestaudio[language={lang}]"
        a_formats.append(f"({b_lang}{acodec_filter}/{b_lang})")
            
    a_chain = "+".join(a_formats)
    format_str = f"{v_format}+{a_chain}/{v_format}+bestaudio/best"
    ytdlp_args.append("--audio-multistreams")
        
    return format_str, ytdlp_args

def fetch_metadata(url: str):
    cmd = ["yt-dlp", "--cookies", COOKIES_FILE, "--flat-playlist", "-J", url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode == 0: return json.loads(r.stdout)
    except: pass
    return {}

def parse_series(data, url):
    entries = data.get("entries", [])
    episodes = []
    for i, e in enumerate(entries):
        ep_url = e.get("url", "")
        if not ep_url.startswith('http'): ep_url = f"https://www.hotstar.com{ep_url}"
        episodes.append({
            "title": safe_filename(e.get("title", f"Episode {i+1}")),
            "url": ep_url,
            "season": e.get("season_number") or 1,
            "episode": e.get("episode_number") or (i + 1),
        })
    
    title = data.get("playlist_title") or data.get("title") or ""
    if not title or title.lower() == "unknown":
        m = re.search(r'/(?:shows|movies)/([^/]+)/', url)
        title = safe_filename(m.group(1).replace('-', ' ').title()) if m else "Series"
    return title, episodes

# ============================================================================
# TELEGRAM ASYNC DOWNLOADER & UPLOADER
# ============================================================================

async def progress_for_pyrogram(current, total, message, text):
    """Updates Telegram message with upload progress without hitting API limits."""
    now = time.time()
    if not hasattr(progress_for_pyrogram, "last_update"):
        progress_for_pyrogram.last_update = 0
    if now - progress_for_pyrogram.last_update > 3 or current == total:
        try:
            percent = round(current * 100 / total, 1)
            await message.edit_text(f"{text}\n**Progress:** {percent}%")
            progress_for_pyrogram.last_update = now
        except: pass

async def process_download(client, message, url, is_movie, metadata=None):
    status_msg = await message.reply_text("⏳ **Initializing Download Engine...**")
    
    if not os.path.exists(COOKIES_FILE):
        await status_msg.edit_text("❌ **ERROR:** `cookies.txt` not found on server!\nPlease upload your Hotstar cookies to the server root.")
        return

    user_id = message.chat.id
    quality = user_states.get(user_id, {}).get("quality", "720")
    acodec = user_states.get(user_id, {}).get("acodec", "aac")
    
    title = metadata.get("title", "Movie") if is_movie else user_states[user_id]["series_title"]
    season = metadata.get("season", 1) if not is_movie else 0
    episode = metadata.get("episode", 1) if not is_movie else 0
    ep_title = metadata.get("title", "") if not is_movie else ""
    
    scene_name = build_scene_name(title, is_movie, season, episode, ep_title, quality, acodec)
    output_path = OUTPUT_DIR / f"{scene_name}.%(ext)s"
    final_file = OUTPUT_DIR / f"{scene_name}.mkv"
    
    custom_format, extra_args = build_ytdlp_format(quality, acodec)
    
    cmd = [
        "yt-dlp", "--cookies", COOKIES_FILE, "-f", custom_format,
        "--merge-output-format", "mkv", "-o", str(output_path),
        "--no-part", "--write-thumbnail", "--convert-thumbnails", "jpg"
    ]
    cmd.extend(extra_args)
    cmd.append(url)

    await status_msg.edit_text(f"📥 **Downloading:** `{scene_name}.mkv`\n*(Please wait, audio merging takes time)*")

    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await process.communicate()
    
    if final_file.exists():
        await status_msg.edit_text("📤 **Upload in progress to Telegram...**")
        
        try:
            await client.send_document(
                chat_id=message.chat.id,
                document=str(final_file),
                caption=f"🎬 **{scene_name}**\n\n🛡 Downloaded via Hotstar PRO",
                progress=progress_for_pyrogram,
                progress_args=(status_msg, "📤 **Uploading to Telegram...**")
            )
            await status_msg.delete()
        except Exception as e:
            await status_msg.edit_text(f"❌ **Upload Failed:** {str(e)}\n*(File might be larger than Telegram's 2GB limit)*")
        finally:
            os.remove(final_file)
    else:
        await status_msg.edit_text("❌ **Download Failed.**\n*This is likely due to Widevine DRM (Premium Content) or expired cookies.*")


# ============================================================================
# BOT INTERFACE & COMMANDS
# ============================================================================

@app.on_message(filters.command("start") & filters.user(OWNER_ID))
async def start_command(client, message):
    welcome_text = (
        f"👋 **Welcome to JioHotstar PRO, {message.from_user.first_name}!**\n\n"
        f"I am your personal Scene-Release downloading bot. I can extract movies and series from Hotstar, embed subtitles, and upload them directly to you in `.mkv` format.\n\n"
        f"🔗 **To begin:** Just send me a valid JioHotstar URL!"
    )
    await message.reply_text(welcome_text)

@app.on_message(filters.command("help") & filters.user(OWNER_ID))
async def help_command(client, message):
    help_text = (
        "🛠 **How to use this Bot:**\n\n"
        "1️⃣ Paste a Hotstar link in the chat.\n"
        "2️⃣ Choose your Video Quality (1080p, 720p, etc).\n"
        "3️⃣ Choose your Audio Codec (AAC or DDP 5.1).\n"
        "4️⃣ Wait for the bot to rip, merge, and upload the `.mkv` file.\n\n"
        "⚠️ **Important Limitations:**\n"
        "• **DRM:** New blockbusters/Disney movies are protected by Widevine DRM and cannot be downloaded.\n"
        "• **2GB Limit:** Telegram blocks files over 2GB. If your 1080p movie is too large, it will fail to upload. Choose 720p next time.\n"
        "• **Cookies:** If downloads suddenly stop working, your `cookies.txt` has expired and needs to be replaced on your server."
    )
    await message.reply_text(help_text)

# Prevent unauthorized users from using the bot
@app.on_message(filters.private & ~filters.user(OWNER_ID))
async def unauthorized_user(client, message):
    await message.reply_text("⛔️ **Access Denied.** You are not authorized to use this bot's premium bandwidth or cookies.")

@app.on_message(filters.text & filters.regex(r"hotstar\.com") & filters.user(OWNER_ID))
async def handle_url(client, message):
    url = message.text
    status_msg = await message.reply_text("🔍 **Fetching Metadata from Hotstar...**")
    
    data = await asyncio.to_thread(fetch_metadata, url)
    if not data:
        return await status_msg.edit_text("❌ Failed to fetch data. Check your cookies or ensure the link is correct.")

    user_id = message.chat.id
    user_states[user_id] = {"url": url}
    
    buttons = [
        [InlineKeyboardButton("1080p", callback_data="q_1080"), InlineKeyboardButton("720p", callback_data="q_720")],
        [InlineKeyboardButton("480p", callback_data="q_480"), InlineKeyboardButton("360p", callback_data="q_360")]
    ]
    
    if data.get("_type") != "playlist" or len(data.get("entries", [])) == 0:
        user_states[user_id]["type"] = "movie"
        user_states[user_id]["meta"] = data
        await status_msg.edit_text(f"🎬 **Movie Detected:** {data.get('title', 'Unknown')}\n\nSelect Quality:", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        title, episodes = parse_series(data, url)
        user_states[user_id]["type"] = "series"
        user_states[user_id]["episodes"] = episodes
        user_states[user_id]["series_title"] = title
        await status_msg.edit_text(f"📺 **Series Detected:** {title} ({len(episodes)} eps)\n\nSelect Quality:", reply_markup=InlineKeyboardMarkup(buttons))

@app.on_callback_query(filters.user(OWNER_ID))
async def callback_handler(client, query):
    user_id = query.message.chat.id
    data = query.data
    
    if data.startswith("q_"):
        user_states[user_id]["quality"] = data.split("_")[1]
        buttons = [
            [InlineKeyboardButton("AAC 2.0 (Standard/Smaller)", callback_data="ac_aac")],
            [InlineKeyboardButton("DDP 5.1 (Surround/Larger)", callback_data="ac_ddp")]
        ]
        await query.message.edit_text("🎵 **Select Audio Codec:**", reply_markup=InlineKeyboardMarkup(buttons))
        
    elif data.startswith("ac_"):
        user_states[user_id]["acodec"] = data.split("_")[1]
        state = user_states[user_id]
        
        await query.message.delete()
        
        if state["type"] == "movie":
            await process_download(client, query.message, state["url"], True, state["meta"])
        else:
            # Send the first episode automatically to prevent Telegram flooding
            first_ep = state["episodes"][0]
            await query.message.reply_text(f"⚙️ **Series Mode:** Downloading S{first_ep['season']:02d}E{first_ep['episode']:02d}...\n*(Bot currently processes one episode per link to avoid Telegram upload bans)*")
            await process_download(client, query.message, first_ep["url"], False, first_ep)

if __name__ == "__main__":
    print("🤖 Hotstar PRO Bot is running...")
    app.run()
