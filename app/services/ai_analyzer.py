import json, re, logging
from typing import Optional
import anthropic

logger = logging.getLogger(__name__)
_client = anthropic.Anthropic()
MODEL = "claude-opus-4-5"

def _pricing_prompt(text, category, total):
    return f"""You are a senior procurement specialist. Analyze this supplier offer and evaluate the pricing.
Category: {category} | Total: €{total:,.2f}
Document: {text[:4000]}
Respond ONLY with valid JSON:
{{"score":<0-100>,"verdict":"<fair|below_market|overpriced>","confidence":"<low|medium|high>","market_position":"<assessment>","overpriced_items":[{{"item":"","issue":"","estimated_saving":0}}],"fair_items":[""],"benchmarks":[{{"metric":"","offer_value":"","market_range":"","assessment":"<over|fair|under>"}}],"summary":"<2-3 sentences>"}}"""

def _savings_prompt(text, category, total):
    return f"""You are an expert procurement negotiator. Identify every savings opportunity.
Category: {category} | Total: €{total:,.2f}
Document: {text[:4000]}
Respond ONLY with valid JSON:
{{"score":<0-100>,"realistic_savings_eur":<float>,"max_savings_eur":<float>,"savings_pct":<float>,"negotiation_points":[{{"point":"","rationale":"","potential_saving":0,"difficulty":"<easy|medium|hard>","suggested_email_text":""}}],"quick_wins":[""],"negotiation_strategy":"","walk_away_point":"","best_alternative":""}}"""

def _terms_prompt(text, category):
    return f"""You are a contract lawyer specializing in procurement. Review terms and conditions for risks.
Category: {category}
Document: {text[:4000]}
Respond ONLY with valid JSON:
{{"score":<0-100>,"critical":[{{"clause":"","risk":"","recommendation":""}}],"warnings":[{{"clause":"","note":"","suggested_change":""}}],"acceptable":[""],"missing_clauses":[""],"overall_assessment":"<2-3 sentences>"}}"""

def _general_prompt(text, category, total, questionnaire):
    ctx = f"\nClient context: {json.dumps(questionnaire)}" if questionnaire else ""
    return f"""You are a senior procurement consultant. Provide comprehensive analysis.
Category: {category} | Total: €{total:,.2f}{ctx}
Document: {text[:3000]}
Respond ONLY with valid JSON:
{{"score":<0-100>,"executive_summary":"","strengths":[""],"weaknesses":[""],"recommendation":"<accept|negotiate|reject>","recommendation_reason":"","next_steps":[""],"risk_level":"<low|medium|high>","supplier_leverage":""}}"""

def _parse(text):
    cleaned = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("```").strip()
    try:
        return json.loads(cleaned)
    except:
        m = re.search(r'\{.*\}', cleaned, re.DOTALL)
        if m: return json.loads(m.group())
        raise ValueError(f"Cannot parse JSON: {text[:200]}")

def _counts(result, check_type):
    c, w, o = 0, 0, 0
    if check_type == "terms":
        c = len(result.get("critical", [])); w = len(result.get("warnings", [])); o = len(result.get("acceptable", []))
    elif check_type == "pricing":
        items = result.get("overpriced_items", [])
        c = sum(1 for i in items if (i.get("estimated_saving") or 0) > 5000)
        w = len(items) - c; o = len(result.get("fair_items", []))
    elif check_type == "savings":
        pts = result.get("negotiation_points", [])
        c = sum(1 for p in pts if p.get("difficulty") == "easy")
        w = sum(1 for p in pts if p.get("difficulty") == "medium")
        o = sum(1 for p in pts if p.get("difficulty") == "hard")
    elif check_type == "general":
        r = result.get("risk_level", "medium")
        if r == "high": c = 1
        elif r == "medium": w = 1
        else: o = 1
    return c, w, o

async def run_analysis(offer_text, category, total_net, check_types=None, questionnaire=None):
    if check_types is None:
        check_types = ["pricing", "savings", "terms", "general"]
    results = {}
    for ct in check_types:
        try:
            if ct == "pricing": prompt = _pricing_prompt(offer_text, category, total_net)
            elif ct == "savings": prompt = _savings_prompt(offer_text, category, total_net)
            elif ct == "terms": prompt = _terms_prompt(offer_text, category)
            elif ct == "general": prompt = _general_prompt(offer_text, category, total_net, questionnaire)
            else: continue
            resp = _client.messages.create(model=MODEL, max_tokens=2000, messages=[{"role":"user","content":prompt}])
            raw = resp.content[0].text
            parsed = _parse(raw)
            score = max(0, min(100, int(parsed.get("score", 50))))
            c, w, o = _counts(parsed, ct)
            results[ct] = {"score": score, "result": parsed, "critical_count": c, "warning_count": w, "ok_count": o, "raw_response": raw}
            logger.info(f"AI check '{ct}' done — score: {score}")
        except Exception as e:
            logger.error(f"AI check '{ct}' failed: {e}")
            results[ct] = {"score": 0, "result": {"error": str(e)}, "critical_count": 0, "warning_count": 0, "ok_count": 0, "raw_response": ""}
    weights = {"pricing": 0.35, "savings": 0.35, "terms": 0.20, "general": 0.10}
    tw = sum(weights.get(ct, 0.25) for ct in results)
    agg = int(sum(results[ct]["score"] * weights.get(ct, 0.25) for ct in results) / tw) if tw else 0
    return {"checks": results, "aggregate_score": agg}

def extract_total_savings(analysis_result):
    r = analysis_result.get("checks", {}).get("savings", {}).get("result", {})
    return float(r.get("realistic_savings_eur") or 0), float(r.get("max_savings_eur") or 0)
