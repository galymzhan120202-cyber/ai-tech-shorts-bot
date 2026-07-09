# AI/Tech Shorts Bot — Setup нұсқаулығы

Код пен pipeline толық дайын (`video_gen.py`, `scheduler.py`, `.github/workflows/upload.yml`).
Төмендегі қадамдарды тек сіз қолмен жасай аласыз (браузерде/сыртқы сервистерде).

## 1. Жаңа YouTube арна

1. Жаңа Google аккаунт ашыңыз (немесе қазіргі аккаунтта қосымша арна құрыңыз — Brand Account).
2. YouTube Studio-да арнаны AI/Tech нишасына сай атаумен, суретпен баптаңыз.

## 2. Google Cloud OAuth (жүктеу үшін міндетті)

1. https://console.cloud.google.com — жаңа жоба жасаңыз (мыс. `AITechShortsBot`).
2. **YouTube Data API v3**-ті қосыңыз (APIs & Services → Library).
3. **OAuth consent screen** баптаңыз (External, Testing режимі жеткілікті).
4. **Credentials → Create Credentials → OAuth client ID → Desktop app** жасап, JSON жүктеп алыңыз → осы файлды `client_secrets.json` деп осы папкаға салыңыз.
5. Жергілікті бір рет `python video_gen.py` іске қосыңыз — браузерде 1-жаңа арнамен логин болып, `youtube_token.json` автоматты жасалады.

**Маңызды:** OAuth логин кезінде дәл жаңа AI/Tech арнаға тиесілі Google аккаунтпен кіріңіз, әйтпесе видео ескі арнаға жүктеледі.

## 3. GitHub repo + Secrets

1. Жаңа бөлек GitHub repo ашыңыз (мыс. `ai-tech-shorts-bot`), осы папканы push етіңіз.
2. Repo → Settings → Secrets and variables → Actions → төмендегі 6 Secret қосыңыз:
   - `AITECH_GEMINI_API_KEY`
   - `AITECH_PEXELS_API_KEY` — 4-қадамды қараңыз
   - `AITECH_TELEGRAM_NOTIFY_TOKEN`
   - `AITECH_TELEGRAM_NOTIFY_CHAT_ID`
   - `AITECH_CLIENT_SECRETS_JSON` — `client_secrets.json` файлының толық мазмұны
   - `AITECH_YOUTUBE_TOKEN_JSON` — `youtube_token.json` файлының толық мазмұны (2-қадамнан кейін пайда болады)

## 4. Фон видео (Pexels API — автоматты, шексіз)

Фон видео **қолмен жинақталмайды** — `video_gen.py` әр жүктеу алдында [Pexels Video API](https://www.pexels.com/api/) арқылы кездейсоқ tech-тақырыпты (AI, coding, robots, space, gadgets...) 9:16 stock footage іздеп, автоматты жүктеп алады. Бұл жылдар бойы тегін жұмыс істейді (күніне 3 сұрау — Pexels лимиті 20 000/ай, шамамен 90/ай ғана жұмсалады) және әр видео басқа footage болады.

**Баптау:**
1. https://www.pexels.com/api/ — тегін тіркеліп, API кілт алыңыз (бірден беріледі, күту керек емес).
2. `.env`-ге `PEXELS_API_KEY=...` қосыңыз (немесе GitHub Secret-ке `AITECH_PEXELS_API_KEY`).
3. Кілт болмаса — код автоматты `backgrounds/` папкасындағы локал файлдарға ауысады (fallback), сондықтан 1-2 сақтық видео қосып қою ұсынылады (Pexels/желі сәтсіз болған жағдайға).

## 5. Музыка (Openverse API — автоматты, шексіз, кілтсіз)

Музыка да **қолмен жинақталмайды** — `video_gen.py` әр жүктеу алдында [Openverse API](https://api.openverse.org/) арқылы (Jamendo/Freesound/Wikimedia Commons-тың CC-каталогы) CC0 немесе CC-BY лицензиялы tech/энергетикалы фон музыка іздеп, автоматты жүктеп алады.

- **Кілт/тіркелу керек емес** — API толық анонимды жұмыс істейді, шексіз тегін.
- Тек **CC0** (лицензия/атрибуция керек емес) және **CC-BY** (атрибуция керек) треки қолданылады — екеуі де коммерциялық/монетизацияланған YouTube-ке заңды рұқсат етеді.
- CC-BY трек түскенде, код авторлық атрибуцияны видео сипаттамасына (description) автоматты қосады — заң талабы солай орындалады, сізге ештеңе істеу керек емес.
- Сценарий ұзақтығына сай (әдетте 30-45 сек) жеткілікті ұзын трек ғана таңдалады.

**Сақтық fallback:** Openverse/желі сәтсіз болған сирек жағдайға арнап, `music/` папкасына 2-3 сақтық трек қолмен қосып қою ұсынылады ([Mixkit Music](https://mixkit.co/free-stock-music/) — royalty-free, атрибуция керек емес). Бос қалса да, Openverse дұрыс жұмыс істеп тұрса, видео жасалуы жалғаса береді.

## 6. Брендинг (Canva)

- **Banner:** Canva → "YouTube Channel Art" → aesthetic/vibrant санатынан tech-стильді шаблон таңдап, арна атауын/түсін баптаңыз.
- **Logo:** Canva → "YouTube Logo" → tech/futuristic шаблон.
- **Thumbnail:** Shorts-та thumbnail автоматты кадрдан алынады, бірақ custom thumbnail қаласаңыз Canva → "YouTube Thumbnail".

## 7. Тексеру реті

1. `.env.example`-ды `.env` етіп көшіріп, нақты кілттермен толтырыңыз.
2. `pip install -r requirements.txt`
3. Жергілікті сынау (жүктеместен): `python -c "from video_gen import generate_video; generate_video(skip_upload=True)"`
4. `final_shorts.mp4`-ты тексеріңіз (тақырып/субтитр/дауыс дұрыс па).
5. Нақты жүктеуді бір рет қолмен сынаңыз: `python video_gen.py`
6. Барлығы жұмыс істесе, GitHub Actions-та `workflow_dispatch` арқылы бір рет қолмен іске қосып тексеріңіз.
7. Содан кейін ғана cron кестесіне сеніп қалдырыңыз.
