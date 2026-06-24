"""
飲酒ログ Web入力フォーム（Render.com デプロイ用）
  - /          → 入力フォーム
  - /drinks/log → 入力フォーム（別名）
  - POST /api/drinks/add → Notion DB にページ作成 + Gemini Vision 解析
"""

import base64
import json
import os
import re
import urllib.request
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

BASE_DIR     = Path(__file__).parent
PRESETS_PATH = BASE_DIR / "data" / "drink_presets.json"

app = Flask(__name__)


def load_presets() -> dict:
    if PRESETS_PATH.exists():
        with open(PRESETS_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


# ── Notion ────────────────────────────────────────────────────

def notion_headers():
    token = os.environ.get("NOTION_TOKEN", "").strip()
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def notion_post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"https://api.notion.com/v1/{path}",
        data=data, headers=notion_headers(), method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def notion_patch(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"https://api.notion.com/v1/{path}",
        data=data, headers=notion_headers(), method="PATCH",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# ── Gemini Vision ─────────────────────────────────────────────

def gemini_vision(img_bytes: bytes, category: str) -> dict:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model   = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    if not api_key:
        return {}

    if category == "ワイン":
        prompt = (
            'このワインラベルの画像から以下をJSONのみで返答。不明な項目はnull。'
            '価格は推定せず、ラベルに無ければnull。\n'
            '{"type":"red|white|rose|sparkling","region":"産地","producer":"生産者",'
            '"vintage":年(数値),"grapes":["品種"],"abv":度数(数値),"retail_price":小売価格円(数値またはnull)}'
        )
    else:
        prompt = (
            'この日本酒ラベルの画像から以下をJSONのみで返答。不明な項目はnull。'
            '価格は推定せず、ラベルに無ければnull。\n'
            '{"brand":"銘柄","brewery":"蔵元","prefecture":"都道府県","city":"市町村",'
            '"rice":"使用米","grade":"純米/吟醸/純米吟醸/大吟醸等","polish_ratio":精米歩合(数値),'
            '"abv":度数(数値),"retail_price":小売価格円(数値またはnull)}'
        )

    body = json.dumps({
        "contents": [{"parts": [
            {"text": prompt},
            {"inline_data": {"mime_type": "image/jpeg",
                             "data": base64.b64encode(img_bytes).decode()}},
        ]}]
    }).encode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read())
        raw = resp["candidates"][0]["content"]["parts"][0]["text"]
        m = re.search(r'\{[\s\S]*\}', raw)
        return json.loads(m.group()) if m else {}
    except Exception:
        return {}


# ── Routes ───────────────────────────────────────────────────

@app.route("/")
@app.route("/drinks/log")
def drinks_log():
    return render_template("drinks_log.html", presets=load_presets())


@app.route("/api/drinks/add", methods=["POST"])
def api_drinks_add():
    NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
    DRINKS_DB_ID = os.environ.get("DRINKS_DB_ID", "").strip()

    category = request.form.get("category", "").strip()
    serving  = request.form.get("serving", "").strip()
    name     = request.form.get("name", "").strip()
    count    = int(request.form.get("count") or 1)
    place    = request.form.get("place", "").strip()
    note     = request.form.get("note", "").strip()
    dt_str   = request.form.get("datetime", "").strip()

    if not category:
        return jsonify({"ok": False, "error": "カテゴリは必須です"}), 400
    if not dt_str:
        dt_str = datetime.now().strftime("%Y-%m-%dT%H:%M")

    # ── プリセットから純アルコールg ──
    presets = load_presets()
    cat_key = {
        "ビール": "beer", "ワイン": "wine", "日本酒": "sake",
        "ハイボール": "highball", "サワー": "sour", "その他": "other",
    }.get(category, "other")
    cat_pre = presets.get(cat_key, {})
    preset  = cat_pre.get(serving) or cat_pre.get("default") or (
        list(cat_pre.values())[0] if cat_pre else {}
    )
    volume_ml = (preset.get("volume_ml", 200) * count) if preset else None
    abv       = preset.get("abv", 5.0) if preset else None
    alcohol_g = round(volume_ml * (abv / 100) * 0.8, 1) if (volume_ml and abv) else None

    # ── ワイン/日本酒 写真 → Gemini Vision ──
    wine_data = None
    sake_data = None
    meta      = ""
    photo_file = request.files.get("photo")
    if photo_file and photo_file.filename and category in ("ワイン", "日本酒"):
        img_bytes = photo_file.read()
        vision = gemini_vision(img_bytes, category)
        if vision:
            if vision.get("abv"):
                abv = float(vision["abv"])
                volume_ml = 120 * count if category == "ワイン" else 180 * count
                alcohol_g = round(volume_ml * (abv / 100) * 0.8, 1)
            if category == "ワイン":
                wine_data = vision
                parts = [str(v) for k, v in vision.items() if v and k not in ("abv", "retail_price")]
                meta = " / ".join(parts)
            else:
                sake_data = vision
                parts = [vision.get("brand"), vision.get("brewery"), vision.get("grade"),
                         f"精米{vision.get('polish_ratio')}%" if vision.get("polish_ratio") else None]
                meta = " / ".join(p for p in parts if p)

    # ── Notion ページ作成 ──
    notion_page_id = None
    if NOTION_TOKEN and DRINKS_DB_ID:
        title_text = f"{name}（{category}）" if name else f"{category} {dt_str[:10]}"
        props = {
            "Name":     {"title": [{"type": "text", "text": {"content": title_text}}]},
            "日時":     {"date": {"start": dt_str}},
            "カテゴリ": {"select": {"name": category}},
            "杯数":     {"number": count},
            "解析済み": {"checkbox": True},
        }
        if serving:
            props["提供（ビール）"] = {"select": {"name": serving}}
        if name:
            props["銘柄・商品名"] = {"rich_text": [{"type": "text", "text": {"content": name}}]}
        if place:
            props["店・場所"] = {"rich_text": [{"type": "text", "text": {"content": place}}]}
        if note:
            props["メモ"]    = {"rich_text": [{"type": "text", "text": {"content": note}}]}
        if alcohol_g is not None:
            props["純アルコールg"] = {"number": alcohol_g}
        if meta:
            props["解析メタ"] = {"rich_text": [{"type": "text", "text": {"content": meta[:2000]}}]}
        try:
            resp = notion_post("pages", {"parent": {"database_id": DRINKS_DB_ID}, "properties": props})
            notion_page_id = resp["id"]
        except Exception as e:
            return jsonify({"ok": False, "error": f"Notion エラー: {e}"}), 500

    return jsonify({
        "ok":        True,
        "id":        notion_page_id,
        "alcohol_g": alcohol_g,
        "meta":      meta,
        "notion":    notion_page_id is not None,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5052))
    app.run(host="0.0.0.0", port=port, debug=False)
