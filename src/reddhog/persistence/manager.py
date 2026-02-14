import asyncio
import csv
import io
import json
import logging
from pathlib import Path

import aiofiles
from openpyxl import Workbook

from reddhog.config import SCRAPER_DATA
from reddhog.models import Post

logger = logging.getLogger("reddit_scraper")

EXPORT_HEADERS = [
    "id", "title", "flair", "description", "upvotes", "comments_count",
    "author", "timestamp", "url", "images_local", "comment_count_crawled",
]


class DataManager:
    def __init__(self, data_dir: Path | None = None):
        self._data_dir = data_dir if data_dir is not None else SCRAPER_DATA
        self.json_path = self._data_dir / "data.json"
        self.csv_path = self._data_dir / "data.csv"
        self.excel_path = self._data_dir / "data.xlsx"
        self.data: dict[str, Post] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        if not self.json_path.exists():
            self.data = {}
            return
        async with aiofiles.open(self.json_path, encoding="utf-8") as f:
            content = await f.read()
        data_list = json.loads(content)
        self.data = {p["id"]: Post.model_validate(p) for p in data_list}

    def upsert(self, post: Post) -> None:
        self.data[post.id] = post

    async def save_json(self) -> None:
        data_list = [p.model_dump() for p in self.data.values()]
        tmp_path = self.json_path.parent / (self.json_path.name + ".tmp")
        async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data_list, ensure_ascii=False, indent=2))
        Path(tmp_path).replace(self.json_path)

    async def upsert_and_save(self, post: Post) -> None:
        async with self._lock:
            self.upsert(post)
            await self.save_json()

    @staticmethod
    def _cell_for_csv(value: str | int) -> str:
        if isinstance(value, int):
            return str(value)
        s = (value or "").replace("\r\n", " ").replace("\n", " ").replace("\r", " ").strip()
        return s

    def export_csv(self) -> None:
        data_list = list(self.data.values())
        buf = io.StringIO()
        w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
        w.writerow(EXPORT_HEADERS)
        for post in data_list:
            w.writerow([
                self._cell_for_csv(post.id),
                self._cell_for_csv(post.title),
                self._cell_for_csv(post.flair),
                self._cell_for_csv(post.description),
                self._cell_for_csv(post.upvotes),
                self._cell_for_csv(post.comments_count),
                self._cell_for_csv(post.author),
                self._cell_for_csv(post.timestamp),
                self._cell_for_csv(post.url),
                "|".join(i.local_path for i in post.images),
                len(post.comments),
            ])
        with self.csv_path.open("w", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())

    def export_excel(self) -> None:
        wb = Workbook()
        ws = wb.worksheets[0]
        ws.title = "Posts"
        ws.append(EXPORT_HEADERS)
        for post in self.data.values():
            row = [
                post.id,
                post.title,
                post.flair,
                post.description,
                post.upvotes,
                post.comments_count,
                post.author,
                post.timestamp,
                post.url,
                "|".join(i.local_path for i in post.images),
                len(post.comments),
            ]
            ws.append(row)
        wb.save(str(self.excel_path))
        logger.info(f"Exported Excel: {self.excel_path}")
