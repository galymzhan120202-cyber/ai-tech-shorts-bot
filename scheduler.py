import os
import sys
import io
import schedule
import time
import logging
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
load_dotenv()

# Күнде 3 уақыт (UTC). Психология ботымен соқтығыспас үшін 30-60 мин ығыстырылған.
# Қазақстан UTC+5:
POST_TIMES = [
    t.strip()
    for t in os.getenv('POST_TIMES', '03:30,09:30,15:30').split(',')
    if t.strip()
]

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('scheduler.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

from video_gen import generate_video, send_telegram

def make_job(slot_num):
    def job():
        logger.info(f"⏰ {slot_num}-жүктеу басталды...")
        send_telegram(f"⏰ <b>AI/Tech {slot_num}/3 жүктеу басталды</b>")
        try:
            generate_video()
            logger.info(f"✅ {slot_num}-жүктеу сәтті аяқталды")
        except Exception as e:
            logger.error(f"❌ {slot_num}-жүктеу сәтсіз: {e}")
    return job

for i, t in enumerate(POST_TIMES, start=1):
    schedule.every().day.at(t).do(make_job(i))
    logger.info(f"  ✓ {i}-жүктеу: {t} UTC ({int(t.split(':')[0])+5:02d}:{t.split(':')[1]} KZ)")

kz_times = [f"{int(t.split(':')[0])+5:02d}:{t.split(':')[1]}" for t in POST_TIMES]
logger.info(f"🚀 AI/Tech Scheduler іске қосылды — күнде {len(POST_TIMES)} видео")
logger.info("   Тоқтату үшін Ctrl+C")
send_telegram(
    f"🚀 <b>AI/Tech Scheduler іске қосылды</b>\n"
    f"📅 Күнде <b>{len(POST_TIMES)} видео</b>\n"
    f"🕐 Уақыттары (KZ): <b>{' / '.join(kz_times)}</b>"
)

while True:
    schedule.run_pending()
    time.sleep(30)
