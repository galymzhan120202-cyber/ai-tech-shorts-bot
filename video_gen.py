import os
import random
import asyncio
import requests
import edge_tts
import google_auth_oauthlib.flow
import googleapiclient.discovery
import googleapiclient.http
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from moviepy import VideoFileClip, AudioFileClip, CompositeAudioClip
from moviepy.video.VideoClip import TextClip
from moviepy.video.compositing.CompositeVideoClip import CompositeVideoClip, concatenate_videoclips
from moviepy.video.fx import MultiplyColor
import sys
import io
import json
import logging
import time
from dotenv import load_dotenv
import traceback
import glob
import re

# UTF-8 кодтеуін орнату консоль үшін
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# .env файлын жүктеу
load_dotenv()

# --- ПАРАМЕТРЛЕР (ОРТА АЙНЫМАЛАЛАРДАН) ---
base_dir = os.getenv('BASE_DIR', os.path.dirname(os.path.abspath(__file__)))
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
PEXELS_API_KEY = os.getenv('PEXELS_API_KEY', '')
EDGE_TTS_VOICE = os.getenv('EDGE_TTS_VOICE', 'en-US-GuyNeural')
TELEGRAM_NOTIFY_TOKEN = os.getenv('TELEGRAM_NOTIFY_TOKEN', '')
TELEGRAM_NOTIFY_CHAT_ID = os.getenv('TELEGRAM_NOTIFY_CHAT_ID', '')

# Қайтара сынау параметрлері
MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))
RETRY_DELAY = int(os.getenv('RETRY_DELAY', '2'))
MIN_SCRIPT_LENGTH = int(os.getenv('MIN_SCRIPT_LENGTH', '20'))
# 30-40 сек сөйлеу ~75-105 сөзге сай келеді (Gemini промпты соны сұрайды) —
# бұрынғы 50 сөздік лимит сценарийді дерлік әр жолы кесіп тастайтын.
MAX_SCRIPT_LENGTH = int(os.getenv('MAX_SCRIPT_LENGTH', '110'))

# YouTube параметрлері (28 = Science & Technology)
YOUTUBE_CATEGORY_ID = os.getenv('YOUTUBE_CATEGORY_ID', '28')
YOUTUBE_PRIVACY_STATUS = os.getenv('YOUTUBE_PRIVACY_STATUS', 'public')
YOUTUBE_MADE_FOR_KIDS = os.getenv('YOUTUBE_MADE_FOR_KIDS', 'false').lower() == 'true'

# Видео құру параметрлері
VIDEO_CODEC = os.getenv('VIDEO_CODEC', 'libx264')
AUDIO_CODEC = os.getenv('AUDIO_CODEC', 'aac')
VIDEO_FPS = int(os.getenv('VIDEO_FPS', '24'))
VIDEO_PRESET = os.getenv('VIDEO_PRESET', 'ultrafast')

# Логирование орнату
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(base_dir, 'debug.log'), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# API ключтарын тексеру
if not GEMINI_API_KEY:
    logger.warning('⚠️ GEMINI_API_KEY .env файлында жоқ!')

SUBTITLE_CHAR_MAP = {
    '—': '-', '–': '-', ''': "'", ''': "'", '"': '"', '"': '"', '…': '...',
}

def sanitize_subtitle_text(text):
    """Субтитр қаріпінде glyph жоқ сирек Unicode таңбаларды (em dash, т.б.)
    қарапайым ASCII баламасына ауыстыру."""
    for src, dst in SUBTITLE_CHAR_MAP.items():
        text = text.replace(src, dst)
    return text

def truncate_to_sentence(script, max_words):
    """MAX_SCRIPT_LENGTH-тен асса, сөз ортасынан емес соңғы толық сөйлемнен
    қысқарту. CTA ("Follow for more.") әрқашан сақталады — жарты сөйлеммен
    аяқталған видео retention-ге зиян келтіреді."""
    has_cta = script.rstrip().endswith("Follow for more.")
    body = script.rsplit("Follow for more.", 1)[0].strip() if has_cta else script

    words = body.split()
    budget = max_words - 3 if has_cta else max_words
    if len(words) > budget:
        truncated = ' '.join(words[:budget])
        last_period = truncated.rfind('.')
        if last_period > len(truncated) * 0.5:
            truncated = truncated[:last_period + 1]
        body = truncated

    return f"{body} Follow for more." if has_cta else body

def validate_script(script):
    """Сценарийдің ұзындығы мен сапасын тексеру"""
    if not script or not script.strip():
        raise ValueError("Сценарий бос болуы мүмкін емес")

    script = script.strip()
    word_count = len(script.split())

    if word_count < MIN_SCRIPT_LENGTH:
        raise ValueError(f"Сценарий тым қысқа ({word_count} сөз, мин. {MIN_SCRIPT_LENGTH})")

    if word_count > MAX_SCRIPT_LENGTH:
        logger.warning(f"⚠️ Сценарий ұзын ({word_count} сөз), сөйлем шекарасынан қысқартылады")
        script = truncate_to_sentence(script, MAX_SCRIPT_LENGTH)

    return script

def ensure_directories_exist():
    """Қажетті папқаларды қаралыды және тексеру"""
    required_dirs = [
        os.path.join(base_dir, 'backgrounds'),
        os.path.join(base_dir, 'music'),
    ]

    for directory in required_dirs:
        if not os.path.exists(directory):
            raise FileNotFoundError(f"❌ Папқа жоқ: {directory}")

    # Фон видео — Pexels API арқылы (кілт керек). Кілт жоқ болса локал файлдарға сүйенеді.
    bg_files = [f for f in os.listdir(os.path.join(base_dir, 'backgrounds')) if not f.startswith('_pexels')]
    if not bg_files and not PEXELS_API_KEY:
        raise FileNotFoundError("❌ backgrounds/ бос және PEXELS_API_KEY орнатылмаған")

    # Музыка — Openverse API арқылы (кілт керек емес, әрдайым қолжетімді).
    # Толық сәтсіздік болса (желі/API проблема), generate_video ішінде локал fallback тексеріледі.

    logger.info("✓ Барлық папқалар дайын")

def retry_with_backoff(func, max_retries=MAX_RETRIES, retry_delay=RETRY_DELAY):
    """Функцияны қайта сынау (exponential backoff)"""
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = retry_delay * (2 ** attempt)
                logger.warning(f"⚠️ Сәтсіз (әрекет {attempt + 1}/{max_retries}): {str(e)[:100]}")
                logger.info(f"⏳ {wait_time} сек. күте тұр...")
                time.sleep(wait_time)
            else:
                logger.error(f"❌ {max_retries} әрекеттен кейін сәтсіз")
                raise

TOPICS = [
    # --- AI Tools & Hacks ---
    ("AI tools that will replace your job in 2026", "#ai #aitools #futuretech"),
    ("chatgpt tricks nobody is using yet", "#chatgpt #aitools #productivity"),
    ("AI apps that feel illegal to know about", "#aitools #ai #technology"),
    ("how AI reads your mind through your phone", "#ai #privacytech #bigdata"),
    ("free AI tools that replace expensive software", "#aitools #ai #productivity"),
    ("the AI prompt trick that changes everything", "#chatgpt #aitools #ai"),

    # --- Tech Facts ---
    ("mind-blowing tech facts that sound fake but are true", "#techfacts #technology #mindblown"),
    ("the hidden feature your phone has been hiding from you", "#phonehacks #techfacts #smartphone"),
    ("why your phone battery is designed to die", "#techfacts #smartphone #conspiracy"),
    ("the truth about 5G that companies don't tell you", "#5g #techfacts #technology"),
    ("tech secrets big companies don't want you to know", "#techfacts #technology #bigtech"),
    ("the reason your wifi is slower than it should be", "#wifi #techfacts #technology"),

    # --- Future Tech / Futurism ---
    ("technology that will change your life by 2030", "#futuretech #technology #innovation"),
    ("why robots will take over faster than you think", "#robots #ai #futuretech"),
    ("the scariest AI breakthrough of this year", "#ai #futuretech #technology"),
    ("quantum computers explained in 30 seconds", "#quantumcomputing #futuretech #technology"),
    ("the technology billionaires are secretly investing in", "#futuretech #technology #innovation"),

    # --- Coding / Programmer life ---
    ("coding tricks every programmer wishes they knew sooner", "#coding #programming #softwareengineer"),
    ("why programmers secretly hate this one habit", "#programming #coding #techlife"),
    ("the programming language taking over in 2026", "#coding #programming #techtrends"),
    ("signs you're a self-taught programmer", "#coding #programminglife #softwareengineer"),
    ("the coding mistake that costs companies millions", "#coding #programming #softwareengineer"),

    # --- Space & Science Tech ---
    ("space technology that sounds like science fiction", "#spacetech #technology #nasa"),
    ("the AI that is helping scientists talk to animals", "#ai #sciencetech #technology"),
    ("how NASA uses AI to explore space", "#nasa #ai #spacetech"),
    ("the science behind self-driving cars", "#spacetech #ai #technology"),

    # --- Gadgets ---
    ("gadgets from the future you can buy right now", "#gadgets #technology #futuretech"),
    ("hidden iphone settings almost nobody knows", "#iphone #techfacts #phonehacks"),
    ("the smart home gadget that changes everything", "#smarthome #gadgets #technology"),
    ("gadgets tech reviewers don't want you to skip", "#gadgets #technology #techreview"),

    # --- Real numbers & disasters (best historical performers — concrete
    # figures/stories outperform vague hooks like "billionaires' secret") ---
    ("the coding bug that cost a company $440 million in 45 minutes", "#coding #programming #techfacts"),
    ("the $125 million rocket that was lost because of one line of code", "#spacetech #coding #nasa"),
    ("the hard drive that weighed over a ton but stored only 5 megabytes", "#techfacts #technology #history"),
    ("the typo that deleted a company's entire production database", "#coding #programming #techfacts"),
    ("how one bug shut down the New York Stock Exchange for hours", "#techfacts #coding #bigtech"),
    ("the number of lines of code it took to land humans on the moon", "#spacetech #coding #nasa"),
    ("the AI city being built in space right now", "#spacetech #ai #futuretech"),
    ("the 1 typo that took down half the internet for an hour", "#techfacts #coding #bigtech"),
    ("the software glitch that cost NASA a $327 million Mars orbiter", "#spacetech #nasa #techfacts"),
    ("the bank error that accidentally sent $900 million to strangers", "#techfacts #bigtech #technology"),
]

HOOK_STARTERS = [
    "Nobody is talking about this, but",
    "Tech companies don't want you to know",
    "This changes everything —",
    "Scientists just revealed",
    "Here's the AI trick that",
    "Most people have no idea",
    "Silicon Valley insiders know",
]

STRONG_HASHTAG_POOL = [
    "#ai", "#technology", "#tech", "#techfacts", "#youtubeshorts",
    "#trending", "#viral", "#fyp", "#futuretech", "#artificialintelligence",
    "#innovation", "#gadgets", "#technews", "#sciencefacts", "#mindblown",
    "#explore", "#didyouknow",
]

# Gemini толығымен сәтсіз болғанда қолданылатын резервтік нұсқалар (бірнешеу —
# бірыңғай статикалық fallback арнада дәл бірдей видео 2 рет шығуына әкелген).
FALLBACK_CONTENTS = [
    {
        "script": "Tech companies don't want you to know this — your phone is already using AI to predict your next move before you make it. It learns your habits from every tap, scroll and pause. That data trains the algorithms that keep you scrolling longer. Follow for more.",
        "title": "The AI Trick Hiding Inside Your Phone",
        "niche": "#ai #aitools #technology #techfacts",
    },
    {
        "script": "Nobody is talking about this, but a single bad code deploy once cost a company four hundred forty million dollars in under an hour. No hackers, no years-old bug — just one unchecked release. It's proof that in tech, small mistakes scale fast. Follow for more.",
        "title": "The Coding Mistake That Cost $440 Million",
        "niche": "#coding #programming #techfacts",
    },
    {
        "script": "Scientists just revealed that early hard drives the size of a refrigerator stored less data than a single photo on your phone today. One model weighed over a ton and held just five megabytes. Technology has grown faster than almost anyone predicted. Follow for more.",
        "title": "The 1-Ton Hard Drive That Held Only 5MB",
        "niche": "#techfacts #technology #history",
    },
]

TECH_VISUAL_QUERIES = [
    "artificial intelligence", "coding on laptop", "server room technology",
    "circuit board macro", "futuristic city night", "robot technology",
    "space rocket launch", "quantum computer lab", "smartphone technology",
    "data center servers", "programmer typing keyboard", "drone flying technology",
    "virtual reality headset", "5g network technology", "tech gadgets flat lay",
    "neon technology background", "computer chip macro", "nasa space technology",
    "digital network abstract", "hacker typing computer",
]

def fetch_pexels_background():
    """Pexels API арқылы кездейсоқ 9:16 tech-тақырыпты видео жүктеп алу.
    Кілт жоқ болса немесе сұрау сәтсіз болса — None қайтарады (локал fallback үшін)."""
    if not PEXELS_API_KEY:
        return None

    query = random.choice(TECH_VISUAL_QUERIES)
    try:
        response = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "orientation": "portrait", "per_page": 15},
            timeout=15
        )
        response.raise_for_status()
        videos = response.json().get("videos", [])
        if not videos:
            logger.warning(f"⚠️ Pexels: '{query}' бойынша видео табылмады")
            return None

        video_data = random.choice(videos)
        candidates = [
            vf for vf in video_data.get("video_files", [])
            if vf.get("width", 0) < vf.get("height", 0) and vf.get("width", 0) >= 720
        ]
        if not candidates:
            # 720p+ тік нұсқа жоқ болса, ең сапалы қолжетімдіге түсеміз
            candidates = [
                vf for vf in video_data.get("video_files", [])
                if vf.get("width", 0) < vf.get("height", 0) and vf.get("width", 0) >= 480
            ]
        if not candidates:
            return None
        # Ең жақсы сапаны таңдау (ең үлкен ені — анық, бұлыңғы емес кадр үшін)
        candidates.sort(key=lambda vf: vf["width"], reverse=True)
        video_file = candidates[0]

        dest = os.path.join(base_dir, "backgrounds", "_pexels_temp.mp4")
        dl_response = requests.get(video_file["link"], stream=True, timeout=30)
        dl_response.raise_for_status()

        with open(dest, "wb") as f:
            for chunk in dl_response.iter_content(chunk_size=1024 * 256):
                f.write(chunk)

        if os.path.getsize(dest) < 10_000:
            raise Exception("Жүктелген видео тым кіші")

        logger.info(f"✓ Pexels-тен видео жүктелді (сұрау: '{query}')")
        return dest

    except Exception as e:
        logger.warning(f"⚠️ Pexels қатесі, локал fallback қолданылады: {str(e)[:100]}")
        return None

MUSIC_QUERIES = [
    "ambient", "technology", "electronic", "cinematic", "inspiring",
    "energetic", "futuristic", "instrumental", "synth", "electronic instrumental",
]

def _try_fetch_openverse_music(query, min_duration_sec):
    """Бір сұраныс бойынша Openverse-тен лайықты трек іздеп көру. Таппаса — None."""
    response = requests.get(
        "https://api.openverse.org/v1/audio/",
        params={
            "q": query,
            "category": "music",
            "license": "cc0,by",
            "page_size": 20,
        },
        timeout=15,
        headers={"User-Agent": "AITechShortsBot/1.0 (automated background music fetch)"}
    )
    response.raise_for_status()
    results = response.json().get("results", [])

    min_duration_ms = (min_duration_sec + 5) * 1000
    candidates = [
        r for r in results
        if r.get("duration") and r["duration"] >= min_duration_ms and r.get("url")
    ]
    if not candidates:
        logger.warning(f"⚠️ Openverse: '{query}' бойынша лайықты трек табылмады")
        return None

    track = random.choice(candidates)
    dest = os.path.join(base_dir, "music", "_openverse_temp.mp3")

    dl_response = requests.get(track["url"], stream=True, timeout=30)
    dl_response.raise_for_status()

    with open(dest, "wb") as f:
        for chunk in dl_response.iter_content(chunk_size=1024 * 256):
            f.write(chunk)

    if os.path.getsize(dest) < 10_000:
        raise Exception("Жүктелген музыка тым кіші")

    license_type = (track.get("license") or "").lower()
    attribution = None
    if license_type and license_type != "cc0":
        creator = track.get("creator", "Unknown artist")
        title = track.get("title", "Untitled")
        source_url = track.get("foreign_landing_url") or track.get("url")
        attribution = f'Music: "{title}" by {creator} ({license_type.upper()}) — {source_url}'

    logger.info(f"✓ Openverse-тен музыка жүктелді (сұрау: '{query}', лицензия: {license_type or 'белгісіз'})")
    return dest, attribution

def fetch_openverse_music(min_duration_sec):
    """Openverse API (Jamendo/Freesound/Wikimedia CC-каталогы) арқылы CC0/CC-BY
    музыка іздеп жүктеп алу. OAuth/кілт керек емес. Бірнеше сұранысты кезекпен
    сынайды (біреуі нәтиже бермесе, келесісіне өтеді). Сәтсіз болса — None
    қайтарады (локал music/ fallback үшін). CC-BY трек табылса, атрибуция
    жолын да қайтарады — ол видео сипаттамасына қосылады (лицензия талабы)."""
    tried_queries = random.sample(MUSIC_QUERIES, min(4, len(MUSIC_QUERIES)))
    for query in tried_queries:
        try:
            result = _try_fetch_openverse_music(query, min_duration_sec)
            if result:
                return result
        except Exception as e:
            logger.warning(f"⚠️ Openverse қатесі ('{query}'): {str(e)[:100]}")

    logger.warning("⚠️ Openverse: барлық сұраныстар сәтсіз, локал fallback қолданылады")
    return None

def get_local_music_attribution(filename):
    """music/fallback_attribution.json файлынан локал сақтық трек үшін CC-BY
    атрибуциясын іздеп табу (бар болса). Файл/жазба жоқ болса — None."""
    attribution_file = os.path.join(base_dir, "music", "fallback_attribution.json")
    if not os.path.exists(attribution_file):
        return None
    try:
        with open(attribution_file, "r", encoding="utf-8") as f:
            entries = json.load(f)
        for entry in entries:
            if entry.get("file") == filename and (entry.get("license") or "").lower() != "cc0":
                return (
                    f'Music: "{entry.get("title", "Untitled")}" by '
                    f'{entry.get("creator", "Unknown artist")} '
                    f'({entry.get("license", "by").upper()}) — {entry.get("foreign_landing_url", "")}'
                )
    except Exception:
        return None
    return None

def send_telegram(message: str):
    """Telegram хабарламасы жіберу"""
    if not TELEGRAM_NOTIFY_TOKEN or not TELEGRAM_NOTIFY_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_NOTIFY_TOKEN}/sendMessage"
        requests.post(
            url,
            json={"chat_id": TELEGRAM_NOTIFY_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception:
        pass

def pick_rotating_tags(exclude_tags, count=5):
    """STRONG_HASHTAG_POOL-дан exclude_tags-пен қайталанбайтын тегтерді таңдау."""
    excluded = {t.lower() for t in exclude_tags.split()}
    pool = [t for t in STRONG_HASHTAG_POOL if t.lower() not in excluded]
    return ' '.join(random.sample(pool, min(count, len(pool))))

TOPIC_STOPWORDS = {
    "the", "a", "an", "of", "in", "on", "to", "your", "you", "that", "this",
    "is", "are", "will", "for", "and", "by", "right", "now", "one", "you're",
}

def _topic_keywords(text):
    return {w for w in re.findall(r"[a-z']+", text.lower()) if w not in TOPIC_STOPWORDS and len(w) > 3}

def pick_fresh_topic(recent_titles, attempts=6):
    """TOPICS-тен соңғы жүктелген видео атауларымен тым ұқсамайтын тақырып
    таңдау (2+ маңызды сөз сәйкес келсе — қайталанады деп есептеледі).
    Мақсат — "phone reads your mind" секілді тақырыптың қатарынан 2 рет
    шығып кетуін болдырмау."""
    recent_word_sets = [_topic_keywords(t) for t in recent_titles]
    if not recent_word_sets:
        return random.choice(TOPICS)

    for _ in range(attempts):
        topic, niche_tags = random.choice(TOPICS)
        topic_words = _topic_keywords(topic)
        collision = any(len(topic_words & rw) >= 2 for rw in recent_word_sets)
        if not collision:
            return topic, niche_tags

    return random.choice(TOPICS)

def get_gemini_content(recent_titles=None):
    """Gemini-дан сценарий + тақырып + хештегтер алу"""
    logger.info("📝 Gemini-дан контент жазылуда...")

    models_to_try = [
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}",
        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash-lite:generateContent?key={GEMINI_API_KEY}",
        f"https://generativelanguage.googleapis.com/v1/models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}",
    ]

    topic, niche_tags = pick_fresh_topic(recent_titles or [])
    hook_start = random.choice(HOOK_STARTERS)
    rotating_tags = pick_rotating_tags(niche_tags)

    prompt = (
        f'Create viral YouTube Shorts content about {topic}.\n'
        f'The hook MUST start with: "{hook_start}"\n'
        'Respond ONLY in this exact JSON format (no extra text, no markdown):\n'
        '{"script": "...", "title": "...", "hashtags": "..."}\n\n'
        'Rules:\n'
        f'- script: Start with "{hook_start}" as a shocking hook. The hook must include a specific number, '
        'dollar amount, or concrete statistic whenever the topic allows it (e.g. "$125 million", "1 ton", "47%") '
        '— concrete numbers consistently outperform vague claims like "secret" or "changes everything" with no specifics. '
        'Then 2-3 sentences of the fact, building on that concrete detail. End with "Follow for more." No emojis. '
        '30-40 seconds of speech (75-100 words).\n'
        '- title: Under 55 characters. Start with a number or a concrete figure when possible. Must grab attention. Do NOT include hashtags in title.\n'
        '  Good examples: "The Bug That Cost $440 Million" / "1 Ton Hard Drive Held Only 5MB"\n'
        f'- hashtags: Write exactly in this format (9 tags total, keep #shorts always):\n'
        f'  {niche_tags} {rotating_tags} #shorts\n'
        '  Replace only the first 3 niche tags if needed to match the specific video topic. Keep the rest exactly as given.'
    )

    for url in models_to_try:
        try:
            model_name = url.split("models/")[1].split(":")[0] if "models/" in url else "unknown"
            logger.info(f"🔄 Модель сынау: {model_name}")

            response = requests.post(
                url,
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=15,
                headers={"Content-Type": "application/json"}
            )

            if response.status_code == 200:
                payload = response.json()
                if 'candidates' in payload and payload['candidates']:
                    parts = payload['candidates'][0].get('content', {}).get('parts', [])
                    if parts and 'text' in parts[0]:
                        raw = parts[0]['text'].strip()
                        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
                        if json_match:
                            data = json.loads(json_match.group())
                            script = validate_script(data.get('script', ''))
                            title = data.get('title', f'Tech Facts: {topic.title()}')[:100]
                            hashtags = data.get('hashtags', f'{niche_tags} {rotating_tags} #shorts')
                            description = f"{script}\n\n{hashtags}"
                            tags = parse_hashtags_to_tags(hashtags)
                            logger.info(f"✓ Контент дайын (тақырып: {topic})")
                            return script, title, description, tags
            else:
                logger.warning(f"⚠️ {model_name}: HTTP {response.status_code}")

        except Exception as e:
            logger.warning(f"⚠️ {model_name} қатесі: {str(e)[:100]}")

    logger.warning("⚠️ Gemini сәтсіз, резервтік контент қолданылуда")
    fallback = random.choice(FALLBACK_CONTENTS)
    fallback_hashtags = f"{fallback['niche']} {pick_rotating_tags(fallback['niche'], 4)} #shorts"
    fallback_desc = f"{fallback['script']}\n\n{fallback_hashtags}"
    return (
        validate_script(fallback['script']),
        fallback['title'],
        fallback_desc,
        parse_hashtags_to_tags(fallback_hashtags),
    )

def parse_hashtags_to_tags(hashtags_str):
    """'#ai #shorts #fyp' секілді хэштег жолын YouTube tags[] өрісіне сай
    таза сөздер тізіміне айналдыру (# белгісіз, қайталанусыз)."""
    seen = set()
    tags = []
    for tag in hashtags_str.split():
        clean = tag.lstrip('#').strip()
        if clean and clean.lower() not in seen:
            seen.add(clean.lower())
            tags.append(clean)
    return tags

def get_recent_channel_titles(max_results=15):
    """Арнаға соңғы жүктелген видеолардың атауларын YouTube API арқылы алу
    (тақырып қайталанбауын тексеру үшін — pick_fresh_topic соны қолданады).
    Токен жоқ/сәтсіз болса — бос тізім қайтарады (video generation бұғатталмайды)."""
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    token_file = os.path.join(base_dir, "youtube_token.json")

    if not os.path.exists(token_file):
        return []

    try:
        credentials = Credentials.from_authorized_user_file(token_file, scopes)
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())

        youtube = googleapiclient.discovery.build("youtube", "v3", credentials=credentials)
        channels_response = youtube.channels().list(part="contentDetails", mine=True).execute()
        items = channels_response.get("items", [])
        if not items:
            return []

        uploads_playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        playlist_response = youtube.playlistItems().list(
            part="snippet", playlistId=uploads_playlist_id, maxResults=max_results
        ).execute()

        titles = [item["snippet"]["title"] for item in playlist_response.get("items", [])]
        logger.info(f"✓ Соңғы {len(titles)} видео атауы алынды (дубляж тексеру үшін)")
        return titles

    except Exception as e:
        logger.warning(f"⚠️ Соңғы видео атауларын алу сәтсіз: {str(e)[:100]}")
        return []

def upload_to_youtube(video_path, title, description, tags=None):
    logger.info("📤 YouTube-ке жүктеу басталуда...")

    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    client_file = os.path.join(base_dir, "client_secrets.json")
    token_file = os.path.join(base_dir, "youtube_token.json")

    credentials = None

    try:
        # 1. Сохраненный токен проверка
        if os.path.exists(token_file):
            try:
                credentials = Credentials.from_authorized_user_file(token_file, scopes)
                if credentials.expired and credentials.refresh_token:
                    credentials.refresh(Request())
                    with open(token_file, 'w') as f:
                        f.write(credentials.to_json())
                logger.info("✓ Сохраненные учетные данные загружены")
            except Exception as e:
                logger.warning(f"⚠️ Токен мәселесі: {e}")
                credentials = None

        # 2. Жаңа OAuth ағымы
        if credentials is None:
            try:
                flow = google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file(
                    client_file, scopes
                )
                credentials = flow.run_local_server(
                    port=0,
                    open_browser=True,
                    authorization_prompt_message='Браузерде OAuth логинін орындаңыз: {url}',
                    success_message='✓ Аутентификация сәтті! Терезесін жабыңыз.'
                )

                with open(token_file, 'w') as f:
                    f.write(credentials.to_json())
                logger.info("✓ Жаңа OAuth токены сақталды")

            except Exception as e:
                logger.error(f"❌ OAuth қатесі: {e}")
                raise

        # 3. YouTube API клиентін құру
        youtube = googleapiclient.discovery.build("youtube", "v3", credentials=credentials)

        # 4. Видеоны жүктеу
        request_body = {
            "snippet": {
                "title": title,
                "description": description,
                "categoryId": YOUTUBE_CATEGORY_ID,
                "tags": tags or ["ai", "technology", "shorts", "techfacts"]
            },
            "status": {
                "privacyStatus": YOUTUBE_PRIVACY_STATUS,
                "selfDeclaredMadeForKids": YOUTUBE_MADE_FOR_KIDS
            }
        }

        logger.info(f"📤 Файл жүктелуде: {os.path.basename(video_path)}")

        media = googleapiclient.http.MediaFileUpload(
            video_path,
            chunksize=1024*1024,
            resumable=True
        )

        request = youtube.videos().insert(
            part="snippet,status",
            body=request_body,
            media_body=media
        )

        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                logger.info(f"  Прогресс: {progress}%")

        video_id = response['id']
        logger.info(f"\n✅ ЖЕҢІС! Видео YouTube-та жүктелді!")
        logger.info(f"   ID: {video_id}")
        logger.info(f"   URL: https://youtube.com/shorts/{video_id}")

    except Exception as e:
        logger.error(f"❌ Жүктеу қатесі: {e}")
        raise

def cleanup_temp_files():
    """Уақытша файлдарды өчіру"""
    temp_patterns = [
        os.path.join(base_dir, "TEMP_MPY_*.mp4"),
        os.path.join(base_dir, "temp_voice.mp3"),
        os.path.join(base_dir, "backgrounds", "_pexels_temp.mp4"),
        os.path.join(base_dir, "music", "_openverse_temp.mp3")
    ]
    for pattern in temp_patterns:
        for temp_file in glob.glob(pattern):
            try:
                os.remove(temp_file)
                logger.debug(f"  Қалдық өшірілді: {os.path.basename(temp_file)}")
            except:
                pass

def generate_video(script_override: str = None, skip_upload: bool = False):
    try:
        logger.info("🎬 Видео құру процессі басталды")

        # Папқалар мен файлдарды тексеру
        ensure_directories_exist()

        # Уақытша файлдарды тазалау
        cleanup_temp_files()

        # Сценарий + тақырып + сипаттама алу
        if script_override:
            script = validate_script(script_override)
            video_title = f"Tech Facts #shorts"
            override_niche = "#ai #technology #shorts #techfacts"
            override_hashtags = f"{override_niche} {pick_rotating_tags(override_niche, 3)}"
            video_description = f"{script}\n\n{override_hashtags}"
            video_tags = parse_hashtags_to_tags(override_hashtags)
            logger.info("Жіберілген мәтін қолданылды")
        else:
            recent_titles = get_recent_channel_titles()
            script, video_title, video_description, video_tags = retry_with_backoff(
                lambda: get_gemini_content(recent_titles)
            )

        logger.info(f"📝 Сценарий: {script[:80]}...")
        logger.info(f"🏷️ Тақырып: {video_title}")

        # 1. Дыбыс жасау — Edge TTS (сөйлем таймштамптарымен)
        temp_voice = os.path.join(base_dir, "temp_voice.mp3")

        async def _generate_tts_with_timestamps():
            communicate = edge_tts.Communicate(script, voice=EDGE_TTS_VOICE)
            sentences = []
            audio_bytes = bytearray()
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_bytes.extend(chunk["data"])
                elif chunk["type"] == "SentenceBoundary":
                    sentences.append({
                        "text": chunk["text"],
                        "start": chunk["offset"] / 10_000_000,
                        "duration": chunk["duration"] / 10_000_000,
                    })
            with open(temp_voice, "wb") as f:
                f.write(bytes(audio_bytes))
            return sentences

        def create_audio():
            sentences = asyncio.run(_generate_tts_with_timestamps())
            if os.path.getsize(temp_voice) < 1000:
                raise Exception("Дыбыс файлы тым кішкентай")
            logger.info(f"✓ Edge TTS дайын: {os.path.getsize(temp_voice)} байт, {len(sentences)} сөйлем")
            return sentences

        sentence_timestamps = retry_with_backoff(create_audio)

        def build_word_chunks(sentences, words_per_chunk=4):
            """Сөйлем уақытын сөз топтарына бөлу"""
            chunks = []
            for sent in sentences:
                words = sent["text"].split()
                groups = [words[i:i + words_per_chunk] for i in range(0, len(words), words_per_chunk)]
                chunk_dur = sent["duration"] / max(len(groups), 1)
                for j, group in enumerate(groups):
                    chunks.append({
                        "text": sanitize_subtitle_text(" ".join(group)),
                        "start": sent["start"] + j * chunk_dur,
                        "duration": chunk_dur,
                    })
            return chunks

        # 2. Файлдарды таңдау
        bg_folder = os.path.join(base_dir, "backgrounds")
        music_folder = os.path.join(base_dir, "music")

        total_script_duration = (
            sentence_timestamps[-1]["start"] + sentence_timestamps[-1]["duration"]
            if sentence_timestamps else 45
        )

        try:
            bg_path = fetch_pexels_background()

            if not bg_path:
                bg_files = [f for f in os.listdir(bg_folder) if f.endswith(('.mp4', '.mov')) and not f.startswith('_pexels')]
                if not bg_files:
                    raise FileNotFoundError("Фондық видео файлдары жоқ (Pexels де, локал да)")
                bg_path = os.path.join(bg_folder, random.choice(bg_files))

            music_path = None
            music_attribution = None
            music_result = fetch_openverse_music(total_script_duration)
            if music_result:
                music_path, music_attribution = music_result

            if not music_path:
                music_files = [f for f in os.listdir(music_folder) if f.endswith(('.mp3', '.wav')) and not f.startswith('_openverse')]
                if not music_files:
                    raise FileNotFoundError("Музыка файлдары жоқ (Openverse де, локал да)")
                chosen_music_file = random.choice(music_files)
                music_path = os.path.join(music_folder, chosen_music_file)
                music_attribution = get_local_music_attribution(chosen_music_file)

            if music_attribution:
                video_description += f"\n\n{music_attribution}"

            logger.info(f"🎵 Таңдалды - Видео: {os.path.basename(bg_path)}, Музыка: {os.path.basename(music_path)}")

        except Exception as e:
            logger.error(f"❌ Файл таңдау қатесі: {e}")
            raise

        # 3. Видео құрастыру
        video = None
        voice = None
        music = None
        final_video = None

        try:
            logger.info("🎬 Видео құралуда...")

            video = VideoFileClip(bg_path)

            # Stock видеолар көбіне басында focus-pull/blur эффектімен ашылады — қиып тастау
            intro_skip = min(0.7, max(0, video.duration - 1))
            if intro_skip > 0:
                video = video.subclipped(intro_skip)

            try:
                video = video.with_effects([MultiplyColor(0.55)])
            except Exception:
                logger.warning("⚠️ Қараңғылату эффект қосылмады")

            voice = AudioFileClip(temp_voice)
            music = AudioFileClip(music_path).subclipped(0, voice.duration)
            music = music.with_volume_scaled(0.08)

            # Видеоны дауысқа сәйкес ұзарту
            if video.duration < voice.duration:
                num_loops = int(voice.duration / video.duration) + 1
                video = concatenate_videoclips([video] * num_loops).subclipped(0, voice.duration)
            else:
                video = video.subclipped(0, voice.duration)

            # Баяу zoom in эффект (фон жақындап келеді)
            try:
                dur = voice.duration
                video = video.resized(lambda t: 1 + 0.03 * (t / dur))
                logger.info("✓ Zoom эффект қосылды")
            except Exception:
                logger.warning("⚠️ Zoom эффект қосылмады")

            # 4. СӨЗ-СӨЗБЕН СУБТИТР
            try:
                chunks = build_word_chunks(sentence_timestamps, words_per_chunk=4)

                wrap_w = int(video.w * 0.88)
                font_sz = max(45, min(70, int(video.w / 11)))
                logger.info(f"📝 Субтитр: {len(chunks)} топ, font={font_sz}px")

                sub_clips = []
                for chunk in chunks:
                    c = (
                        TextClip(
                            text=chunk["text"],
                            font_size=font_sz,
                            color='white',
                            stroke_color='black',
                            stroke_width=3,
                            method='caption',
                            size=(wrap_w, None),
                            margin=(20, 20),
                            text_align='center',
                        )
                        .with_start(chunk["start"])
                        .with_duration(chunk["duration"])
                        .with_position(('center', 'center'))
                    )
                    sub_clips.append(c)

                final_audio = CompositeAudioClip([voice, music])
                final_video = CompositeVideoClip([video] + sub_clips).with_audio(final_audio)
                logger.info(f"✓ Сөз-сөзбен субтитр қосылды ({len(sub_clips)} топ)")

            except Exception as e:
                logger.warning(f"⚠️ Субтитр қатесі: {e}")
                final_audio = CompositeAudioClip([voice, music])
                final_video = video.with_audio(final_audio)

            final_output = os.path.join(base_dir, "final_shorts.mp4")

            logger.info(f"\n⏳ Видео құрылуда ({VIDEO_CODEC}, {VIDEO_FPS}fps)...")

            try:
                final_video.write_videofile(
                    final_output,
                    codec=VIDEO_CODEC,
                    audio_codec=AUDIO_CODEC,
                    fps=VIDEO_FPS,
                    preset=VIDEO_PRESET,
                    logger=None
                )
                logger.info(f"✓ Видео дайын: {final_output}")

            except Exception as write_error:
                logger.warning(f"⚠️ Видео жазу қатесі: {write_error}")
                logger.info("   Резервтік кодек қолданылуда...")
                final_video.write_videofile(
                    final_output,
                    codec="mpeg4",
                    audio_codec="libmp3lame",
                    fps=VIDEO_FPS,
                    preset='ultrafast'
                )

            # 5. YouTube жүктеу
            if not skip_upload:
                retry_with_backoff(lambda: upload_to_youtube(final_output, video_title, video_description, video_tags))
                send_telegram(
                    f"✅ <b>Жаңа AI/Tech видео жүктелді!</b>\n"
                    f"📌 <b>Тақырып:</b> {video_title}\n"
                    f"📝 <b>Сценарий:</b> {script[:120]}..."
                )
            else:
                logger.info(f"✓ Видео сақталды (жүктеу өтіп кетті)")

        finally:
            # Ресурстарды босату
            try:
                if video:
                    video.close()
                if voice:
                    voice.close()
                if music:
                    music.close()
                if final_video:
                    final_video.close()
                logger.info("✓ Ресурстар босатылды")
            except:
                pass

    except Exception as e:
        logger.error(f"❌ Қате: {e}")
        logger.debug(traceback.format_exc())
        send_telegram(f"❌ <b>AI/Tech видео жасауда қате шықты!</b>\n<code>{str(e)[:300]}</code>")
        raise

if __name__ == "__main__":
    try:
        generate_video()
    except Exception as e:
        logger.error(f"Программа сәтсіз аяқталды: {e}")
        sys.exit(1)
