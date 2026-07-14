import json
import logging
import time
from pathlib import Path
from typing import Callable

from core.config import get_settings
from core.errors import AppException
from repositories.nominated_repository import NominatedRepository

logger = logging.getLogger(__name__)


class SimpleCache:
    def __init__(self):
        settings = get_settings()
        self.ttl = settings.cache_ttl_seconds
        self.cache_dir = Path(settings.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.memory = {}

    def _file(self, key: str) -> Path:
        safe = key.replace(":", "_").replace("/", "_")
        return self.cache_dir / f"{safe}.json"

    def get_or_load(self, key: str, loader: Callable):
        now = time.time()
        item = self.memory.get(key)
        if item and now - item["ts"] < self.ttl:
            logger.info("cache_hit memory key=%s", key)
            return item["data"]

        file_path = self._file(key)
        if file_path.exists():
            payload = json.loads(file_path.read_text(encoding="utf-8"))
            if now - payload.get("ts", 0) < self.ttl:
                logger.info("cache_hit file key=%s", key)
                self.memory[key] = payload
                return payload["data"]

        logger.info("cache_miss key=%s loading_from_db=true", key)
        data = loader()
        payload = {"ts": now, "data": data}
        self.memory[key] = payload
        file_path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        return data

    def clear(self):
        self.memory.clear()
        for p in self.cache_dir.glob("*.json"):
            p.unlink(missing_ok=True)


cache = SimpleCache()


class NominatedService:
    def __init__(self, repo: NominatedRepository):
        self.repo = repo

    def bootstrap(self, enrollment_id: int, board_level_id: int, location_value: int):
        key = f"nom_bootstrap:{enrollment_id}:{board_level_id}:{location_value}"
        return cache.get_or_load(key, lambda: {
            "postTypes": list(self.repo.get_post_types()),
            "levels": list(self.repo.get_levels()),
            "states": list(self.repo.get_states()),
            "departments": self.departments(enrollment_id, board_level_id, location_value),
        })
    def departments(self, enrollment_id: int, board_level_id: int, location_value: int):
        logger.info(
            "service=departments enrollment_id=%s board_level_id=%s location_value=%s",
            enrollment_id,
            board_level_id,
            location_value,
        )

        return self.repo.get_departments(
            enrollment_id=enrollment_id,
            board_level_id=board_level_id,
            location_value=location_value,
        )
    def locations(self, board_level_id: int, state_id: int | None):
        key = f"locations:{board_level_id}:{state_id or 'ALL'}"
        return cache.get_or_load(key, lambda: list(self.repo.get_locations(board_level_id, state_id)))

    def boards(self, enrollment_id: int, board_level_id: int, location_value: int, department_id: int | None = None):
        key = f"boards:{enrollment_id}:{board_level_id}:{location_value}:{department_id if department_id is not None else 'ALL'}"
        return cache.get_or_load(key, lambda: list(self.repo.get_boards(enrollment_id, board_level_id, location_value, department_id)))

    def positions(self, enrollment_id: int, board_level_id: int, location_value: int, department_id: int, board_id: int):
        key = f"positions:{enrollment_id}:{board_level_id}:{location_value}:{department_id}:{board_id}"
        return cache.get_or_load(key, lambda: list(self.repo.get_positions(enrollment_id, board_level_id, location_value, department_id, board_id)))

    def capacity(self, enrollment_id: int, board_level_id: int, location_value: int, department_id: int, board_id: int, position_id: int):
        key = f"capacity:{enrollment_id}:{board_level_id}:{location_value}:{department_id}:{board_id}:{position_id}"
        data = cache.get_or_load(key, lambda: self.repo.get_position_capacity(enrollment_id, board_level_id, location_value, department_id, board_id, position_id))
        if not data:
            raise AppException("No capacity data found for selected position", 404, "CAPACITY_NOT_FOUND")
        return data

    def search_cadre(self, query: str, limit: int = 10):
        if not query or len(query.strip()) < 3:
            raise AppException("Search text must be at least 3 characters", 400, "INVALID_SEARCH_TEXT")
        return list(self.repo.search_cadre(query.strip(), limit))

    def refresh_cache(self):
        cache.clear()
        return {"cacheCleared": True}
