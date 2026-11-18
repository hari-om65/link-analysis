// Small vanilla front end for POST /link-analysis.
// Everything coming back from the API is inserted with textContent (never innerHTML),
// because it contains text taken from third-party websites.

(function () {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const form = $("#analyse-form");
  const input = $("#url");
  const submitBtn = $("#submit");
  let lastResult = null;
  let timerId = null;

  // ---------------------------------------------------------------------------
  // helpers

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    if (props) {
      for (const [key, value] of Object.entries(props)) {
        if (value === undefined || value === null || value === false) continue;
        if (key === "class") node.className = value;
        else if (key === "text") node.textContent = value;
        else node.setAttribute(key, value);
      }
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function safeUrl(value) {
    try {
      const url = new URL(value);
      return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
    } catch (_) {
      return null;
    }
  }

  function link(href, text) {
    const safe = safeUrl(href);
    return safe ? el("a", { href: safe, target: "_blank", rel: "noopener noreferrer", text: text || safe }) : el("span", { text: text || "" });
  }

  function list(items) {
    if (!items || !items.length) return el("p", { class: "muted", text: "None found." });
    return el("ul", { class: "clean" }, items.map((item) => el("li", { text: item })));
  }

  function chips(items, extraClass) {
    return el("div", { class: "chips" + (extraClass ? " " + extraClass : "") }, (items || []).map((item) => el("span", { text: item })));
  }

  function table(headers, rows) {
    return el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        null,
        el("thead", null, el("tr", null, headers.map((h) => el("th", { text: h })))),
        el("tbody", null, rows.map((cells) => el("tr", null, cells.map((cell) => el("td", null, cell)))))
      )
    );
  }

  function card(title, ...children) {
    return el("div", { class: "card" }, title ? el("h3", { text: title }) : null, ...children);
  }

  function badge(text, kind) {
    return el("span", { class: "badge" + (kind ? " " + kind : ""), text });
  }

  function formatMs(ms) {
    if (ms === null || ms === undefined) return "-";
    return ms < 1000 ? ms + " ms" : (ms / 1000).toFixed(1) + " s";
  }

  // ---------------------------------------------------------------------------
  // progress / errors

  function explain(text) {
    return el("p", { class: "explain" }, el("span", { text }));
  }

  function startProgress(url) {
    $("#progress-url").textContent = url;
    $("#intro").hidden = true;
    $("#progress").hidden = false;
    const started = Date.now();
    const steps = document.querySelectorAll(".steps li");
    // Rough timings - the API is a single request, so this is only an indication
    const schedule = [0, 1, 7, 30];
    const tick = () => {
      const seconds = Math.floor((Date.now() - started) / 1000);
      $("#timer").textContent = seconds + "s";
      steps.forEach((step, index) => {
        const reached = seconds >= schedule[index];
        const next = index + 1 < schedule.length && seconds >= schedule[index + 1];
        step.classList.toggle("done", reached && next);
        step.classList.toggle("active", reached && !next);
      });
    };
    tick();
    timerId = setInterval(tick, 500);
  }

  function stopProgress() {
    clearInterval(timerId);
    $("#progress").hidden = true;
  }

  const ERROR_TITLES = {
    invalid_url: "That doesn't look like a valid URL",
    blocked_url: "This address can't be analysed",
    website_access_denied: "The website blocked our request",
    website_not_found: "Page not found",
    dns_resolution_failed: "Domain not found",
    website_timeout: "The website took too long to respond",
    content_not_usable: "No readable content on this page",
    rate_limited: "Slow down a little",
    llm_not_configured: "Analysis service not configured",
  };

  const ERROR_HELP = {
    invalid_url: "Type a website address like razorpay.com or https://www.example.com.",
    blocked_url: "Only public websites can be analysed. Local or internal network addresses are blocked for security.",
    website_access_denied: "This website doesn't allow automated visitors. Try a different website.",
    website_not_found: "Check the link for typos, or try the homepage instead.",
    dns_resolution_failed: "Check the spelling of the domain name.",
    website_timeout: "The website may be slow or down right now. Try again in a minute.",
    content_not_usable: "Try the company's homepage instead of a file or an empty page.",
    rate_limited: "There is a limit on analyses per minute. Wait a moment and try again.",
    llm_error: "The analysis service is busy right now. Wait a minute and try again.",
    llm_timeout: "The analysis service is busy right now. Wait a minute and try again.",
  };

  function showError(code, message) {
    $("#error-title").textContent = ERROR_TITLES[code] || "Something went wrong";
    $("#error-message").textContent = message || "Please try again.";
    $("#error-help").textContent = ERROR_HELP[code] ? "What you can do: " + ERROR_HELP[code] : "";
    $("#error").hidden = false;
  }

  // ---------------------------------------------------------------------------
  // rendering

  function renderBrand(data) {
    const brand = data.brand;
    const meta = data.meta;
    $("#brand-name").textContent = brand.name;
    $("#brand-category").textContent = brand.category;
    $("#brand-summary").textContent = brand.summary;

    const website = $("#brand-website");
    website.textContent = data.website;
    website.href = safeUrl(data.website) || "#";

    const social = $("#brand-social");
    social.replaceChildren(
      ...Object.entries(brand.social_profiles || {}).map(([platform, url]) => link(url, platform))
    );

    const contacts = [...(brand.contact_emails || []), ...(brand.contact_phones || [])];
    $("#brand-contacts").textContent = contacts.length ? "Contact: " + contacts.join(" · ") : "";

    const audit = data.analysis.seo_improvements.current_seo_audit || {};
    const flags = [];
    if (meta.cached) flags.push("this site was analysed in the last hour, so the saved report was shown instantly at no cost");
    if (meta.used_fallback) flags.push("backup model used");
    if (audit.rendered_with_javascript) flags.push("site opened in a browser (JavaScript site)");

    const stats = [
      ["Time taken", meta.cached ? "Instant (saved)" : formatMs(meta.total_duration_ms), null, "How long the whole analysis took"],
      ["Cost", meta.cached ? "$0 (saved)" : meta.estimated_cost_usd != null ? "$" + meta.estimated_cost_usd.toFixed(4) : "-", null, "What this analysis cost in model usage"],
      ["Pages read", String(meta.pages_analyzed.length), null, meta.pages_analyzed.join("\n")],
      ["Model", meta.llm_model, null, "The language model that wrote the report"],
    ];
    if (flags.length) stats.push(["Good to know", flags.join(" · "), "wide"]);

    $("#meta").replaceChildren(
      ...stats.map(([label, value, cls, tip]) =>
        el("div", { class: cls, title: tip }, el("dt", { text: label }), el("dd", { text: value }))
      )
    );
  }

  function renderGlance(data) {
    const a = data.analysis;
    const audit = a.seo_improvements.current_seo_audit;
    const rows = [
      ["Main competitors", a.competitor_analysis.competitors.slice(0, 3).map((c) => c.name).join(", ") || "None identified"],
      ["Biggest opportunity", a.competitor_analysis.opportunities[0] || "-"],
      ["Top search query", a.target_search_queries[0] ? `“${a.target_search_queries[0].query}”` : "-"],
      ["Main keyword to rank for", a.seo_improvements.primary_keyword],
      ["SEO problems found", audit ? `${audit.issues.length} (see section 3)` : "-"],
      ["Main customers", a.target_audience.primary_audience.slice(0, 3).join(", ") || "-"],
    ];
    $("#glance").replaceChildren(...rows.map(([label, value]) => el("div", null, el("dt", { text: label }), el("dd", { text: value }))));
  }

  function renderCompetitors(section) {
    const you = section.your_brand;
    const confidenceKind = { high: "good", medium: "warn", low: "bad" };

    const competitorCards = section.competitors.map((c) =>
      el(
        "div",
        { class: "item" },
        el(
          "div",
          { class: "item-head" },
          el("strong", { text: c.name }),
          badge(c.confidence + " confidence", confidenceKind[c.confidence])
        ),
        c.website
          ? el("p", { class: "small" }, link(c.website, c.website.replace(/^https?:\/\//, "")), " ",
              c.website_verified === true ? badge("site verified", "good") : c.website_verified === false ? badge("site not reachable", "bad") : null)
          : null,
        el("p", { text: c.main_service }),
        el("p", { class: "muted small", text: c.target_market }),
        el("div", { class: "label", text: "Strengths" }),
        list(c.strengths),
        el("div", { class: "label", text: "Weaknesses / gaps" }),
        list(c.weaknesses)
      )
    );

    const competitorNames = section.competitors.map((c) => c.name);
    const comparisonRows = section.comparison.map((row) => [
      el("strong", { text: row.aspect }),
      row.your_brand,
      ...competitorNames.map((name) => row.competitors[name] || "-"),
    ]);

    return [
      explain("Who this business competes with and how it compares. Competitors are suggested by the model and their websites are checked to exist — \"confidence\" shows how sure it is."),
      card(
        "This business",
        el("p", { text: you.overview }),
        el("div", { class: "grid-2" },
          el("div", null, el("div", { class: "label", text: "Products & services" }), list(you.main_products_services)),
          el("div", null, el("div", { class: "label", text: "Key strengths" }), list(you.key_strengths))
        ),
        el("div", { class: "label", text: "Target market" }),
        el("p", { text: you.target_market })
      ),
      card("Competitors", el("div", { class: "grid" }, competitorCards)),
      comparisonRows.length ? card("Side-by-side comparison", table(["", you.name, ...competitorNames], comparisonRows)) : null,
      el("div", { class: "grid-2" },
        card("Where it is stronger than competitors", list(section.competitive_advantages)),
        card("Opportunities to grow", list(section.opportunities))
      ),
      section.research_note ? el("p", { class: "note", text: section.research_note }) : null,
    ];
  }

  function renderQueries(queries) {
    return [
      explain("Five things a potential customer might type or search to find a business like this, and on which platforms. \"Intent\" is what the person is trying to do."),
      card(
        "Top 5 target search queries",
        table(
          ["Search query", "Where people search", "Intent", "Why it matters", "Who searches"],
          queries.map((q) => [
            el("strong", { text: q.query }),
            el("div", { class: "chips" }, q.platforms.map((p) => el("span", { text: p }))),
            q.intent,
            q.business_relevance,
            q.target_audience,
          ])
        ),
        el("p", { class: "note", text: "Queries are relevance-based suggestions. No search volume or ranking data is shown, because none is measured." })
      ),
    ];
  }

  function renderSeo(seo) {
    const audit = seo.current_seo_audit;
    const blocks = [
      explain("SEO means getting found on Google. Below: the words to target, a better page title and description (what shows in Google results), and a technical check-up of the website."),
      card(
        "Keywords to target",
        el("div", { class: "label", text: "Main keyword" }),
        chips([seo.primary_keyword], "primary"),
        el("div", { class: "label", text: "Related keywords" }),
        chips(seo.secondary_keywords),
        el("div", { class: "label", text: "Longer, more specific searches" }),
        chips(seo.long_tail_keywords),
        el("div", { class: "label", text: "Business & product terms" }),
        chips(seo.business_keywords)
      ),
      card(
        "Google title & description (current vs. suggested)",
        el("div", { class: "compare" },
          el("div", { class: "box" },
            el("div", { class: "label", text: "Current title" + (audit ? ` (${audit.title_length} chars)` : "") }),
            el("p", { text: (audit && audit.current_title) || "—" })),
          el("div", { class: "box new" },
            el("div", { class: "label", text: `Recommended title (${seo.recommended_title.length} chars)` }),
            el("p", { text: seo.recommended_title }))
        ),
        seo.title_rationale ? el("p", { class: "muted small", text: seo.title_rationale }) : null,
        el("div", { class: "compare", style: "margin-top:12px" },
          el("div", { class: "box" },
            el("div", { class: "label", text: "Current meta description" + (audit ? ` (${audit.meta_description_length} chars)` : "") }),
            el("p", { text: (audit && audit.current_meta_description) || "—" })),
          el("div", { class: "box new" },
            el("div", { class: "label", text: `Recommended (${seo.meta_description.length} chars)` }),
            el("p", { text: seo.meta_description }))
        )
      ),
    ];

    if (audit) {
      const checks = [
        ["Secure (HTTPS)", audit.uses_https, "The site uses a secure connection"],
        ["One main heading (H1)", audit.h1_count === 1, "Google expects one clear main heading"],
        ["Canonical tag", audit.has_canonical, "Tells Google which URL is the official one"],
        ["Structured data", audit.has_structured_data, "Business details Google can read directly"],
        ["Social preview tags", audit.has_open_graph, "Controls how links look when shared on social media"],
        ["Mobile friendly", audit.has_viewport_meta, "Page is set up to fit phone screens"],
        ["robots.txt", audit.has_robots_txt, "File that guides search engine crawlers"],
        ["Sitemap", !!audit.sitemap_url, "List of pages that helps Google find them all"],
        ["Image descriptions", audit.images_missing_alt === 0, "Alt text on every image"],
        ["Content visible without JavaScript", !audit.rendered_with_javascript, "Search engines can read the page straight away"],
      ];
      blocks.push(
        card(
          "Technical check-up (measured on the site)",
          el("ul", { class: "checks" },
            checks.map(([label, ok, desc]) =>
              el("li", { class: ok ? null : "fail" }, el("span", null, el("span", { text: label }), el("span", { class: "check-desc", text: desc })))
            )
          ),
          el("p", { class: "muted small" },
            `Page size ${audit.html_size_kb} KB · loaded in ${formatMs(audit.response_time_ms)} · ${audit.word_count} words on the homepage · ${audit.images_missing_alt} of ${audit.images_total} images without a description`),
          el("div", { class: "label", text: "Problems to fix" }),
          list(audit.issues)
        )
      );
    }

    blocks.push(
      card(
        "New pages or articles to add (to attract more search traffic)",
        table(["Type", "Title", "Purpose"], seo.supporting_content.map((s) => [badge(s.type, "plain"), el("strong", { text: s.title }), s.purpose]))
      )
    );
    return blocks;
  }

  function renderServices(services) {
    return [
      explain("The 5 most important things this business offers. \"On website\" means it is listed on the site; \"inferred\" means it is a likely service that the site does not state directly."),
      card(
        "Top 5 services",
        table(
          ["Service", "Description", "Industry", "Customer", "Main benefit", "Source"],
          services.map((s) => [
            el("strong", { text: s.service_name }),
            s.description,
            s.target_industry,
            s.target_customer,
            s.main_benefit,
            s.source === "website" ? badge("on website", "good") : badge("inferred", "warn"),
          ])
        )
      ),
    ];
  }

  function renderAudience(a) {
    const group = (title, items) => el("div", { class: "item" }, el("div", { class: "label", text: title }), list(items));
    return [
      explain("Who the customers are and what they care about — useful for deciding who to target in ads and content."),
      el("div", { class: "grid" },
        group("Primary audience", a.primary_audience),
        group("Secondary audience", a.secondary_audience),
        group("Industry segments", a.industry_segments),
        group("Business types", a.business_types),
        group("Customer needs & challenges", a.customer_needs),
        group("Interests", a.customer_interests)
      ),
      card("Customer profiles", list(a.customer_profiles),
        el("div", { class: "label", text: "Geographic focus" }),
        el("p", { text: a.geographic_focus || "Not stated on the website" })),
      card("Recommended segments for marketing",
        table(["Audience", "Business type", "Requirement"], a.recommended_segments.map((s) => [el("strong", { text: s.audience }), s.business_type, s.requirement]))),
    ];
  }

  function render(data) {
    lastResult = data;
    renderBrand(data);
    renderGlance(data);
    const a = data.analysis;
    $("#panel-competitors").replaceChildren(...renderCompetitors(a.competitor_analysis).filter(Boolean));
    $("#panel-queries").replaceChildren(...renderQueries(a.target_search_queries));
    $("#panel-seo").replaceChildren(...renderSeo(a.seo_improvements));
    $("#panel-services").replaceChildren(...renderServices(a.top_services));
    $("#panel-audience").replaceChildren(...renderAudience(a.target_audience));
    $("#raw-json").textContent = JSON.stringify(data, null, 2);
    $("#notes").replaceChildren(...(data.meta.notes || []).map((n) => el("p", { class: "note", text: n })));
    selectTab("competitors");
    $("#results").hidden = false;
    $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function selectTab(name) {
    document.querySelectorAll(".tab").forEach((tab) => {
      const active = tab.dataset.tab === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", String(active));
    });
    document.querySelectorAll(".panel").forEach((panel) => {
      panel.hidden = panel.id !== "panel-" + name;
    });
  }

  // ---------------------------------------------------------------------------
  // actions

  async function analyse(url) {
    $("#error").hidden = true;
    $("#results").hidden = true;
    submitBtn.disabled = true;
    submitBtn.textContent = "Analysing…";
    startProgress(url);

    const params = new URLSearchParams({ url });
    history.replaceState(null, "", "?" + params.toString());

    try {
      const response = await fetch("/link-analysis", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      let body = null;
      try {
        body = await response.json();
      } catch (_) {
        /* non-JSON error page from a proxy */
      }
      if (!response.ok || !body || !body.success) {
        const error = (body && body.error) || {};
        showError(error.code, error.message || `The server answered with HTTP ${response.status}.`);
        return;
      }
      render(body);
    } catch (err) {
      showError(null, "Could not reach the API. Is the server running?");
    } finally {
      stopProgress();
      submitBtn.disabled = false;
      submitBtn.textContent = "Analyse website";
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const url = input.value.trim();
    if (url) analyse(url);
  });

  document.querySelectorAll(".chip").forEach((chip) =>
    chip.addEventListener("click", () => {
      input.value = chip.dataset.url;
      analyse(chip.dataset.url);
    })
  );

  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => selectTab(tab.dataset.tab)));

  $("#copy-json").addEventListener("click", async (event) => {
    if (!lastResult) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(lastResult, null, 2));
      event.target.textContent = "Copied";
    } catch (_) {
      event.target.textContent = "Copy failed";
    }
    setTimeout(() => (event.target.textContent = "Copy"), 1500);
  });

  $("#download-json").addEventListener("click", () => {
    if (!lastResult) return;
    const host = (safeUrl(lastResult.website) ? new URL(lastResult.website).hostname : "website").replace(/^www\./, "");
    const blob = new Blob([JSON.stringify(lastResult, null, 2)], { type: "application/json" });
    const a = el("a", { href: URL.createObjectURL(blob), download: `link-analysis-${host}.json` });
    document.body.append(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
  });

  $("#print").addEventListener("click", () => window.print());

  $("#again").addEventListener("click", () => {
    input.value = "";
    window.scrollTo({ top: 0, behavior: "smooth" });
    input.focus({ preventScroll: true });
  });

  // "How to use" always works, even after the intro was hidden by a first analysis
  $("#help-link").addEventListener("click", (event) => {
    event.preventDefault();
    $("#intro").hidden = false;
    $("#how-it-works").scrollIntoView({ behavior: "smooth" });
  });

  // Shareable links: /?url=https://example.com runs the analysis straight away
  const preset = new URLSearchParams(location.search).get("url");
  if (preset) {
    input.value = preset;
    analyse(preset);
  }
})();
