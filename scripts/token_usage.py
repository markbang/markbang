#!/usr/bin/env python3
"""Render recent LLM token usage (Cohub + CLIProxyAPI keeper) as light/dark SVGs."""
import http.cookiejar
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DAYS = 30
TZ = timezone(timedelta(hours=8))
OUT = Path(__file__).resolve().parent.parent / "assets"
COHUB_API = "https://api.cohub.live"
COHUB_ISSUER = "https://auth.neta.art"
COHUB_CLIENT_ID = "f8d26cdlwx85b0e5l3om2"
COHUB_RESOURCE = "https://api.talesofai"

THEMES = {
    "light": {"bg": "#ffffff", "fg": "#1f2328", "muted": "#59636e", "grid": "#d1d9e0", "cohub": "#1f6feb", "cpa": "#e8a33d"},
    "dark": {"bg": "#0d1117", "fg": "#f0f6fc", "muted": "#9198a1", "grid": "#30363d", "cohub": "#4493f8", "cpa": "#f0b552"},
}


def cohub_access_token():
    refresh_token = os.environ.get("COHUB_REFRESH_TOKEN")
    if not refresh_token:
        return subprocess.run(["cohub", "auth", "token"], check=True, capture_output=True, text=True).stdout.strip()
    body = urllib.parse.urlencode({
        "client_id": COHUB_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "resource": COHUB_RESOURCE,
    }).encode()
    try:
        with urllib.request.urlopen(f"{COHUB_ISSUER}/oidc/token", data=body, timeout=30) as resp:
            token = json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"Cohub refresh failed ({e.code}: {e.read().decode()[:200]}); re-run device login and reset COHUB_REFRESH_TOKEN")
    # Logto rotates refresh tokens: the old one is now spent, so hand the new one back to CI immediately.
    out = os.environ.get("COHUB_REFRESH_TOKEN_OUT")
    if out and token.get("refresh_token"):
        Path(out).write_text(token["refresh_token"])
    return token["access_token"]


def cohub_daily():
    req = urllib.request.Request(
        f"{COHUB_API}/api/me/activity?days={DAYS + 1}",
        headers={"Authorization": f"Bearer {cohub_access_token()}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    daily = defaultdict(int)
    for row in data["hourly"]:
        ts = datetime.fromisoformat(row["bucketStartAt"].replace("Z", "+00:00"))
        daily[ts.astimezone(TZ).date()] += row["totalTokens"]
    models = defaultdict(int)
    for m in data["rankings"]["llmModels"]:
        models[m["model"]] += m["totalTokens"]
    return daily, models


def keeper_daily():
    base = os.environ.get("KEEPER_URL", "https://ai-admin.bangwu.me").rstrip("/")
    api_key = os.environ.get("KEEPER_API_KEY")
    if not api_key:
        print("KEEPER_API_KEY not set; skipping CLIProxyAPI source", file=sys.stderr)
        return None
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    req = urllib.request.Request(
        f"{base}/api/v1/auth/api-key-login",
        data=json.dumps({"apiKey": api_key}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener.open(req, timeout=30).read()
    with opener.open(f"{base}/api/v1/key-overview?range={DAYS}d", timeout=60) as resp:
        series = json.load(resp)["series"]
    daily = defaultdict(int)
    for bucket, tokens in zip(series["buckets"], series["tokens"]):
        daily[date.fromisoformat(bucket[:10])] += tokens
    return daily


def human(n):
    for unit, size in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return str(int(n))


def render(theme, days, cohub, cpa, models):
    t = THEMES[theme]
    w, h = 800, 260
    left, right, top, bottom = 24, 24, 92, 36
    cw, ch = w - left - right, h - top - bottom
    totals = [cohub.get(d, 0) + (cpa or {}).get(d, 0) for d in days]
    peak = max(totals) or 1
    slot = cw / len(days)
    bw = slot * 0.68
    total = sum(totals)
    cohub_total = sum(cohub.get(d, 0) for d in days)
    cpa_total = sum((cpa or {}).get(d, 0) for d in days)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" font-family="-apple-system,BlinkMacSystemFont,Segoe UI,Helvetica,Arial,sans-serif">',
        f'<rect width="{w}" height="{h}" rx="10" fill="{t["bg"]}" stroke="{t["grid"]}"/>',
        f'<text x="{left}" y="40" font-size="13" fill="{t["muted"]}">Tokens · last {DAYS} days</text>',
        f'<text x="{left}" y="74" font-size="30" font-weight="600" fill="{t["fg"]}">{human(total)}</text>',
    ]

    legend = [("Cohub", t["cohub"], cohub_total)]
    if cpa is not None:
        legend.append(("CLIProxyAPI", t["cpa"], cpa_total))
    x = w - right
    for label, color, value in reversed(legend):
        text = f"{label} {human(value)}"
        x -= len(text) * 7 + 18
        parts.append(f'<rect x="{x}" y="31" width="10" height="10" rx="2" fill="{color}"/>')
        parts.append(f'<text x="{x + 15}" y="40" font-size="12" fill="{t["muted"]}">{text}</text>')
        x -= 14

    top_models = sorted(models.items(), key=lambda kv: -kv[1])[:3]
    if top_models:
        line = "  ·  ".join(f"{m} {human(v)}" for m, v in top_models)
        parts.append(f'<text x="{w - right}" y="74" font-size="12" text-anchor="end" fill="{t["muted"]}">{line}</text>')

    base_y = top + ch
    parts.append(f'<line x1="{left}" y1="{base_y}" x2="{w - right}" y2="{base_y}" stroke="{t["grid"]}"/>')
    for i, d in enumerate(days):
        bx = left + i * slot + (slot - bw) / 2
        y = base_y
        for value, color in ((cohub.get(d, 0), t["cohub"]), ((cpa or {}).get(d, 0), t["cpa"])):
            if value <= 0:
                continue
            bh = max(value / peak * ch, 1.5)
            y -= bh
            parts.append(f'<rect x="{bx:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="1.5" fill="{color}"><title>{d} {human(value)}</title></rect>')

    for i in (0, len(days) // 2, len(days) - 1):
        cx = left + i * slot + slot / 2
        parts.append(f'<text x="{cx:.1f}" y="{h - 14}" font-size="11" text-anchor="middle" fill="{t["muted"]}">{days[i]:%m-%d}</text>')

    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main():
    today = datetime.now(TZ).date()
    days = [today - timedelta(days=i) for i in range(DAYS - 1, -1, -1)]
    cohub, models = cohub_daily()
    cpa = keeper_daily()
    OUT.mkdir(exist_ok=True)
    for theme in THEMES:
        (OUT / f"token-usage-{theme}.svg").write_text(render(theme, days, cohub, cpa, models))
    print(f"wrote {OUT}/token-usage-{{light,dark}}.svg")


if __name__ == "__main__":
    main()
