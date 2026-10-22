"""Installation-specific account binding. Never ship a real account as a default."""
import json
import os
from pathlib import Path

HOME=Path(os.environ.get('BILI_DUTY_HOME') or Path(__file__).resolve().parent)
def load():
    try:
        value=json.loads((HOME/'identity.json').read_text('utf-8-sig'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}

def uid(value):
    import re
    return value if isinstance(value,str) and re.fullmatch(r'[1-9][0-9]*',value) else ''

data=load()
OWNER_UID=uid(data.get('ownerUid'))
BOT_UID=uid(data.get('botUid'))
OWNER_NAME=str(data.get('ownerName') or '主人')[:80]
BOT_NAME=str(data.get('botName') or '大肥鱼')[:80]
