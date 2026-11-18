SYSTEM_PROMPT = """You are a senior digital marketing and SEO analyst.
You receive text extracted from a company's website and produce a structured business analysis.

Rules:
- Base every statement on the supplied website content. Do not produce generic filler that could apply to any company.
- Use the brand's real name, real products/services and real wording from the site wherever possible.
- Services: list services actually offered on the website and set "source" to "website". Only if the site shows fewer than five, add clearly useful related services and set "source" to "inferred".
- Competitors: suggest 3-5 real, existing companies that compete in the same market and region. Only include a "website" if you are confident it is the competitor's real homepage domain, otherwise use null. Set "confidence" to high, medium or low honestly. Never invent companies.
- Comparison: every row's "competitors" object must have one entry for EACH competitor, keyed by the exact competitor name used in "competitors".
- Search queries: write exactly 5 queries real customers would type or search. Platforms must only be chosen from: Google, Facebook, Instagram, LinkedIn, X. Do NOT include search volumes, rankings or any numbers you cannot know.
- SEO: the recommended title should be 50-60 characters and include the primary keyword. The meta description should be 140-160 characters, human friendly, and include the primary keyword.
- If the location is not stated on the website, set "geographic_focus" to null instead of guessing.
- Write in English, concise and specific. Return ONLY valid JSON matching the requested structure - no markdown, no comments."""


RESPONSE_TEMPLATE = """{
  "brand": {
    "name": "string - brand name as shown on the website",
    "category": "string - main business category",
    "summary": "string - 1-2 sentence business summary"
  },
  "analysis": {
    "competitor_analysis": {
      "your_brand": {
        "name": "string",
        "overview": "string - 2-3 sentences",
        "category": "string",
        "main_products_services": ["string"],
        "target_market": "string",
        "key_strengths": ["string"]
      },
      "competitors": [
        {
          "name": "string",
          "website": "https://domain.com or null",
          "main_service": "string",
          "target_market": "string",
          "strengths": ["string"],
          "weaknesses": ["string - gaps the brand could exploit"],
          "confidence": "high | medium | low"
        }
      ],
      "comparison": [
        {"aspect": "Main Service", "your_brand": "string", "competitors": {"<competitor name>": "string"}},
        {"aspect": "Target Market", "your_brand": "string", "competitors": {"<competitor name>": "string"}},
        {"aspect": "Key Strength", "your_brand": "string", "competitors": {"<competitor name>": "string"}},
        {"aspect": "Pricing / Positioning", "your_brand": "string", "competitors": {"<competitor name>": "string"}},
        {"aspect": "Opportunity", "your_brand": "string", "competitors": {"<competitor name>": "string"}}
      ],
      "competitive_advantages": ["string - what the brand does better"],
      "opportunities": ["string - concrete ways to improve market positioning"]
    },
    "target_search_queries": [
      {
        "query": "string",
        "platforms": ["Google | Facebook | Instagram | LinkedIn | X"],
        "intent": "string - e.g. Service Search, Comparison, Inspiration, B2B Search",
        "business_relevance": "string - why this query matters for this business",
        "target_audience": "string - who searches this"
      }
    ],
    "seo_improvements": {
      "primary_keyword": "string",
      "secondary_keywords": ["string"],
      "long_tail_keywords": ["string"],
      "business_keywords": ["string"],
      "recommended_title": "string",
      "title_rationale": "string - why this title is better than the current one",
      "meta_description": "string",
      "supporting_content": [
        {"type": "Service landing page | Industry page | FAQ | Blog topic | Location page | Article", "title": "string", "purpose": "string"}
      ]
    },
    "top_services": [
      {
        "service_name": "string",
        "description": "string",
        "target_industry": "string",
        "target_customer": "string",
        "main_benefit": "string",
        "source": "website | inferred"
      }
    ],
    "target_audience": {
      "primary_audience": ["string"],
      "secondary_audience": ["string"],
      "industry_segments": ["string"],
      "business_types": ["string"],
      "customer_profiles": ["string - short persona description"],
      "geographic_focus": "string or null",
      "customer_needs": ["string - needs and challenges"],
      "customer_interests": ["string"],
      "recommended_segments": [
        {"audience": "string", "business_type": "string", "requirement": "string"}
      ]
    }
  }
}"""


def build_user_prompt(website_text: str, seo_issues: list[str]) -> str:
    issues = "\n".join(f"- {issue}" for issue in seo_issues) or "- none detected"
    return f"""Analyse the following website.

=== WEBSITE CONTENT START ===
{website_text}
=== WEBSITE CONTENT END ===

Technical SEO issues already detected on the homepage (use them when writing SEO advice):
{issues}

Requirements:
- exactly 5 target_search_queries
- exactly 5 top_services
- 3 to 5 competitors
- 5 to 8 secondary_keywords, 4 to 6 long_tail_keywords, 4 to 6 supporting_content items
- 3 to 5 recommended_segments

Return a JSON object with exactly this structure:
{RESPONSE_TEMPLATE}"""


def build_repair_prompt(previous_json: str, errors: str) -> str:
    return f"""Your previous JSON did not match the required structure.

Validation errors:
{errors}

Previous JSON:
{previous_json}

Fix the errors and return the complete corrected JSON object only, using this structure:
{RESPONSE_TEMPLATE}"""
