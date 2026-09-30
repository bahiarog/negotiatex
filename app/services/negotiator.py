import json, re, logging
from typing import Optional
import anthropic

logger = logging.getLogger(__name__)
_client = anthropic.Anthropic()
MODEL = "claude-sonnet-4-6"

COUNTER_OFFER_PROMPT = """You are an expert procurement negotiator. Based on the analysis of a supplier offer, generate a professional counter-offer package.

OFFER DETAILS:
Title: {title}
Category: {category}
Total: €{total:,.2f}
Supplier: {supplier}

ANALYSIS RESULTS:
Pricing Check (score {pricing_score}/100): {pricing_summary}
Savings Analysis (score {savings_score}/100): {savings_summary}
Identified savings potential: €{realistic_savings:,.0f} realistic / €{max_savings:,.0f} maximum
Top negotiation points:
{negotiation_points}

Terms issues:
{terms_issues}

Generate a comprehensive counter-offer package. Respond ONLY with valid JSON:
{{
  "target_price": <float — realistic counter-offer total>,
  "target_price_rationale": "<why this target is justified>",
  "reduction_percentage": <float — percentage reduction from original>,
  "priority_items": [
    {{"item": "<specific line item or service>", "original": <float>, "target": <float>, "argument": "<specific argument>"}}
  ],
  "negotiation_email": {{
    "subject": "<email subject line>",
    "body": "<full professional email in {language}, ready to send, ~300 words. Include specific price targets, factual arguments, professional tone. No placeholders — use actual numbers from the analysis.>"
  }},
  "talking_points": ["<3-5 key points for a negotiation call>"],
  "walk_away_price": <float — maximum acceptable price>,
  "best_alternative": "<BATNA — best alternative if supplier doesn't budge>",
  "negotiation_strategy": "<overall approach: aggressive/balanced/collaborative>",
  "expected_outcome": "<realistic expectation>"
}}"""

def _parse(text):
    cleaned = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("```").strip()
    try:
        return json.loads(cleaned)
    except:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            return json.loads(m.group())
        raise ValueError(f"Cannot parse JSON: {text[:200]}")

async def generate_counter_offer(
    title: str,
    category: str,
    total_net: float,
    supplier_name: str,
    analysis_checks: dict,
    language: str = "de",
) -> dict:
    pricing = analysis_checks.get("pricing", {}).get("result", {})
    savings = analysis_checks.get("savings", {}).get("result", {})
    terms = analysis_checks.get("terms", {}).get("result", {})

    pricing_score = analysis_checks.get("pricing", {}).get("score", 0)
    savings_score = analysis_checks.get("savings", {}).get("score", 0)

    realistic_savings = float(savings.get("realistic_savings_eur") or 0)
    max_savings = float(savings.get("max_savings_eur") or 0)

    neg_points = savings.get("negotiation_points", [])[:5]
    neg_points_text = "
".join(
        f"- {p.get('point','')} (saving: €{p.get('potential_saving',0):,.0f}, difficulty: {p.get('difficulty','')})"
        for p in neg_points
    )

    terms_issues = terms.get("critical", [])[:3]
    terms_text = "
".join(f"- {t.get('clause','')} → {t.get('recommendation','')}" for t in terms_issues) or "No critical issues found."

    lang_label = "German" if language == "de" else "English"

    prompt = COUNTER_OFFER_PROMPT.format(
        title=title,
        category=category,
        total=total_net,
        supplier=supplier_name or "the supplier",
        pricing_score=pricing_score,
        pricing_summary=pricing.get("summary", pricing.get("market_position", "See analysis.")),
        savings_score=savings_score,
        savings_summary=savings.get("negotiation_strategy", "See analysis."),
        realistic_savings=realistic_savings,
        max_savings=max_savings,
        negotiation_points=neg_points_text or "No specific points identified.",
        terms_issues=terms_text,
        language=lang_label,
    )

    try:
        resp = _client.messages.create(
            model=MODEL,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text
        result = _parse(raw)
        logger.info(f"Counter-offer generated for offer '{title}': target €{result.get('target_price', 0):,.2f}")
        return result
    except Exception as e:
        logger.error(f"Counter-offer generation failed: {e}")
        target = round(total_net * 0.85, 2)
        return {
            "target_price": target,
            "target_price_rationale": "10-15% reduction based on market analysis",
            "reduction_percentage": 15.0,
            "priority_items": [],
            "negotiation_email": {
                "subject": f"Verhandlungsanfrage: {title}",
                "body": f"Sehr geehrte Damen und Herren,

bezugnehmend auf Ihr Angebot {title} (€{total_net:,.2f}) möchten wir Ihnen einen Gegenvorschlag unterbreiten.

Nach unserer Analyse sehen wir Potenzial für eine Preisreduzierung auf €{target:,.2f}. Wir freuen uns auf Ihre Rückmeldung.

Mit freundlichen Grüßen",
            },
            "talking_points": ["Marktpreisvergleich zeigt Einsparpotenzial", "Langfristige Partnerschaft als Argument", "Volumenzusage als Verhandlungsmasse"],
            "walk_away_price": total_net,
            "best_alternative": "Alternative Anbieter anfragen",
            "negotiation_strategy": "balanced",
            "expected_outcome": f"Einigung bei ca. €{target:,.2f}",
        }
