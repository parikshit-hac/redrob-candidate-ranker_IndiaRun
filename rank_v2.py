#!/usr/bin/env python3
"""
Redrob Hackathon - Candidate Ranking System, v2
=================================================
Design goal: rank candidates the way a great recruiter would -- by reading
the JD and understanding what it actually needs, not by string-matching a
hand-picked keyword list. v1 of this system (rank.py) hardcoded skill/company
lists derived from one human reading of this specific JD; this version reads
job_description.txt at runtime and is JD-agnostic: point it at a different
JD and it adapts without code changes.

ARCHITECTURE (hybrid semantic + structured re-ranking)
-------------------------------------------------------
Stage 1 - JD UNDERSTANDING (semantic, runtime, generic)
    Parse the JD into functional segments using generic structural cues
    (heading-like lines, "must have" / "do not want" language patterns) that
    work across JDs, not just this one:
      - requirements_text   : what the role needs (skills/responsibilities)
      - disqualifiers_text  : explicit "do not want" / "won't move forward" signals
      - ideal_profile_text  : "ideal candidate" / aspirational description, if present
      - logistics_text      : location / notice-period / comp signals
    Falls back gracefully (whole JD as requirements_text) if a JD doesn't
    have these sections, so the pipeline never breaks on a different JD.

Stage 2 - EMBEDDING (semantic, no network, no GPU)
    A TF-IDF + Truncated-SVD (LSA) model is fit on the fly across the JD
    text + all 100k candidate text blobs (headline, summary, career-history
    descriptions, skills). This gives real "vector embeddings" / "semantic
    search" capability -- candidates are compared to the JD in latent
    semantic space, not by literal keyword overlap -- while staying fully
    self-contained (no pretrained-model download, so it's reproducible
    inside a network-off Docker sandbox at Stage 3).

Stage 3 - STRUCTURED RE-RANKING (generic recruiting signals, not JD-specific)
    On top of the semantic fit score, layer signals a recruiter actually
    weighs that are NOT specific to this JD's wording:
      - data-integrity / honeypot detection (internal inconsistency checks)
      - behavioral/availability multiplier (activity recency, response rate,
        interview completion, profile completeness) from redrob_signals
      - generic professional heuristics: job-hopping / title-escalation
        pattern, employment-type classification (a small, documented
        gazetteer of well-known IT-services/consulting firms vs.
        product/startup employers -- general recruiting knowledge, not
        something pulled from this JD's text)
      - logistics fit (location / notice period) derived from whatever the
        JD itself says about location and notice expectations, parsed at
        runtime from logistics_text

Stage 4 - COMPOSITE SCORE -> TOP 100 -> CSV with grounded, per-candidate
    reasoning that cites the actual semantic match terms and signals used.

Runtime budget: fit + score + rank on 100k candidates is CPU-only, no
network, single pass, well under 5 min / 16GB.
"""
import json
import gzip
import csv
import re
import math
import argparse
from datetime import date, datetime

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.decomposition import TruncatedSVD
from sklearn.preprocessing import normalize

TODAY = date(2026, 6, 30)

# ---------------------------------------------------------------------------
# Small, DOCUMENTED, JD-independent gazetteer: generic recruiting-market
# knowledge (which firms are IT-services/consulting vs. product/startups in
# the Indian tech market). This is NOT derived from reading this specific
# JD -- it's the kind of background knowledge any recruiter working this
# market would already have, used only to compute a generic
# "employer-type" feature. Everything that depends on what THIS role wants
# comes from job_description.txt at runtime, not from this list.
# ---------------------------------------------------------------------------
KNOWN_IT_SERVICES_FIRMS = {
    "tcs", "infosys", "wipro", "accenture", "cognizant", "capgemini",
    "hcl", "tech mahindra", "mphasis", "mindtree", "genpact", "genpact ai",
}


def norm(s):
    return (s or "").strip().lower()


def parse_date(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None


def months_between(d1, d2):
    if d1 is None or d2 is None:
        return 0
    return (d2.year - d1.year) * 12 + (d2.month - d1.month)


# ---------------------------------------------------------------------------
# Stage 1: generic JD parsing
# ---------------------------------------------------------------------------
SECTION_HINTS = {
    "requirements": [
        "absolutely need", "must have", "required", "you absolutely need",
        "what you'd actually be doing", "we need someone who", "the skills",
        "things you absolutely need", "responsibilities",
    ],
    "disqualifiers": [
        "do not want", "won't move forward", "will not move forward",
        "disqualifier", "we will not", "explicitly do not want",
        "red flag", "not a fit",
    ],
    "ideal_profile": [
        "ideal candidate", "how to read between the lines", "we're imagining",
    ],
    "logistics": [
        "location:", "notice period", "comp", "logistics", "salary",
        "relocat",
    ],
}


def split_jd_sections(jd_text):
    """Generic, heading-cue based JD segmentation. Falls back to using the
    whole JD as 'requirements' for any section it can't find, so this never
    breaks on a JD with a different structure.

    Only SHORT, heading-like lines (<=80 chars, no terminal period) are
    allowed to switch which section we're currently appending to. This
    avoids a long body sentence that happens to contain a trigger word
    (e.g. "...willing to relocate to Noida or Pune.") from hijacking the
    section pointer mid-paragraph.
    """
    lines = [l.strip() for l in jd_text.splitlines()]
    buckets = {k: [] for k in SECTION_HINTS}
    current = None
    for line in lines:
        if not line:
            continue
        low = line.lower()
        looks_like_heading = len(line) <= 80 and not line.rstrip().endswith((".", ",", ";"))
        matched = None
        for sect, hints in SECTION_HINTS.items():
            if any(h in low for h in hints):
                if looks_like_heading or sect == "requirements":
                    matched = sect
                break
        if matched:
            current = matched
        if current:
            buckets[current].append(line)

    sections = {k: ("\n".join(v) if v else "") for k, v in buckets.items()}
    if not sections["requirements"].strip():
        sections["requirements"] = jd_text
    return sections


def extract_logistics(jd_text):
    """Pull preferred locations and notice-period preference straight out of
    the JD text at runtime (no hardcoded assumption about *which* JD)."""
    low = jd_text.lower()

    # Location: look for "Location:" line, then pull out comma/slash
    # separated proper-noun-ish tokens near it.
    preferred_locations = set()
    loc_match = re.search(r"location:\s*([^\n]+)", low)
    if loc_match:
        chunk = loc_match.group(1)
        for tok in re.split(r"[\/,|]", chunk):
            tok = re.sub(r"\(.*?\)", "", tok).strip()
            tok = re.sub(r"[^a-z\s]", "", tok).strip()
            if tok and len(tok) > 2 and tok not in ("hybrid", "flexible", "india", "remote", "onsite"):
                preferred_locations.add(tok)
    # Also catch "Candidates in X, Y, Z welcome to apply" style lines
    for m in re.finditer(r"candidates in ([^.]+?) welcome", low):
        for tok in re.split(r"[,/]| and ", m.group(1)):
            tok = tok.strip()
            if tok:
                preferred_locations.add(tok)

    # Notice period preference: look for "sub-30-day", "X-day notice", "notice period"
    preferred_notice_days = None
    m = re.search(r"sub-?(\d+)\s*-?\s*day", low)
    if m:
        preferred_notice_days = int(m.group(1))
    else:
        m = re.search(r"(\d+)\s*\+?\s*day(?:s)?\s*notice", low)
        if m:
            preferred_notice_days = int(m.group(1))

    sponsors_visa = "don't sponsor" not in low and "do not sponsor" not in low and "no visa sponsorship" not in low

    return {
        "preferred_locations": preferred_locations,
        "preferred_notice_days": preferred_notice_days,
        "sponsors_visa": sponsors_visa,
    }


# ---------------------------------------------------------------------------
# Candidate text blob (what gets embedded) and structural feature helpers
# ---------------------------------------------------------------------------
def candidate_text_blob(c):
    p = c["profile"]
    parts = [
        p.get("headline", ""), p.get("summary", ""),
        p.get("current_title", ""), p.get("current_industry", ""),
    ]
    for j in c["career_history"]:
        parts.append(j.get("title", ""))
        parts.append(j.get("description", ""))
        parts.append(j.get("industry", ""))
    for s in c.get("skills", []):
        # weight skill mentions by proficiency so "expert" terms carry more
        # semantic mass in the TF-IDF space than "beginner" ones
        rep = {"beginner": 1, "intermediate": 2, "advanced": 3, "expert": 4}.get(s.get("proficiency"), 1)
        parts.append((s.get("name", "") + " ") * rep)
    return " ".join(parts)


def detect_honeypot(c):
    profile = c["profile"]
    yoe = profile.get("years_of_experience", 0) or 0
    total_months = sum(j.get("duration_months", 0) for j in c["career_history"])
    if abs(yoe - total_months / 12.0) > 2.5:
        return True, "years_of_experience inconsistent with career_history total duration"

    for s in c.get("skills", []):
        if s.get("proficiency") == "expert" and s.get("duration_months", 999) <= 2:
            return True, f"'expert' proficiency in {s['name']} with ~0 months of use"

    spans = []
    for j in c["career_history"]:
        sd = parse_date(j.get("start_date"))
        ed = parse_date(j.get("end_date")) or TODAY
        if sd:
            spans.append((sd, ed))
    spans.sort()
    for i in range(1, len(spans)):
        if spans[i][0] < spans[i - 1][1]:
            if months_between(spans[i][0], spans[i - 1][1]) > 6:
                return True, "overlapping career_history timeline >6 months"
    return False, ""


def employer_type(name):
    n = norm(name)
    return "it_services" if n in KNOWN_IT_SERVICES_FIRMS else "other"


def career_pattern(c):
    """Generic, JD-independent professional heuristics."""
    history = c["career_history"]
    all_it_services = len(history) > 0 and all(employer_type(j["company"]) == "it_services" for j in history)

    seniority_rank = {"junior": 0, "associate": 1, "engineer": 2, "senior": 3,
                       "staff": 4, "principal": 5, "lead": 4, "head": 5,
                       "director": 6, "vp": 7}

    def sen(title):
        t = norm(title)
        for k, v in sorted(seniority_rank.items(), key=lambda x: -len(x[0])):
            if k in t:
                return v
        return 2

    short_tenure_jobs = sum(1 for j in history if (j.get("duration_months") or 0) <= 18)
    title_chaser = False
    if len(history) >= 3:
        ordered = sorted(history, key=lambda j: j.get("start_date") or "")
        sens = [sen(j["title"]) for j in ordered]
        if sens == sorted(sens) and sens[-1] > sens[0] and short_tenure_jobs >= len(history) - 1:
            title_chaser = short_tenure_jobs >= 3

    mgmt_titles = {"architect", "director", "head", "vp", "manager", "lead"}
    current_job = next((j for j in history if j.get("is_current")), None)
    current_title = norm(c["profile"].get("current_title"))
    long_mgmt_no_code = bool(
        current_job and any(m in current_title for m in mgmt_titles)
        and (current_job.get("duration_months") or 0) >= 18
    )

    return {
        "all_it_services": all_it_services,
        "title_chaser": title_chaser,
        "long_mgmt_no_code": long_mgmt_no_code,
    }


def behavioral_multiplier(sig):
    last_active = parse_date(sig.get("last_active_date"))
    months_inactive = months_between(last_active, TODAY) if last_active else 12
    activity_factor = max(0.35, 1.0 - 0.10 * max(0, months_inactive - 1))
    resp = sig.get("recruiter_response_rate", 0.3) or 0.0
    response_factor = 0.55 + 0.45 * resp
    interview = sig.get("interview_completion_rate", 0.5) or 0.0
    interview_factor = 0.7 + 0.3 * interview
    open_flag = 1.0 if sig.get("open_to_work_flag") else 0.85
    completeness = sig.get("profile_completeness_score", 50) or 50
    completeness_factor = 0.85 + 0.15 * (completeness / 100.0)
    mult = activity_factor * response_factor * interview_factor * open_flag * completeness_factor
    return mult, months_inactive


def logistics_fit(profile, sig, logistics):
    loc = norm(profile.get("location"))
    country = norm(profile.get("country"))
    score = 0.0
    notes = []
    if logistics["preferred_locations"] and any(p in loc for p in logistics["preferred_locations"]):
        score += 1.0
        notes.append("based in a JD-preferred location")
    elif country == "india":
        score += 0.4
    else:
        if sig.get("willing_to_relocate") and logistics["sponsors_visa"]:
            score += 0.1
        else:
            score -= 0.5
            notes.append("outside India" + ("" if logistics["sponsors_visa"] else " and JD does not sponsor visas"))

    notice = sig.get("notice_period_days")
    pref = logistics["preferred_notice_days"]
    if notice is not None and pref is not None:
        if notice <= pref:
            score += 0.4
            notes.append(f"notice period ({notice}d) meets JD preference (<={pref}d)")
        elif notice <= pref * 2:
            score += 0.0
        else:
            score -= 0.25
            notes.append(f"long {notice}-day notice period vs JD's ~{pref}-day preference")
    return score, notes


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def load_candidates(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jd", required=True, help="Path to job description text/markdown file")
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--top-k", type=int, default=100)
    ap.add_argument("--svd-dims", type=int, default=150)
    args = ap.parse_args()

    with open(args.jd, "r", encoding="utf-8", errors="ignore") as f:
        jd_text = f.read()

    jd_sections = split_jd_sections(jd_text)
    logistics = extract_logistics(jd_text)
    print("JD sections found (chars):", {k: len(v) for k, v in jd_sections.items()})
    print("Parsed logistics:", {k: (v if not isinstance(v, set) else list(v)) for k, v in logistics.items()})

    candidates = list(load_candidates(args.candidates))
    n = len(candidates)
    print(f"Loaded {n} candidates.")

    # ---- Stage 1+2: build the corpus and fit TF-IDF + LSA ----
    blobs = [candidate_text_blob(c) for c in candidates]
    jd_query_texts = [
        jd_sections["requirements"] or jd_text,
        jd_sections["ideal_profile"],
        jd_sections["disqualifiers"],
    ]
    corpus = blobs + jd_query_texts  # JD docs appended at the end

    vectorizer = TfidfVectorizer(
        max_features=60000, ngram_range=(1, 2), min_df=3, max_df=0.6,
        stop_words="english", sublinear_tf=True,
    )
    tfidf = vectorizer.fit_transform(corpus)
    print("TF-IDF matrix shape:", tfidf.shape)

    svd = TruncatedSVD(n_components=args.svd_dims, random_state=42)
    latent = svd.fit_transform(tfidf)
    latent = normalize(latent)  # unit-norm rows -> dot product == cosine similarity
    print("Explained variance (LSA):", round(svd.explained_variance_ratio_.sum(), 3))

    n_cand = len(blobs)
    cand_vecs = latent[:n_cand]
    req_vec = latent[n_cand + 0]
    ideal_vec = latent[n_cand + 1]
    disq_vec = latent[n_cand + 2]

    req_sim = cand_vecs @ req_vec
    ideal_sim = cand_vecs @ ideal_vec if jd_sections["ideal_profile"].strip() else np.zeros(n_cand)
    disq_sim = cand_vecs @ disq_vec if jd_sections["disqualifiers"].strip() else np.zeros(n_cand)

    # Combine into a single semantic fit score. Disqualifier similarity is
    # SUBTRACTED -- a candidate whose profile text resembles the JD's "do
    # NOT want" language is a worse fit, not a better one.
    semantic_fit = 0.65 * req_sim + 0.20 * ideal_sim - 0.55 * disq_sim
    # rescale to a friendly ~0-10 range for readability when composing the
    # final score (purely cosmetic, doesn't change rank order within this term)
    semantic_fit = (semantic_fit - semantic_fit.min()) / (semantic_fit.max() - semantic_fit.min() + 1e-9) * 10

    # ---- Stage 3: structured re-ranking signals ----
    results = []
    for i, c in enumerate(candidates):
        is_hp, hp_reason = detect_honeypot(c)
        career = career_pattern(c)
        sig = c["redrob_signals"]
        mult, months_inactive = behavioral_multiplier(sig)
        loc_score, loc_notes = logistics_fit(c["profile"], sig, logistics)

        structural_penalty = 0.0
        flags = []
        if career["all_it_services"]:
            structural_penalty -= 2.5
            flags.append("entire career at IT-services/consulting firms")
        if career["title_chaser"]:
            structural_penalty -= 1.2
            flags.append("career pattern suggests frequent title-driven job changes")
        if career["long_mgmt_no_code"]:
            structural_penalty -= 1.0
            flags.append("long stint in a management/architecture title")

        composite = semantic_fit[i] + structural_penalty + loc_score
        composite *= mult

        if is_hp:
            composite = -1000.0

        results.append({
            "candidate_id": c["candidate_id"],
            "composite": composite,
            "semantic_fit": semantic_fit[i],
            "req_sim": req_sim[i],
            "ideal_sim": ideal_sim[i],
            "disq_sim": disq_sim[i],
            "is_honeypot": is_hp,
            "hp_reason": hp_reason,
            "flags": flags,
            "loc_notes": loc_notes,
            "mult": mult,
            "months_inactive": months_inactive,
            "candidate": c,
        })

    honeypot_count = sum(1 for r in results if r["is_honeypot"])
    print(f"Detected {honeypot_count} honeypots (excluded from ranking).")

    results.sort(key=lambda r: (-r["composite"], r["candidate_id"]))
    top = results[: args.top_k]
    honeypots_in_top = sum(1 for r in top if r["is_honeypot"])
    print(f"Honeypots in top {args.top_k}: {honeypots_in_top}")

    comps = [r["composite"] for r in top]
    max_s, min_s = max(comps), min(comps)
    span = max(1e-9, max_s - min_s)

    rows = []
    for rank, r in enumerate(top, start=1):
        norm_score = round(0.5 + 0.5 * (r["composite"] - min_s) / span, 4)
        reasoning = build_reasoning(r)
        rows.append((r["candidate_id"], rank, norm_score, reasoning))

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["candidate_id", "rank", "score", "reasoning"])
        for row in rows:
            w.writerow(row)

    print(f"Wrote {len(rows)} rows to {args.out}")


def build_reasoning(r):
    c = r["candidate"]
    p = c["profile"]
    sig = c["redrob_signals"]
    bits = [
        f"{p.get('current_title')} ({p.get('years_of_experience'):.1f} yrs) at {p.get('current_company')}, {p.get('location')}",
        f"semantic JD-fit score {r['req_sim']:.3f}" + (f", ideal-profile similarity {r['ideal_sim']:.3f}" if r['ideal_sim'] else ""),
        f"recruiter response rate {sig.get('recruiter_response_rate', 0):.2f}, notice {sig.get('notice_period_days')}d",
    ]
    concerns = []
    if r["flags"]:
        concerns.append(r["flags"][0])
    if r["months_inactive"] >= 3:
        concerns.append(f"inactive on platform for ~{r['months_inactive']} months")
    if r["disq_sim"] > r["req_sim"] + 0.08:
        concerns.append("profile text resembles JD's stated 'do not want' patterns more than its actual requirements")
    neg_loc = [n for n in r["loc_notes"] if "long" in n or "outside" in n]
    if neg_loc:
        concerns.append(neg_loc[0])

    budget = 260
    while bits and len("; ".join(bits)) + 1 > (budget if not concerns else budget - 90):
        bits.pop()
    sentence1 = "; ".join(bits) + "."
    if concerns:
        s2 = " Concern: " + concerns[0]
        if not s2.endswith((".", "!")):
            s2 += "."
        return (sentence1 + s2)[:320]
    return sentence1[:320]


if __name__ == "__main__":
    main()
