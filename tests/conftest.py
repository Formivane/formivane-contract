import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from fic import config
from fic.db import connect

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("FIC_ROOT", str(tmp_path))
    c = config.load()
    c["db_path"] = str(tmp_path / "test.db")
    c["profile"] = {"company_name": "Test", "accreditations": {}, "finance": {}, "delivery": {"day_rate_gbp": 400}}
    c["collection"]["request_delay_seconds"] = 0
    # Pin the value bands these tests were written against; the live window is tested separately.
    c["relevance"].update(value_min=0, value_max=10**12)
    c["scoring"].update(sweet_spot_min=5000, sweet_spot_max=150000)
    return c


@pytest.fixture
def con(cfg, monkeypatch, tmp_path):
    import fic.learning as L
    monkeypatch.setattr(L, "ROOT", tmp_path)  # knowledge export goes to tmp
    c = connect(cfg["db_path"])
    yield c
    c.close()


def fixture_releases(name):
    return json.loads((FIX / name).read_text())["releases"]


def release(title, desc="", buyer="Test Council", value=25000, days=20, cpv="72413000", sme=True, ocid=None, published=None):
    pub = published or datetime.now(timezone.utc) - timedelta(days=1)
    return {
        "ocid": ocid or f"ocds-test-{uuid.uuid4().hex[:10]}", "id": uuid.uuid4().hex[:8], "date": pub.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tag": ["tender"], "buyer": {"id": "GB-T-1", "name": buyer},
        "parties": [{"id": "GB-T-1", "name": buyer, "roles": ["buyer"], "address": {"region": "UKI"}}],
        "tender": {"title": title, "description": desc, "value": {"amount": value, "currency": "GBP"} if value else None,
                   "classification": {"scheme": "CPV", "id": cpv}, "datePublished": pub.strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "tenderPeriod": {"endDate": (pub + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")},
                   "procurementMethod": "open", "suitability": {"sme": sme},
                   "documents": [{"documentType": "tenderNotice", "url": "https://www.contractsfinder.service.gov.uk/Notice/x"}]},
    }
