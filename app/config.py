"""配置与数据文件加载（路径、减脂归一表、营养映射、估算参数）。"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from functools import lru_cache

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

TAXONOMY_FILE = os.path.join(DATA_DIR, "menu-taxonomy.yaml")
NUTRITION_MAP_FILE = os.path.join(DATA_DIR, "nutrition-mapping.csv")
ESTIMATE_FILE = os.path.join(DATA_DIR, "estimate-params.yaml")


@dataclass(frozen=True)
class Taxonomy:
    version: object
    category_map: dict[str, str]
    overrides: dict[str, dict]
    creamer: dict


@lru_cache(maxsize=1)
def load_taxonomy(path: str = TAXONOMY_FILE) -> Taxonomy:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    overrides = {str(item["productCode"]): item for item in (raw.get("product_overrides") or [])}
    return Taxonomy(
        version=raw.get("version"),
        category_map=raw.get("category_map") or {},
        overrides=overrides,
        creamer=raw.get("creamer") or {},
    )


@lru_cache(maxsize=1)
def load_nutrition_map(path: str = NUTRITION_MAP_FILE) -> dict[str, dict]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8-sig") as f:
        return {row["productCode"]: row for row in csv.DictReader(f)}


@lru_cache(maxsize=1)
def load_estimate_params(path: str = ESTIMATE_FILE) -> dict:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def runtime_config() -> dict:
    """运行时配置：凭据只从环境变量读，不写文件。"""
    return {
        "token": os.environ.get("MCD_TOKEN", "").strip(),
        "endpoint": os.environ.get("MCD_ENDPOINT", "https://mcp.mcd.cn"),
        "host": os.environ.get("APP_HOST", "127.0.0.1"),
        "port": int(os.environ.get("APP_PORT", "8000")),
    }
