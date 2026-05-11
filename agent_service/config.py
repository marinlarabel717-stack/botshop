from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
AGENT_SERVICE_DIR = Path(__file__).resolve().parent


def load_agent_env() -> None:
    """Load root env first, then allow agent_service overrides."""
    for path in (
        BASE_DIR / '.env',
        BASE_DIR / '.env.local',
        AGENT_SERVICE_DIR / '.env',
        AGENT_SERVICE_DIR / '.env.local',
    ):
        if path.exists():
            load_dotenv(path, override=True)


@dataclass
class AgentRuntimeConfig:
    agent_bot_id: str
    bot_token: str
    agent_name: str
    agent_username: str
    customer_service: str
    default_lang: str = 'zh'
    admin_ids: tuple[int, ...] = ()
    trc20_address: str = ''
    recharge_amounts: tuple[float, ...] = (10.0, 30.0, 50.0, 100.0)

    @classmethod
    def from_env(cls) -> 'AgentRuntimeConfig':
        load_agent_env()
        admin_ids_raw = str(os.getenv('AGENT_ADMIN_IDS', os.getenv('ADMIN_IDS', '')) or '').strip()
        admin_ids = tuple(int(item.strip()) for item in admin_ids_raw.split(',') if item.strip().isdigit())
        amounts_raw = str(os.getenv('AGENT_RECHARGE_AMOUNTS', '10,30,50,100') or '').strip()
        amounts = []
        for item in amounts_raw.split(','):
            item = item.strip()
            if not item:
                continue
            try:
                amount = float(item)
            except Exception:
                continue
            if amount > 0:
                amounts.append(amount)
        if not amounts:
            amounts = [10.0, 30.0, 50.0, 100.0]
        return cls(
            agent_bot_id=str(os.getenv('AGENT_BOT_ID', '') or '').strip(),
            bot_token=str(os.getenv('AGENT_BOT_TOKEN', '') or '').strip(),
            agent_name=str(os.getenv('AGENT_NAME', '代理分销') or '代理分销').strip(),
            agent_username=str(os.getenv('AGENT_USERNAME', '') or '').strip().lstrip('@'),
            customer_service=str(os.getenv('AGENT_CUSTOMER_SERVICE', '') or '').strip(),
            default_lang=str(os.getenv('AGENT_DEFAULT_LANG', 'zh') or 'zh').strip() or 'zh',
            admin_ids=admin_ids,
            trc20_address=str(os.getenv('AGENT_TRC20_ADDRESS', os.getenv('TRC20_ADDRESS', '')) or '').strip(),
            recharge_amounts=tuple(amounts),
        )

    def validate(self) -> None:
        if not self.agent_bot_id:
            raise RuntimeError('缺少 AGENT_BOT_ID，请先在 agent_service/.env 中配置代理 bot 标识')
        if not self.bot_token:
            raise RuntimeError('缺少 AGENT_BOT_TOKEN，请先在 agent_service/.env 中配置代理 bot Token')
