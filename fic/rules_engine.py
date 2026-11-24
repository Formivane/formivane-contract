"""Deterministic decision engine: relevance, eligibility, scoring, classification.

Pure functions of (opportunity, config, active rules). This is what lets the learning
engine re-run history with/without a candidate rule at zero Claude cost.
"""
import re
from datetime import datetime, timezone

CLASSES = ["Strong candidate", "Possible candidate", "Requires investigation", "Unlikely to be suitable", "Ineligible"]
POSITIVE = {"Strong candidate", "Possible candidate"}
NEGATIVE = {"Unlikely to be suitable", "Ineligible"}

CATEGORIES = [  # (category, pattern) first match wins
    ("website_maintenance", r"website (maintenance|support|hosting)|web hosting|maintenance and support of .*website"),
    ("website_build", r"website|web site|web design|wordpress|drupal|\bcms\b|web portal|landing page"),
    ("crm", r"\bcrm\b|customer relationship|dynamics 365|salesforce|hubspot"),
    ("automation", r"automat|workflow|\brpa\b|robotic process|power automate|chatbot|artificial intelligence|\bai\b"),
    ("data_reporting", r"dashboard|reporting|power bi|data (analysis|processing|management|platform)|analytics"),
    ("digital_transformation", r"digital transformation|digital service|discovery phase|\balpha\b|\bbeta\b|user research"),
    ("software_dev", r"software|application development|app development|web application|\bapi\b|integration|system"),
]
BASE_EFFORT_DAYS = {  # (low, high) delivery days for a typical small-agency-sized job
    "website_build": (10, 35), "website_maintenance": (6, 24), "crm": (20, 60), "automation": (12, 45),
    "data_reporting": (10, 40), "digital_transformation": (30, 120), "software_dev": (30, 120), "other": (20, 80),
}
DIFFICULTY = {"website_build": "Low-Medium", "website_maintenance": "Low", "crm": "Medium", "automation": "Medium",
              "data_reporting": "Medium", "digital_transformation": "High", "software_dev": "Medium-High", "other": "Unknown"}

M = {"k": 1e3, "m": 1e6, "million": 1e6}
_NUM = r"£\s?([\d,]+(?:\.\d+)?)\s*(m|million|k)?\b"

# (key, regex, label, profile_path, numeric, hard)
BASE_REQUIREMENTS = [
    ("cyber_essentials_plus", r"cyber\s*essentials\s*(plus|\+)", "Cyber Essentials Plus", "accreditations.cyber_essentials_plus", False, True),
    ("cyber_essentials", r"cyber\s*essentials(?!\s*(plus|\+))", "Cyber Essentials", "accreditations.cyber_essentials", False, True),
    ("iso_27001", r"iso\s*/?\s*(iec\s*)?27001", "ISO 27001", "accreditations.iso_27001", False, True),
    ("iso_9001", r"iso\s*9001", "ISO 9001", "accreditations.iso_9001", False, True),
    ("security_clearance", r"\b(sc|dv|security)[ -](clearance|cleared|vetting)|developed vetting", "Security clearance (SC/DV)", "security.sc_clearance_staff", False, True),
    ("bpss", r"\bbpss\b|baseline personnel security", "BPSS checks", "security.bpss_staff", False, False),
    ("framework_gcloud", r"g-?cloud\s*\d*", "G-Cloud framework membership", "frameworks.g_cloud", False, True),
    ("framework_dos", r"digital outcomes(\s+and specialists)?|\bdos\s?\d\b", "Digital Outcomes framework membership", "frameworks.digital_outcomes", False, True),
    ("turnover", r"turnover[^.£]{0,60}" + _NUM, "Minimum annual turnover", "finance.annual_turnover_gbp", True, True),
    ("pi_insurance", r"professional indemnity[^.£]{0,80}" + _NUM, "Professional indemnity insurance", "finance.professional_indemnity_gbp", True, False),
    ("pl_insurance", r"public liability[^.£]{0,80}" + _NUM, "Public liability insurance", "finance.public_liability_gbp", True, False),
    ("el_insurance", r"employer'?s'? liability[^.£]{0,80}" + _NUM, "Employer's liability insurance", "finance.employers_liability_gbp", True, False),
    ("years_trading", r"(?:minimum of|at least)\s+(\d+)\s+years'?\s+(?:of\s+)?(?:trading|experience)", "Minimum years trading/experience", "finance.years_trading", True, True),
    ("references", r"(\d|two|three|four|five)\s+(?:client\s+|contract\s+)?(?:references|case studies)", "Client references", "delivery.references_available", True, False),
    ("onsite", r"\bon[- ]?site\b", "On-site working", "delivery.can_work_onsite", False, False),
]
WORDNUM = {"two": 2, "three": 3, "four": 4, "five": 5}


def text_of(opp):
    return " \n".join(filter(None, [opp.get("title"), opp.get("description"), opp.get("requirements_text")]))


def _profile(profile, path):
    cur = profile
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return None if cur == "unknown" else cur


def _amount(m):
    nums = [g for g in m.groups() if g and re.match(r"^[\d,.]+$", g)]
    if not nums:
        return None
    unit = next((g for g in m.groups() if g and g.lower() in M), None)
    base = float(nums[-1].replace(",", "")) if "£" in m.group(0) else float(nums[0].replace(",", ""))
    return base * M[unit.lower()] if unit else base


# ---------------------------------------------------------------- relevance
def relevance(opp, cfg, rules=()):
    rc = cfg["relevance"]
    title, body = (opp.get("title") or "").lower(), (opp.get("description") or "").lower()[:6000]
    inc = list(rc["include_terms"]) + [r["spec"]["term"] for r in rules if r["kind"] == "relevance_include"]
    exc = list(rc["exclude_terms"]) + [r["spec"]["term"] for r in rules if r["kind"] == "relevance_exclude"]
    score, hits = 0.0, []
    def has(t, s):
        return re.search(r"(?<![a-z0-9])" + re.escape(t.strip()) + r"(?![a-z0-9])", s)
    for term in inc:
        t = term.lower()
        if has(t, title):
            score += rc["title_weight"]; hits.append(term.strip())
        elif has(t, body):
            score += 1; hits.append(term.strip())
    cpvs = (opp.get("cpv_all") or opp.get("cpv") or "").split(",")
    if any(c.startswith(p) for c in cpvs if c for p in rc["cpv_include"]):
        score += 3; hits.append("cpv:" + cpvs[0])
    for term in exc:
        if has(term.lower(), title):
            score -= 4; hits.append("-" + term)
        elif has(term.lower(), body[:1500]):
            score -= 1; hits.append("-" + term)
    return round(score, 2), hits


def category(opp):
    t = ((opp.get("title") or "") + " " + (opp.get("description") or "")[:1500]).lower()
    return next((c for c, p in CATEGORIES if re.search(p, t)), "other")


# ---------------------------------------------------------------- eligibility
def eligibility(opp, profile, rules=()):
    text = text_of(opp)
    found = []
    reqs = [dict(key=k, pattern=p, label=l, path=pp, numeric=n, hard=h, source="base") for k, p, l, pp, n, h in BASE_REQUIREMENTS]
    for r in rules:
        if r["kind"] == "eligibility_gate":
            s = r["spec"]
            reqs.append(dict(key=r["rule_key"], pattern=s["pattern"], label=s.get("label", s["pattern"]), path=s.get("profile_key"),
                             numeric=False, hard=s.get("effect", "ineligible") == "ineligible", source=f"rule:{r['id']}v{r['version']}"))
    seen = set()
    for q in reqs:
        m = re.search(q["pattern"], text, re.I)
        if not m or q["key"] in seen:
            continue
        if q["key"] == "cyber_essentials" and "cyber_essentials_plus" in seen:
            continue
        seen.add(q["key"])
        have = _profile(profile, q["path"]) if q["path"] else None
        need = None
        if q["numeric"]:
            g = m.group(1) if q["key"] in ("years_trading", "references") else None
            need = (WORDNUM.get(g.lower()) or int(g)) if g else _amount(m)
            met = "unknown" if have is None or need is None else ("yes" if float(have) >= need else "no")
        elif not q["path"]:  # learned gate with no profile mapping: effect decides
            met = "no" if q["hard"] else "unknown"
        else:
            met = "unknown" if have is None else ("yes" if have else "no")
        s, e = max(0, m.start() - 60), min(len(text), m.end() + 60)
        found.append({"req": q["label"], "key": q["key"], "met": met, "hard": q["hard"], "required": need,
                      "evidence": " ".join(text[s:e].split()), "source": q["source"]})
    if any(f["met"] == "no" and f["hard"] for f in found):
        status = "ineligible"
    elif any(f["met"] in ("unknown", "no") for f in found):
        status = "requires_verification"
    elif found:
        status = "likely_eligible_unverified"
    else:
        status = "no_blockers_found"
    return {"status": status, "requirements": found}


# ---------------------------------------------------------------- scoring
def _days_left(deadline, today=None):
    if not deadline:
        return None
    try:
        d = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    except ValueError:
        return None
    today = today or datetime.now(timezone.utc)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return (d - today).total_seconds() / 86400


def economics(opp, cfg, effort_override=None):
    cat = opp.get("category") or category(opp)
    lo, hi = effort_override or BASE_EFFORT_DAYS.get(cat, BASE_EFFORT_DAYS["other"])
    v = opp.get("value_amount")
    if v and not effort_override:  # buyer budget implies expected effort at market day rates
        r_lo, r_hi = cfg["economics"]["market_day_rate"]
        lo, hi = max(1, round(v / r_hi)), max(1, round(v / r_lo))
    dc = cfg["economics"]["internal_day_cost"]
    blo, bhi = cfg["economics"]["bid_cost_days"]
    cost = (lo * dc + blo * dc, hi * dc + bhi * dc)
    ceiling = bool(v and (v >= cfg["scoring"]["ceiling_value_flag"] or re.search(r"framework|maximum (estimated )?value|up to £|not guaranteed", text_of(opp), re.I)))
    profit = (round(v - cost[1]), round(v - cost[0])) if v else None
    return {"category": cat, "effort_days": [lo, hi], "est_cost_gbp": [round(cost[0]), round(cost[1])],
            "advertised_value_gbp": v, "value_is_ceiling": ceiling, "profit_range_gbp": profit,
            "difficulty": DIFFICULTY.get(cat, "Unknown"),
            "note": "Profit = advertised value minus your estimated cost. Advertised value is the buyer's budget/ceiling, not guaranteed revenue."}


def score(opp, cfg, profile, rules=(), elig=None, econ=None, buyer_awards=0, today=None):
    sc, w = cfg["scoring"], cfg["scoring"]["weights"]
    elig = elig or eligibility(opp, profile, rules)
    econ = econ or economics(opp, cfg)
    v = opp.get("value_amount")
    c = {}
    # profitability
    if not v:
        c["profitability"] = (0.5, "low", "No advertised value")
    else:
        lo_p = econ["profit_range_gbp"][0]
        band = 1.0 if sc["sweet_spot_min"] <= v <= sc["sweet_spot_max"] else (0.6 if v <= sc["max_realistic_value"] and v > sc["sweet_spot_max"] else 0.3)
        if v < sc["sweet_spot_min"]:
            band = 0.4
        margin = max(min(lo_p / v, 1), -1) if v else 0
        val = max(0.0, min(1.0, 0.6 * band + 0.4 * max(margin, 0)))
        conf = "low" if econ["value_is_ceiling"] else "medium"
        c["profitability"] = (val, conf, f"£{v:,.0f} advertised; worst-case profit £{lo_p:,.0f}" + (" (ceiling value)" if econ["value_is_ceiling"] else ""))
    # feasibility
    rel = opp.get("relevance") or 0
    feas = min(rel / 10, 1.0)
    if v and v > sc["max_realistic_value"]:
        feas *= 0.5
    if econ["category"] in ("digital_transformation", "software_dev") and v and v > 250000:
        feas *= 0.7
    c["feasibility"] = (round(feas, 3), "medium" if rel else "low", f"relevance {rel}, category {econ['category']}")
    # eligibility
    ev = {"ineligible": (0.0, "high"), "requires_verification": (0.4, "low"),
          "likely_eligible_unverified": (0.85, "medium"), "no_blockers_found": (0.7, "low")}[elig["status"]]
    c["eligibility"] = (ev[0], ev[1], elig["status"])
    # competition (proxy, NOT a win probability)
    comp, why = 0.5, []
    if opp.get("sme_suitable") == 1:
        comp += 0.2; why.append("SME-suitable")
    meth = (opp.get("procurement_method") or "").lower()
    if "framework" in meth or "restricted" in meth or "negotiat" in meth:
        comp -= 0.2; why.append(meth[:30])
    if v and v < 30000:
        comp += 0.1; why.append("below £30k (lower-tier competition)")
    if v and v > sc["max_realistic_value"]:
        comp -= 0.2; why.append("large contract")
    c["competition"] = (max(0, min(1, comp)), "low", "proxy only: " + (", ".join(why) or "no signals"))
    # time
    dl = _days_left(opp.get("deadline"), today)
    if dl is None:
        c["time"] = (0.5, "low", "no deadline published")
    elif dl < 0:
        c["time"] = (0.0, "high", "deadline passed")
    elif dl < sc["min_days_to_deadline"]:
        c["time"] = (0.15, "high", f"{dl:.0f} days left")
    else:
        c["time"] = (1.0 if dl >= 10 else 0.6, "high", f"{dl:.0f} days left")
    # strategic
    strat = 0.5 + (0.3 if buyer_awards >= 2 else 0) + (0.2 if econ["category"] in ("automation", "website_build", "crm") else 0)
    c["strategic"] = (min(strat, 1), "low", f"{buyer_awards} relevant past awards by buyer")
    # rule-based adjustments (validated score_adjust rules)
    text = text_of(opp)
    for r in rules:
        if r["kind"] == "score_adjust" and re.search(r["spec"]["pattern"], text, re.I):
            k = r["spec"]["criterion"]
            val, conf, note = c[k]
            c[k] = (max(0, min(1, val + r["spec"]["delta"])), conf, note + f"; rule {r['rule_key']} {r['spec']['delta']:+}")
    total = sum(w[k] * c[k][0] for k in w) * 100
    spread = sum(w[k] * (0.5 if c[k][1] == "low" else 0.2 if c[k][1] == "medium" else 0.05) for k in w) * 100 / 2
    gated = None
    if rel < cfg["relevance"]["min_score"]:
        gated, total = "not relevant to digital services", min(total, 30)
    if elig["status"] == "ineligible":
        gated, total = "eligibility gate: hard requirement not met", min(total, 15)
    elif dl is not None and dl < 0:
        gated, total = "deadline passed", 0
    rc = cfg["relevance"]
    if v and not (rc.get("value_min", 0) <= v <= rc.get("value_max", float("inf"))):
        gated, total = f"value £{v:,.0f} outside £{rc.get('value_min', 0):,.0f}-£{rc.get('value_max', 0):,.0f} window", min(total, 20)
    return {"score": round(total, 1), "uncertainty": round(spread, 1), "gate": gated,
            "criteria": {k: {"value": round(c[k][0], 3), "weight": round(w[k], 3), "confidence": c[k][1], "why": c[k][2]} for k in w}}


def classify(opp, cfg, profile, rules=(), today=None, buyer_awards=0):
    """Full deterministic pipeline -> dict used for storage and for rule testing."""
    rel, hits = relevance(opp, cfg, rules)
    o = dict(opp, relevance=rel)
    o["category"] = category(o)
    elig = eligibility(o, profile, rules)
    econ = economics(o, cfg)
    s = score(o, cfg, profile, rules, elig, econ, buyer_awards, today)
    sc = cfg["scoring"]
    if elig["status"] == "ineligible":
        cls = "Ineligible"
    elif s["gate"] or rel < cfg["relevance"]["min_score"]:
        cls = "Unlikely to be suitable"
    elif elig["status"] == "requires_verification" and s["score"] >= sc["investigate_threshold"]:
        cls = "Requires investigation"
    elif s["score"] >= sc["strong_threshold"]:
        cls = "Strong candidate"
    elif s["score"] >= sc["possible_threshold"]:
        cls = "Possible candidate"
    else:
        cls = "Unlikely to be suitable"
    return {"classification": cls, "relevance": rel, "relevance_terms": hits, "category": o["category"],
            "eligibility": elig, "economics": econ, "score": s}


def next_action(cls, elig):
    return {
        "Strong candidate": "Read the full tender pack; confirm requirements; request a proposal draft.",
        "Possible candidate": "Skim documents; check scope fit and buyer history before committing bid time.",
        "Requires investigation": ("Verify: " + ", ".join(r["req"] for r in elig["requirements"] if r["met"] != "yes")[:120]
                                   if any(r["met"] != "yes" for r in elig["requirements"])
                                   else "Read the tender pack for requirements the notice doesn't show") + "; ask the buyer a clarification question if unclear.",
        "Unlikely to be suitable": "No action. Kept for award intelligence.",
        "Ineligible": "No bid unless the blocking requirement can be met: " + ", ".join(r["req"] for r in elig["requirements"] if r["met"] == "no" and r["hard"])[:120],
    }[cls]
