"""Печатает URL вебхука ЮKassa из текущего окружения.

Запуск (из каталога Aico_coa с активным venv и загруженным .env):
    set -a; source .env; set +a
    python deploy/print_webhook_url.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from services import bridge

domain = settings.DOMAIN or "YOUR_DOMAIN"
print(f"https://{domain}/pay/webhook/{bridge.webhook_token()}")
