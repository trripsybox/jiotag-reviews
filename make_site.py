"""
Static-site dashboard generator.

Reads the 'reviews' tab from the Google Sheet, aggregates everything, and
writes a self-contained docs/index.html file. The HTML is served by GitHub
Pages at https://<username>.github.io/<repo>/.

Privacy: only aggregates (counts, percentages, anonymized topic themes) are
written to the public site. No individual reviewer names, no full review text.

Designed to run on GitHub Actions after make_dashboard.py.
"""

import json
import os
from datetime import datetime, timezone

from common import SHEET_TAB, get_sheet, log
from make_dashboard import compute_aggregates


OUTPUT_DIR = "docs"
OUTPUT_FILE = "index.html"


def render_html(agg: dict) -> str:
    """Build the dashboard HTML with all data baked in."""

    # Data prep for Chart.js
    sentiment_labels = ["Positive", "Neutral", "Negative"]
    sentiment_values = [
        agg["sentiment_counts"].get("positive", 0),
        agg["sentiment_counts"].get("neutral", 0),
        agg["sentiment_counts"].get("negative", 0),
    ]

    source_items = sorted(agg["source_counts"].items(), key=lambda x: -x[1])
    source_labels = [s for s, _ in source_items]
    source_values = [c for _, c in source_items]

    topic_items = agg["topic_counts"].most_common(10)
    topic_labels = [t for t, _ in topic_items]
    topic_values = [c for _, c in topic_items]

    weekly_items = list(agg["weekly"].items())
    weekly_labels = [w for w, _ in weekly_items]
    weekly_values = [c for _, c in weekly_items]

    # For "top complaint themes" we don't expose individual reviews — instead
    # we show topic counts among negative reviews only (privacy-preserving).
    # Recompute topic counts restricted to negative-sentiment rows.
    neg_topic_counts = {}
    for neg in agg["negatives"]:
        for t in (neg.get("topics", "") or "").split(","):
            t = t.strip()
            if t:
                neg_topic_counts[t] = neg_topic_counts.get(t, 0) + 1
    neg_topic_items = sorted(neg_topic_counts.items(), key=lambda x: -x[1])[:8]

    last_refreshed = datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M UTC")

    # Use double-curly for literal braces in CSS/JS to avoid f-string conflicts.
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
      <div class="text-xs uppercase tracking-wider text-slate-500 mb-1">Tagged</div>
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

  <!-- Sentiment & Source -->
  <section class="grid grid-cols-1 lg:grid-cols-2 gap-6 mb-6">
    <div class="bg-white rounded-lg shadow-sm p-6 border border-slate-200">
      <h2 class="text-lg font-semibold text-slate-900 mb-4">Sentiment breakdown</h2>
      <div class="relative h-80"><canvas id="sentimentChart"></canvas></div>
    </div>
    <div class="bg-white rounded-lg shadow-sm p-6 border border-slate-200">
      <h2 class="text-lg font-semibold text-slate-900 mb-4">Reviews by source</h2>
      <div class="relative h-80"><canvas id="sourceChart"></canvas></div>
    </div>
  </section>

  <!-- Top topics -->
  <section class="bg-white rounded-lg shadow-sm p-6 border border-slate-200 mb-6">
    <h2 class="text-lg font-semibold text-slate-900 mb-4">Top topics</h2>
    <div class="relative h-96"><canvas id="topicsChart"></canvas></div>
  </section>

  <!-- Weekly trend -->
  <section class="bg-white rounded-lg shadow-sm p-6 border border-slate-200 mb-6">
    <h2 class="text-lg font-semibold text-slate-900 mb-4">Weekly review volume (last 12 weeks)</h2>
    <div class="relative h-96"><canvas id="weeklyChart"></canvas></div>
  </section>

  <!-- Negative themes -->
  <section class="bg-white rounded-lg shadow-sm p-6 border border-slate-200 mb-6">
    <h2 class="text-lg font-semibold text-slate-900 mb-4">Recent complaint themes</h2>
    <p class="text-sm text-slate-500 mb-4">Topics most commonly raised in recent negative reviews.</p>
    <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
      {''.join(f'<div class="bg-rose-50 border border-rose-200 rounded-md px-4 py-3"><div class="text-sm text-rose-900 font-medium">{t}</div><div class="text-xs text-rose-600 mt-1">{c} mentions</div></div>' for t, c in neg_topic_items)}
    </div>
  </section>

  <!-- Competitor mentions -->
  <section class="bg-white rounded-lg shadow-sm p-6 border border-slate-200 mb-6">
    <h2 class="text-lg font-semibold text-slate-900 mb-2">Competitor mentions</h2>
    <p class="text-3xl font-bold text-amber-600 kpi-num">{agg["competitor_count"]}</p>
    <p class="text-sm text-slate-500 mt-1">Reviews that mention AirTag, Tile, SmartTag, or other competing trackers.</p>
  </section>

  <footer class="text-center text-xs text-slate-400 pt-8 pb-4">
    Built with public review data from Google Play, App Store, and YouTube · Updated daily
  </footer>

</div>

<script>
// Register the datalabels plugin globally so we can use it per-chart.
Chart.register(ChartDataLabels);

// Default: don't show datalabels unless a chart explicitly opts in.
Chart.defaults.set('plugins.datalabels', {{ display: false }});

const palette = ['#10b981', '#94a3b8', '#ef4444', '#3b82f6', '#f59e0b', '#8b5cf6', '#ec4899', '#06b6d4', '#84cc16', '#f97316'];

// --- Sentiment doughnut: show percentage on each slice ---
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
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ position: 'right', labels: {{ font: {{ size: 13 }}, padding: 12 }} }},
      datalabels: {{
        display: true,
        color: '#ffffff',
        font: {{ weight: 'bold', size: 14 }},
        formatter: (value) => {{
          if (sentimentTotal === 0) return '';
          const pct = (value / sentimentTotal * 100).toFixed(1);
          return pct + '%';
        }}
      }},
      tooltip: {{
        callbacks: {{
          label: (ctx) => ctx.label + ': ' + ctx.parsed + ' reviews (' + (ctx.parsed / sentimentTotal * 100).toFixed(1) + '%)'
        }}
      }}
    }}
  }}
}});

// --- Source bar: vertical bars with count labels on top ---
new Chart(document.getElementById('sourceChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(source_labels)},
    datasets: [{{
      label: 'Reviews',
      data: {json.dumps(source_values)},
      backgroundColor: '#3b82f6',
      borderRadius: 4
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ display: false }},
      datalabels: {{
        display: true,
        anchor: 'end',
        align: 'end',
        color: '#1e293b',
        font: {{ weight: 'bold', size: 12 }}
      }}
    }},
    scales: {{
      x: {{ title: {{ display: true, text: 'Source', font: {{ size: 13, weight: 'bold' }} }}, grid: {{ display: false }} }},
      y: {{ title: {{ display: true, text: 'Number of reviews', font: {{ size: 13, weight: 'bold' }} }}, beginAtZero: true, ticks: {{ precision: 0 }} }}
    }}
  }}
}});

// --- Topics horizontal bar: count labels at end of bar ---
new Chart(document.getElementById('topicsChart'), {{
  type: 'bar',
  data: {{
    labels: {json.dumps(topic_labels)},
    datasets: [{{
      label: 'Mentions',
      data: {json.dumps(topic_values)},
      backgroundColor: '#8b5cf6',
      borderRadius: 4
    }}]
  }},
  options: {{
    indexAxis: 'y',
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ display: false }},
      datalabels: {{
        display: true,
        anchor: 'end',
        align: 'end',
        color: '#1e293b',
        font: {{ weight: 'bold', size: 12 }}
      }}
    }},
    scales: {{
      x: {{ title: {{ display: true, text: 'Number of mentions', font: {{ size: 13, weight: 'bold' }} }}, beginAtZero: true, ticks: {{ precision: 0 }} }},
      y: {{ title: {{ display: true, text: 'Topic', font: {{ size: 13, weight: 'bold' }} }}, grid: {{ display: false }} }}
    }}
  }}
}});

// --- Weekly trend line: value above each point ---
new Chart(document.getElementById('weeklyChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps(weekly_labels)},
    datasets: [{{
      label: 'Reviews',
      data: {json.dumps(weekly_values)},
      borderColor: '#3b82f6',
      backgroundColor: 'rgba(59,130,246,0.1)',
      fill: true,
      tension: 0.3,
      pointRadius: 5,
      pointBackgroundColor: '#3b82f6'
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{
      legend: {{ display: false }},
      datalabels: {{
        display: true,
        anchor: 'end',
        align: 'top',
        color: '#1e293b',
        font: {{ weight: 'bold', size: 11 }},
        offset: 6
      }}
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


def main():
    log("=== Site builder starting ===")
    sh = get_sheet().spreadsheet
    reviews_ws = sh.worksheet(SHEET_TAB)
    rows = reviews_ws.get_all_records()
    log(f"Loaded {len(rows)} rows")

    agg = compute_aggregates(rows)
    html = render_html(agg)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"Wrote {len(html):,} bytes to {out_path}")
    log("=== Site build done ===")


if __name__ == "__main__":
    main()
