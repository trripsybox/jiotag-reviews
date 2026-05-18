"""
Static-site dashboard generator.

Reads the 'reviews' tab from the Google Sheet, aggregates everything, generates
LLM-based action items (weekly cadence) and competitor themes (each daily run)
via Groq, and writes a self-contained docs/index.html file. The HTML is served
by GitHub Pages at https://<username>.github.io/<repo>/.

Action items are cached in docs/action_items.json so they refresh only when the
ISO week changes. The week label is shown in the UI so users can see when the
current items were generated.

Privacy: only aggregates (counts, percentages, LLM-rewritten themes) are
written to the public site. No individual reviewer names, no raw review text.

Designed to run on GitHub Actions after make_dashboard.py.
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

from common import SHEET_TAB, TOPIC_TAGS, get_sheet, log
from make_dashboard import compute_aggregates


OUTPUT_DIR = "docs"
OUTPUT_FILE = "index.html"
ACTION_ITEMS_CACHE = os.path.join(OUTPUT_DIR, "action_items.json")

# IST is UTC+5:30
IST = timezone(timedelta(hours=5, minutes=30))

# A constrained list of "primary topics" for the stacked chart.
PRIMARY_TOPICS_ORDER = [
    "battery", "accuracy", "range", "app_connectivity", "find_my_device",
    "price", "build_quality", "customer_service", "setup", "community_find",
    "compatibility", "other",
]


def now_ist_str():
    """Current time formatted in IST, e.g. '18 May 2026, 14:30 IST'."""
    return datetime.now(IST).strftime("%d %b %Y, %H:%M IST")


def current_iso_week_label():
    """e.g. '2026-W20' — week boundary used for action-item caching."""
    now = datetime.now(IST)
    year, week, _ = now.isocalendar()
    return f"{year}-W{week:02d}"


def human_week_range(week_label):
    """Convert '2026-W20' to a human-readable range like 'May 11 – May 17, 2026'."""
    try:
        year_str, week_str = week_label.split("-W")
        year = int(year_str)
        week = int(week_str)
        # Monday of ISO week
        monday = datetime.fromisocalendar(year, week, 1)
        sunday = monday + timedelta(days=6)
        if monday.month == sunday.month:
            return f"{monday.strftime('%b %d')} – {sunday.strftime('%d, %Y')}"
        return f"{monday.strftime('%b %d')} – {sunday.strftime('%b %d, %Y')}"
    except Exception:
        return week_label


# --- LLM helpers (Groq) ------------------------------------------------------

def call_groq(prompt: str, max_tokens: int = 800) -> str:
    """Send a prompt to Groq and return the response text. Returns empty string on failure."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        log("  GROQ_API_KEY not set, skipping LLM call")
        return ""

    url = "https://api.groq.com/openai/v1/chat/completions"
    body = {
        "model": "llama-3.1-8b-instant",
        "messages": [
            {"role": "system", "content": "You are a product analyst. Be concise, factual, and avoid speculation."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    backoff = 5
    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=30)
            if resp.status_code in (429, 503):
                log(f"  Groq {resp.status_code} on attempt {attempt + 1}, backing off {backoff}s")
                time.sleep(backoff)
                backoff *= 2
                continue
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log(f"  Groq call error (attempt {attempt + 1}): {e}")
            if attempt < 2:
                time.sleep(backoff)
                backoff *= 2
    return ""


def generate_action_items(agg: dict) -> list:
    """Ask the LLM to produce the top 5 action items for this week.
    Returns list of dicts: [{"title": ..., "rationale": ..., "priority": "high|medium|low"}]
    """
    # Build a tight prompt — only feed aggregates, no raw review text.
    topic_breakdown = []
    for t in PRIMARY_TOPICS_ORDER:
        counts = agg["topic_by_sentiment"].get(t, {})
        pos = counts.get("positive", 0)
        neu = counts.get("neutral", 0)
        neg = counts.get("negative", 0)
        if pos + neu + neg > 0:
            topic_breakdown.append(f"  - {t}: {neg} neg / {neu} neu / {pos} pos")

    weekly_recent = list(agg["weekly"].items())[-4:]
    weekly_str = ", ".join(f"{w}={c}" for w, c in weekly_recent)

    prompt = f"""You are analyzing reviews of JioTag, a Bluetooth tracker by Reliance Jio (similar to Apple AirTag).

Aggregated review data:
- Total reviews tagged: {agg["tagged"]}
- Sentiment split: {agg["pos_pct"]:.0f}% positive, {agg["neg_pct"]:.0f}% negative
- Recent weekly volume: {weekly_str}
- Competitor mentions: {agg["competitor_count"]} reviews
- Topic breakdown (negative/neutral/positive counts):
{chr(10).join(topic_breakdown)}

Identify the top 5 ACTION ITEMS for the JioTag product team this week. Each action item should:
- Be a specific, concrete recommendation (not "improve the product")
- Be grounded in what the data actually shows (e.g., a topic with high negative count)
- Have a clear priority (high/medium/low) based on volume + severity

Respond with ONLY a JSON array of 5 objects, no prose, no markdown:
[
  {{"title": "...", "rationale": "...", "priority": "high"}},
  ...
]"""

    text = call_groq(prompt, max_tokens=1200)
    if not text:
        return []

    # Try to extract JSON from the response
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    try:
        items = json.loads(text)
        # Validate shape
        result = []
        for it in items[:5]:
            if isinstance(it, dict) and "title" in it:
                result.append({
                    "title": str(it.get("title", "")).strip(),
                    "rationale": str(it.get("rationale", "")).strip(),
                    "priority": str(it.get("priority", "medium")).strip().lower(),
                })
        return result
    except Exception as e:
        log(f"  Could not parse action items JSON: {e}")
        return []


def generate_competitor_themes(agg: dict) -> list:
    """Ask the LLM to summarize the main themes when competitors are mentioned.
    Returns list of dicts: [{"competitor": ..., "theme": ...}]
    """
    if agg["competitor_count"] == 0:
        return []

    # Sample up to 30 competitor-mentioning reviews (capped to keep prompt small).
    sample = agg["competitor_reviews"][:30]
    text_blob = "\n".join(f"- [{r['sentiment']}, topics: {r['topics']}] {r['text'][:200]}" for r in sample)

    prompt = f"""Below are review snippets from JioTag customers who mentioned competing products like AirTag, Tile, or Samsung SmartTag:

{text_blob}

Summarize the MAIN THEMES of how competitors are discussed. Output 3-5 themes.

For each theme:
- Name the SPECIFIC competitor mentioned (e.g. "AirTag", "Tile", "Samsung SmartTag"). Use "Competitors (general)" only if none is named.
- Write ONE clear sentence stating who wins or loses on what attribute. Be UNAMBIGUOUS about direction.
- Use this structure: "[X] is [better/worse] than [Y] at [attribute] because [reason]."
- DO NOT use vague words like "favorably" or "unfavorably" — they hide who's winning.
- DO NOT quote review text. Paraphrase only.

Examples of GOOD themes (unambiguous):
- "AirTag has better range than JioTag — users say AirTag works further from the phone."
- "JioTag is cheaper than AirTag — users mention price as a deciding factor in switching to JioTag."

Examples of BAD themes (ambiguous — don't write these):
- "Users compare AirTag range favorably to JioTag." (WHO is favored? Unclear.)
- "AirTag vs JioTag comparisons are mixed." (Says nothing.)

Respond with ONLY a JSON array, no prose, no markdown:
[
  {{"competitor": "AirTag", "theme": "AirTag has better range than JioTag — ..."}},
  ...
]"""

    text = call_groq(prompt, max_tokens=800)
    if not text:
        return []

    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    try:
        items = json.loads(text)
        result = []
        for it in items[:5]:
            if isinstance(it, dict) and "theme" in it:
                result.append({
                    "competitor": str(it.get("competitor", "Competitor")).strip(),
                    "theme": str(it.get("theme", "")).strip(),
                })
        return result
    except Exception as e:
        log(f"  Could not parse competitor themes JSON: {e}")
        return []


# --- HTML rendering ----------------------------------------------------------

def load_action_items_cache():
    """Load cached action items + the week they were generated for.
    Returns (items_list, week_label) or ([], '').
    """
    if not os.path.exists(ACTION_ITEMS_CACHE):
        return [], ""
    try:
        with open(ACTION_ITEMS_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("items", []), data.get("week", "")
    except Exception as e:
        log(f"  Could not read action items cache: {e}")
        return [], ""


def save_action_items_cache(items, week_label):
    """Persist action items + week label so next run can reuse them."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    try:
        with open(ACTION_ITEMS_CACHE, "w", encoding="utf-8") as f:
            json.dump({"items": items, "week": week_label,
                       "generated_at_ist": now_ist_str()}, f, indent=2)
        log(f"  Saved action items cache for week {week_label}")
    except Exception as e:
        log(f"  Could not save action items cache: {e}")


def render_html(agg: dict, action_items: list, competitor_themes: list,
                action_items_week: str = "") -> str:
    """Build the dashboard HTML with all data baked in."""

    # Sentiment data
    sentiment_labels = ["Positive", "Neutral", "Negative"]
    sentiment_values = [
        agg["sentiment_counts"].get("positive", 0),
        agg["sentiment_counts"].get("neutral", 0),
        agg["sentiment_counts"].get("negative", 0),
    ]

    # Source data
    source_items = sorted(agg["source_counts"].items(), key=lambda x: -x[1])
    source_labels = [s for s, _ in source_items]
    source_values = [c for _, c in source_items]

    # Topic data — stacked by sentiment.
    # Chart.js horizontal bars render index 0 at the TOP of the chart.
    # We want: largest topic at top, descending, with 'other' always at the bottom.
    # So the array should be: [largest, 2nd-largest, ..., smallest non-other, other]
    topics_sorted_desc = sorted(
        agg["topic_counts"].items(), key=lambda x: -x[1]
    )
    non_other = [(t, c) for t, c in topics_sorted_desc if t != "other"]
    other = [(t, c) for t, c in topics_sorted_desc if t == "other"]
    topics_ordered = non_other + other  # 'other' last → appears at bottom

    topic_labels = []
    pos_per_topic = []
    neu_per_topic = []
    neg_per_topic = []
    for t, _total in topics_ordered:
        counts = agg["topic_by_sentiment"].get(t, {})
        topic_labels.append(t)
        pos_per_topic.append(counts.get("positive", 0))
        neu_per_topic.append(counts.get("neutral", 0))
        neg_per_topic.append(counts.get("negative", 0))

    # Weekly trend
    weekly_items = list(agg["weekly"].items())
    weekly_labels = [w for w, _ in weekly_items]
    weekly_values = [c for _, c in weekly_items]

    # Competitor topic breakdown (top 6)
    comp_topic_items = agg["competitor_topic_counts"].most_common(6)

    last_refreshed = now_ist_str()
    action_items_week_human = human_week_range(action_items_week) if action_items_week else "—"

    # --- Build action items HTML ---
    if action_items:
        action_html = ""
        priority_colors = {
            "high": ("bg-rose-50", "border-rose-200", "text-rose-700", "bg-rose-100"),
            "medium": ("bg-amber-50", "border-amber-200", "text-amber-700", "bg-amber-100"),
            "low": ("bg-slate-50", "border-slate-200", "text-slate-700", "bg-slate-100"),
        }
        for i, item in enumerate(action_items, 1):
            colors = priority_colors.get(item["priority"], priority_colors["medium"])
            bg, border, text_color, badge_bg = colors
            action_html += f"""
            <div class="{bg} border {border} rounded-lg p-4 mb-3 flex items-start gap-4">
              <div class="text-2xl font-bold text-slate-300 leading-none">{i}</div>
              <div class="flex-1">
                <div class="flex items-center gap-2 mb-1">
                  <h3 class="font-semibold text-slate-900">{item["title"]}</h3>
                  <span class="text-xs uppercase tracking-wider px-2 py-0.5 rounded {badge_bg} {text_color} font-medium">{item["priority"]}</span>
                </div>
                <p class="text-sm text-slate-600">{item["rationale"]}</p>
              </div>
            </div>
            """
    else:
        action_html = '<p class="text-sm text-slate-500 italic">Action items could not be generated this run (LLM unavailable). Will retry on next daily refresh.</p>'

    # --- Build competitor themes HTML ---
    if competitor_themes:
        comp_themes_html = '<ul class="space-y-3">'
        for ct in competitor_themes:
            comp_themes_html += f'''
            <li class="flex items-start gap-3">
              <span class="inline-block bg-amber-100 text-amber-800 text-xs font-medium px-2 py-0.5 rounded mt-0.5 shrink-0">{ct["competitor"]}</span>
              <span class="text-sm text-slate-700">{ct["theme"]}</span>
            </li>
            '''
        comp_themes_html += '</ul>'
    else:
        comp_themes_html = '<p class="text-sm text-slate-500 italic">Themes will appear once enough competitor-mentioning reviews are collected.</p>'

    # --- Competitor topic breakdown ---
    if comp_topic_items:
        comp_topic_html = '<div class="grid grid-cols-2 md:grid-cols-3 gap-2 mt-4">'
        for t, c in comp_topic_items:
            comp_topic_html += f'''
            <div class="bg-amber-50 border border-amber-200 rounded px-3 py-2">
              <div class="text-sm font-medium text-amber-900">{t}</div>
              <div class="text-xs text-amber-700">{c} reviews</div>
            </div>
            '''
        comp_topic_html += '</div>'
    else:
        comp_topic_html = ''

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>JioTag Review Dashboard</title>
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-datalabels@2.2.0"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  .kpi-num {{ font-feature-settings: "tnum"; }}
  .info-tooltip {{ position: relative; display: inline-block; }}
  .info-tooltip .tooltip-text {{
    visibility: hidden; opacity: 0; transition: opacity 0.2s;
    position: absolute; z-index: 10;
    bottom: 125%; left: 50%; transform: translateX(-50%);
    width: 280px; background: #1e293b; color: #fff;
    text-align: left; padding: 10px 12px; border-radius: 6px;
    font-size: 12px; line-height: 1.4; font-weight: normal;
    box-shadow: 0 4px 12px rgba(0,0,0,0.15);
  }}
  .info-tooltip:hover .tooltip-text {{ visibility: visible; opacity: 1; }}
  .info-tooltip .tooltip-text::after {{
    content: ""; position: absolute; top: 100%; left: 50%; margin-left: -5px;
    border-width: 5px; border-style: solid;
    border-color: #1e293b transparent transparent transparent;
  }}
  .info-icon {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 16px; height: 16px; border-radius: 50%;
    background: #cbd5e1; color: #475569;
    font-size: 11px; font-weight: bold; cursor: help;
    margin-left: 6px;
  }}
</style>
</head>
<body class="bg-slate-50 text-slate-800">

<div class="max-w-7xl mx-auto px-6 py-10">

  <!-- Hero -->
  <header class="mb-10">
    <div class="flex items-baseline justify-between flex-wrap gap-2">
      <h1 class="text-4xl font-bold text-slate-900">JioTag Review Dashboard</h1>
      <span class="text-sm text-slate-500">Last refreshed: {last_refreshed}</span>
    </div>
    <p class="mt-2 text-slate-600">
      Automated analysis of {agg["total"]} customer reviews from Google Play, Apple App Store, and YouTube.
      Updated daily.
    </p>
  </header>

  <!-- KPI strip -->
  <section class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4 mb-10">
    <div class="bg-white rounded-lg shadow-sm p-5 border border-slate-200">
      <div class="text-xs uppercase tracking-wider text-slate-500 mb-1">Total reviews</div>
      <div class="kpi-num text-3xl font-bold text-slate-900">{agg["total"]:,}</div>
    </div>
    <div class="bg-white rounded-lg shadow-sm p-5 border border-slate-200">
      <div class="text-xs uppercase tracking-wider text-slate-500 mb-1 flex items-center">
        Tagged
        <span class="info-tooltip">
          <span class="info-icon">i</span>
          <span class="tooltip-text">
            <strong>Why "tagged"?</strong><br>
            Reviews are scraped <b>weekly</b> on Monday from Play Store, App Store, and YouTube.
            Sentiment + topic tagging happens <b>daily</b> in the background to spread the LLM workload across the week
            and stay within free-tier API limits. So "Tagged" = reviews already classified, vs. just scraped.
          </span>
        </span>
      </div>
      <div class="kpi-num text-3xl font-bold text-slate-900">{agg["tagged"]:,}</div>
    </div>
    <div class="bg-white rounded-lg shadow-sm p-5 border border-slate-200">
      <div class="text-xs uppercase tracking-wider text-slate-500 mb-1">% Negative</div>
      <div class="kpi-num text-3xl font-bold text-rose-600">{agg["neg_pct"]:.1f}%</div>
    </div>
    <div class="bg-white rounded-lg shadow-sm p-5 border border-slate-200">
      <div class="text-xs uppercase tracking-wider text-slate-500 mb-1">% Positive</div>
      <div class="kpi-num text-3xl font-bold text-emerald-600">{agg["pos_pct"]:.1f}%</div>
    </div>
    <div class="bg-white rounded-lg shadow-sm p-5 border border-slate-200">
      <div class="text-xs uppercase tracking-wider text-slate-500 mb-1">Top topic</div>
      <div class="kpi-num text-2xl font-bold text-slate-900 truncate">{agg["top_topic"]}</div>
    </div>
    <div class="bg-white rounded-lg shadow-sm p-5 border border-slate-200">
      <div class="text-xs uppercase tracking-wider text-slate-500 mb-1">New this week</div>
      <div class="kpi-num text-3xl font-bold text-blue-600">{agg["new_this_week"]:,}</div>
    </div>
  </section>

  <!-- Action items (NEW — top placement because they're the most actionable thing) -->
  <section class="bg-white rounded-lg shadow-sm p-6 border border-slate-200 mb-6">
    <div class="flex items-center justify-between mb-1 flex-wrap gap-2">
      <h2 class="text-lg font-semibold text-slate-900">Top 5 action items</h2>
      <span class="text-xs text-slate-400 italic">AI-generated · refreshed weekly · review before acting</span>
    </div>
    <p class="text-sm text-slate-500 mb-4">
      <span class="font-semibold text-slate-700">Week of {action_items_week_human}</span>
      <span class="text-slate-400"> · {action_items_week}</span>
    </p>
    {action_html}
  </section>

  <!-- Reviews by source (LEFT) and Sentiment breakdown (RIGHT) — swapped order -->
  <section class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
    <div class="bg-white rounded-lg shadow-sm p-6 border border-slate-200">
      <h2 class="text-lg font-semibold text-slate-900 mb-4">Reviews by source</h2>
      <div class="relative h-80"><canvas id="sourceChart"></canvas></div>
    </div>
    <div class="bg-white rounded-lg shadow-sm p-6 border border-slate-200">
      <h2 class="text-lg font-semibold text-slate-900 mb-4">Sentiment breakdown</h2>
      <div class="relative h-80"><canvas id="sentimentChart"></canvas></div>
    </div>
  </section>

  <!-- Top topics — stacked by sentiment -->
  <section class="bg-white rounded-lg shadow-sm p-6 border border-slate-200 mb-6">
    <h2 class="text-lg font-semibold text-slate-900 mb-1">Top topics</h2>
    <p class="text-sm text-slate-500 mb-4">Each bar shows how many reviews mention the topic, broken down by sentiment.</p>
    <div class="relative h-96"><canvas id="topicsChart"></canvas></div>
  </section>

  <!-- Weekly trend -->
  <section class="bg-white rounded-lg shadow-sm p-6 border border-slate-200 mb-6">
    <h2 class="text-lg font-semibold text-slate-900 mb-4">Weekly review volume (last 12 weeks)</h2>
    <div class="relative h-96"><canvas id="weeklyChart"></canvas></div>
  </section>

  <!-- Competitor mentions — refined with themes + topic breakdown -->
  <section class="bg-white rounded-lg shadow-sm p-6 border border-slate-200 mb-6">
    <div class="flex items-center justify-between mb-1">
      <h2 class="text-lg font-semibold text-slate-900">Competitor mentions</h2>
      <span class="text-xs text-slate-400 italic">AI-summarized · no raw quotes</span>
    </div>
    <p class="text-sm text-slate-500 mb-4">
      <span class="font-semibold text-amber-700 text-base">{agg["competitor_count"]}</span>
      reviews mention competing products. Themes paraphrased below.
    </p>

    <div class="grid grid-cols-1 md:grid-cols-2 gap-6">
      <div>
        <h3 class="text-sm font-semibold text-slate-700 mb-3 uppercase tracking-wider">Discussion themes</h3>
        {comp_themes_html}
      </div>
      <div>
        <h3 class="text-sm font-semibold text-slate-700 mb-3 uppercase tracking-wider">Topics raised alongside competitors</h3>
        {comp_topic_html}
      </div>
    </div>
  </section>

  <footer class="text-center text-xs text-slate-400 pt-8 pb-4">
    Built with public review data from Google Play, App Store, and YouTube · Updated daily
  </footer>

</div>

<script>
Chart.register(ChartDataLabels);
Chart.defaults.set('plugins.datalabels', {{ display: false }});

// --- Sentiment doughnut ---
const sentimentTotal = {json.dumps(sum(sentiment_values))};
new Chart(document.getElementById('sentimentChart'), {{
  type: 'doughnut',
  data: {{
    labels: {json.dumps(sentiment_labels)},
    datasets: [{{
      data: {json.dumps(sentiment_values)},
      backgroundColor: ['#10b981', '#94a3b8', '#ef4444'],
      borderWidth: 2,
      borderColor: '#ffffff'
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{ position: 'right', labels: {{ font: {{ size: 13 }}, padding: 12 }} }},
      datalabels: {{
        display: true, color: '#ffffff', font: {{ weight: 'bold', size: 14 }},
        formatter: (value) => sentimentTotal === 0 ? '' : (value / sentimentTotal * 100).toFixed(1) + '%'
      }},
      tooltip: {{
        callbacks: {{
          label: (ctx) => ctx.label + ': ' + ctx.parsed + ' reviews (' + (ctx.parsed / sentimentTotal * 100).toFixed(1) + '%)'
        }}
      }}
    }}
  }}
}});

// --- Source bar ---
new Chart(document.getElementById('sourceChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(source_labels)},
    datasets: [{{
      label: 'Reviews', data: {json.dumps(source_values)},
      backgroundColor: '#3b82f6', borderRadius: 4
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{ display: false }},
      datalabels: {{ display: true, anchor: 'end', align: 'end', color: '#1e293b', font: {{ weight: 'bold', size: 12 }} }}
    }},
    scales: {{
      x: {{ title: {{ display: true, text: 'Source', font: {{ size: 13, weight: 'bold' }} }}, grid: {{ display: false }} }},
      y: {{ title: {{ display: true, text: 'Number of reviews', font: {{ size: 13, weight: 'bold' }} }}, beginAtZero: true, ticks: {{ precision: 0 }} }}
    }}
  }}
}});

// --- Topics: stacked horizontal bar by sentiment ---
new Chart(document.getElementById('topicsChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(topic_labels)},
    datasets: [
      {{ label: 'Negative', data: {json.dumps(neg_per_topic)}, backgroundColor: '#ef4444', borderRadius: 2 }},
      {{ label: 'Neutral',  data: {json.dumps(neu_per_topic)}, backgroundColor: '#94a3b8', borderRadius: 2 }},
      {{ label: 'Positive', data: {json.dumps(pos_per_topic)}, backgroundColor: '#10b981', borderRadius: 2 }}
    ]
  }},
  options: {{
    indexAxis: 'y', responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{ position: 'top', labels: {{ font: {{ size: 12 }}, boxWidth: 14, padding: 12 }} }},
      datalabels: {{
        display: (ctx) => ctx.dataset.data[ctx.dataIndex] >= 3,
        color: '#ffffff', font: {{ weight: 'bold', size: 11 }}
      }},
      tooltip: {{ mode: 'index', intersect: false }}
    }},
    scales: {{
      x: {{ stacked: true, title: {{ display: true, text: 'Number of mentions', font: {{ size: 13, weight: 'bold' }} }}, beginAtZero: true, ticks: {{ precision: 0 }} }},
      y: {{ stacked: true, title: {{ display: true, text: 'Topic', font: {{ size: 13, weight: 'bold' }} }}, grid: {{ display: false }} }}
    }}
  }}
}});

// --- Weekly trend ---
new Chart(document.getElementById('weeklyChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(weekly_labels)},
    datasets: [{{
      label: 'Reviews', data: {json.dumps(weekly_values)},
      borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.1)',
      fill: true, tension: 0.3, pointRadius: 5, pointBackgroundColor: '#3b82f6'
    }}]
  }},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{ display: false }},
      datalabels: {{ display: true, anchor: 'end', align: 'top', color: '#1e293b', font: {{ weight: 'bold', size: 11 }}, offset: 6 }}
    }},
    scales: {{
      x: {{ title: {{ display: true, text: 'ISO week', font: {{ size: 13, weight: 'bold' }} }}, grid: {{ display: false }} }},
      y: {{ title: {{ display: true, text: 'Reviews collected', font: {{ size: 13, weight: 'bold' }} }}, beginAtZero: true, ticks: {{ precision: 0 }} }}
    }}
  }}
}});
</script>
</body>
</html>
"""
    return html


# --- Main --------------------------------------------------------------------

def main():
    log("=== Site builder starting ===")
    sh = get_sheet().spreadsheet
    reviews_ws = sh.worksheet(SHEET_TAB)
    rows = reviews_ws.get_all_records()
    log(f"Loaded {len(rows)} rows")

    agg = compute_aggregates(rows)
    log("Aggregates computed")

    # --- Action items: regenerate only when ISO week changes (weekly cadence) ---
    current_week = current_iso_week_label()
    cached_items, cached_week = load_action_items_cache()

    if cached_items and cached_week == current_week:
        log(f"Reusing cached action items from week {cached_week} (no Groq call this run)")
        action_items = cached_items
        action_items_week = cached_week
    else:
        log(f"Generating fresh action items for week {current_week} via Groq...")
        action_items = generate_action_items(agg)
        log(f"  Got {len(action_items)} action items")
        if action_items:
            save_action_items_cache(action_items, current_week)
            action_items_week = current_week
        elif cached_items:
            # LLM failed but we have stale cache — show the old items rather than nothing.
            log("  Falling back to previous week's cached action items")
            action_items = cached_items
            action_items_week = cached_week
        else:
            action_items_week = current_week

    # --- Competitor themes: regenerate every run (cheap, only ~1 call) ---
    log("Generating competitor themes via Groq...")
    competitor_themes = generate_competitor_themes(agg)
    log(f"  Got {len(competitor_themes)} competitor themes")

    html = render_html(agg, action_items, competitor_themes, action_items_week)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"Wrote {len(html):,} bytes to {out_path}")
    log("=== Site build done ===")


if __name__ == "__main__":
    main()
