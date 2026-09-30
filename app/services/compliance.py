"""
Compliance Agent - validates AI outputs for GDPR compliance and quality.
Uses claude-haiku for speed/cost. Acts as gate after analysis, before output.
"""
import re, logging
import anthropic

logger = logging.getLogger(__name__)
MODEL = "claude-haiku-4-5-20251001"

PII_PATTERNS = [
    r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',  # emails
    r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b',  # phone numbers
    r'\b(IBAN|BIC|SWIFT)[\s:]?[A-Z0-9]{8,34}\b',  # banking
]

def _detect_pii(text: str) -> list[str]:
    found = []
    for pattern in PII_PATTERNS:
        matches = re.findall(pattern, text, re.IGNORECASE)
        found.extend(matches)
    return found

async def validate_analysis(analysis_result: dict, offer_summary: str) -> dict:
    """
    Validate AI analysis output for compliance issues.
    Returns dict with: passed, flags, risk_level, redaction_needed
    """
    import json

    # Quick regex PII scan first
    result_str = json.dumps(analysis_result)
    pii_found = _detect_pii(result_str)

    flags = []
    if pii_found:
        flags.append(f"PII detected in output: {len(pii_found)} instance(s)")

    # Claude Haiku compliance review
    try:
        client = anthropic.Anthropic()
        prompt = f"""You are a GDPR compliance officer reviewing an AI-generated procurement analysis output.

OFFER CONTEXT: {offer_summary[:500]}

ANALYSIS OUTPUT (excerpt):
{result_str[:3000]}

Review for:
1. Any supplier-identifiable PII that should not be in the output (names, emails, addresses, tax IDs)
2. Whether savings/price recommendations are plausible (score 0-100 should match findings)
3. Any legally problematic statements (definitive legal advice, false guarantees)

Respond ONLY with JSON:
{{"passed": true/false, "flags": ["issue1", "issue2"], "risk_level": "low/medium/high", "recommendation": "brief action"}}"""

        resp = client.messages.create(
            model=MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        text = resp.content[0].text.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])
            # Merge regex findings
            if pii_found:
                result["passed"] = False
                result.setdefault("flags", []).extend(flags)
            return result
    except Exception as e:
        logger.warning(f"Compliance agent error: {e}")

    # Fallback: basic pass if no PII found
    return {
        "passed": len(pii_found) == 0,
        "flags": flags,
        "risk_level": "low" if not pii_found else "medium",
        "recommendation": "Manual review recommended" if pii_found else "Auto-approved"
    }
