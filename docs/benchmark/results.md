# LLM benchmark results (2026-11-18 17:14 UTC)

Measured with `scripts/benchmark_llm.py` - same extracted content and prompt for every model.

| Model | Runs | Valid JSON | Median time (s) | Avg input tokens | Avg output tokens | Avg cost (USD) |
|---|---:|---:|---:|---:|---:|---:|
| gemini:gemini-3.5-flash-lite | 2 | 0/2 | - | - | - | - |
| gemini:gemini-3-flash-preview | 2 | 2/2 | 18.61 | 5091 | 4430 | 0.01584 |
| gemini:gemini-3.1-flash-lite | 2 | 1/2 | 9.15 | 3944 | 3076 | 0.0056 |
| gemini:gemini-3.6-flash | 2 | 0/2 | - | - | - | - |

## Output spot-check

- **gemini-3.5-flash-lite** on https://basecamp.com: failed - gemini returned HTTP 503: This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.
- **gemini-3-flash-preview** on https://basecamp.com: brand `Basecamp`, keyword `Project Management Software`, competitors Asana, Monday.com, Trello
- **gemini-3.1-flash-lite** on https://basecamp.com: brand `Basecamp`, keyword `project management platform`, competitors Asana, Trello, Monday.com
- **gemini-3.6-flash** on https://basecamp.com: failed - The model returned JSON but not an object.
- **gemini-3.5-flash-lite** on https://www.zoho.com/crm/: failed - gemini returned HTTP 503: This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.
- **gemini-3-flash-preview** on https://www.zoho.com/crm/: brand `Zoho`, keyword `Zoho CRM`, competitors Salesforce, HubSpot, Freshworks
- **gemini-3.1-flash-lite** on https://www.zoho.com/crm/: failed - gemini returned HTTP 503: This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.
- **gemini-3.6-flash** on https://www.zoho.com/crm/: failed - gemini request timed out.
