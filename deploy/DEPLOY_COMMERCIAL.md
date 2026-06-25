# Деплой CoachDojo (бот + Mini App + ЮKassa)

Пошаговая установка на VPS (Ubuntu). Предполагается путь `/opt/coach-bot`.
Шаги, помеченные 🔑, требуют домен/ключи ЮKassa — их подставляем в самом конце.

## 1. Сервер и код
```bash
sudo adduser --system --group coachbot
sudo mkdir -p /opt/coach-bot && sudo chown coachbot:coachbot /opt/coach-bot
# залить проект в /opt/coach-bot/Aico_coa (git clone / scp / rsync)
cd /opt/coach-bot/Aico_coa
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo mkdir -p /var/lib/coach-bot/data && sudo chown -R coachbot:coachbot /var/lib/coach-bot
```

## 2. Конфиг .env
```bash
cp .env.example .env
# заполнить TELEGRAM_TOKEN, ANTHROPIC_API_KEY
# HMAC_SECRET сгенерировать: openssl rand -hex 32
nano .env
```
ЮKassa-ключи и DOMAIN можно оставить пустыми на старте — бот поднимется, но
оплата будет недоступна (paywall покажет текстовые тарифы вместо Mini App).

## 3. Сервис (systemd)
```bash
sudo cp deploy/coach-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now coach-bot
journalctl -u coach-bot -f          # смотрим логи запуска
```
В логах должно быть: инициализация БД, `Legacy migrate`, `Scheduler loop started`,
`Bridge started on 127.0.0.1:8080`.

## 4. 🔑 Домен, nginx и TLS
```bash
# A-запись домена → IP сервера
sudo cp deploy/nginx.conf /etc/nginx/sites-available/coach-bot
sudo sed -i 's/YOUR_DOMAIN/coachdojo.app/g' /etc/nginx/sites-available/coach-bot
sudo ln -s /etc/nginx/sites-available/coach-bot /etc/nginx/sites-enabled/
sudo certbot --nginx -d coachdojo.app          # выпуск TLS
sudo nginx -t && sudo systemctl reload nginx
```
Затем в `.env` указать `DOMAIN=coachdojo.app` и `sudo systemctl restart coach-bot`.

Проверка гейта: открыть `https://coachdojo.app/` в браузере — должно быть
«Откройте страницу из бота» (403). Это правильно: Mini App открывается только из бота.

## 5. 🔑 ЮKassa
1. В личном кабинете ЮKassa взять **shopId** и **секретный ключ** → в `.env`
   (`YK_SHOP_ID`, `YK_SECRET_KEY`). Если включены «Чеки» — `YK_RECEIPTS_ENABLED=true`.
2. `sudo systemctl restart coach-bot`.
3. Узнать URL вебхука:
   ```bash
   cd /opt/coach-bot/Aico_coa && set -a; source .env; set +a
   .venv/bin/python deploy/print_webhook_url.py
   ```
4. В ЮKassa (HTTP-уведомления) добавить этот URL для события
   `payment.succeeded`.

## 6. 🔑 BotFather
- `/setmenubutton` или Mini App: бот сам ставит персональную menu-кнопку на `/start`,
  отдельная настройка не обязательна. При желании задать домен Mini App в
  настройках бота (Bot Settings → Configure Mini App) на `https://DOMAIN/`.

## 7. Смоук-тест полного цикла
1. В боте `/start` → пройти онбординг → исчерпать бесплатный лимит → появляется paywall.
2. Нажать «Оформить подписку» → открывается Mini App с тарифами.
3. Выбрать тариф → ЮKassa → оплатить тестовой картой.
4. Прилетает «✅ Оплата прошла…», режимы открываются.
5. В логах — `purchase`, в БД `payments.status=succeeded`, `users.access_until` в будущем.

## Обновление и бэкап
```bash
# бэкап БД перед изменениями
cp /var/lib/coach-bot/data/bot.db /var/lib/coach-bot/data/bot.db.bak
# деплой новой версии
git pull && .venv/bin/pip install -r requirements.txt
sudo systemctl restart coach-bot
```
Схема БД создаётся идемпотентно при старте; миграции данных безопасны к повтору.
