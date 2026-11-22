"""OCDS release -> flat opportunity / award records. Keeps org-level data only (no named contacts)."""
import hashlib
import json
import re

REQ_SENT = re.compile(r"[^.\n]*\b(must|shall|mandatory|required|requirement|minimum|accredit|certif|insurance|"
                      r"clearance|turnover|framework|experience)\b[^.\n]*[.\n]?", re.I)


def _buyer(rel):
    b = rel.get("buyer") or {}
    party = next((p for p in rel.get("parties", []) if "buyer" in (p.get("roles") or [])), {})
    return b.get("name") or party.get("name"), b.get("id") or party.get("id"), party.get("address") or {}


def _url(source, rel, docs):
    for d in docs:
        if d.get("url") and d.get("documentType") in ("tenderNotice", "awardNotice"):
            return d["url"]
    if source == "fts":
        return f"https://www.find-tender.service.gov.uk/Notice/{rel.get('id')}"
    return f"https://www.contractsfinder.service.gov.uk/Notice/{rel.get('ocid', '').split('-', 2)[-1]}"


def fingerprint(title, buyer):
    t = re.sub(r"\b[a-z]*\d[\w/-]*\b", " ", (title or "").lower())  # drop reference codes
    t = re.sub(r"[^a-z ]", " ", t)
    b = re.sub(r"[^a-z]", "", (buyer or "").lower())[:30]
    return " ".join(t.split())[:80] + "|" + b


def opportunity(source, rel):
    t = rel.get("tender") or {}
    buyer, buyer_id, addr = _buyer(rel)
    lots = t.get("lots") or []
    desc_parts = [t.get("description") or ""] + [l.get("description") or "" for l in lots]
    if source == "fts" and rel.get("description"):
        desc_parts.append(rel["description"])
    description = "\n".join(p for p in dict.fromkeys(desc_parts) if p).strip()
    cpvs = [(t.get("classification") or {}).get("id")]
    for it in t.get("items") or []:
        cpvs += [c.get("id") for c in it.get("additionalClassifications") or []]
    value = (t.get("value") or {}).get("amount")
    if value is None:
        lot_vals = [(l.get("value") or {}).get("amount") for l in lots]
        value = max([v for v in lot_vals if v] or [None], key=lambda v: v or 0)
    sme = (t.get("suitability") or {}).get("sme")
    if sme is None:
        sme = next(((l.get("suitability") or {}).get("sme") for l in lots if (l.get("suitability") or {}).get("sme") is not None), None)
    reqs = [c.get("description", "") for c in (t.get("selectionCriteria") or {}).get("criteria", [])]
    reqs += [m.group(0).strip() for m in REQ_SENT.finditer(description)]
    method = t.get("procurementMethodDetails") or t.get("procurementMethod") or ""
    if (t.get("techniques") or {}).get("hasFrameworkAgreement"):
        method += " (framework)"
    rec = {
        "ocid": rel["ocid"], "source": source, "notice_id": rel.get("id"),
        "title": (t.get("title") or "").strip(), "description": description,
        "requirements_text": "\n".join(dict.fromkeys(r for r in reqs if r))[:4000],
        "buyer": buyer, "buyer_id": buyer_id,
        "region": addr.get("region") or addr.get("locality") or addr.get("postalCode"),
        "cpv": cpvs[0], "cpv_all": ",".join(c for c in dict.fromkeys(cpvs) if c),
        "value_amount": value, "value_currency": (t.get("value") or {}).get("currency", "GBP"),
        "deadline": (t.get("tenderPeriod") or {}).get("endDate"),
        "published": t.get("datePublished") or rel.get("date"), "updated": rel.get("date"),
        "procurement_method": method.strip(), "sme_suitable": None if sme is None else int(bool(sme)),
        "url": _url(source, rel, t.get("documents") or []),
    }
    rec["fingerprint"] = fingerprint(rec["title"], buyer)
    rec["raw_hash"] = hashlib.sha1(json.dumps([rec["title"], description, value, rec["deadline"]], default=str).encode()).hexdigest()
    return rec


def awards(source, rel):
    t = rel.get("tender") or {}
    buyer, buyer_id, _ = _buyer(rel)
    out = []
    for a in rel.get("awards") or []:
        sup = a.get("suppliers") or []
        cp = a.get("contractPeriod") or {}
        out.append({
            "award_key": f"{rel['ocid']}:{a.get('id')}", "ocid": rel["ocid"], "source": source,
            "title": a.get("title") or t.get("title"), "buyer": buyer, "buyer_id": buyer_id,
            "supplier": "; ".join(s.get("name", "") for s in sup),
            "supplier_id": "; ".join(s.get("id", "") for s in sup),
            "value": (a.get("value") or {}).get("amount"), "award_date": a.get("date"),
            "contract_start": cp.get("startDate"), "contract_end": cp.get("endDate"),
            "cpv": (t.get("classification") or {}).get("id"),
            "url": _url(source, rel, a.get("documents") or []),
            "description": t.get("description") or "",
        })
    return out
