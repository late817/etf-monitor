# -*- coding: utf-8 -*-
"""
台股主動式ETF每日持股抓取器
資料來源：各投信官網（法定每日全透明揭露）
輸出：docs/data/latest.json（儀表板讀取）、docs/data/report.json（診斷）
"""
import json
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
TPE = timezone(timedelta(hours=8))

DATE_PATTERNS = [
    r"(?:資料|交易|基準|淨值|揭露)?日期[:：\s]*?(20\d{2})[/\-.年](\d{1,2})[/\-.月](\d{1,2})",
    r"Trade\s*Date[:：\s]*?(20\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})",
    r"(20\d{2})[/\-.](\d{1,2})[/\-.](\d{1,2})",
]
CASH_KEYWORDS = ["現金", "附買回", "應收", "應付", "期貨", "申購", "買回款", "保證金", "存款", "CASH"]
WEIGHT_HEADERS = ["權重", "比重", "比例", "占比", "佔比", "weight", "%"]
SHARES_HEADERS = ["數量", "股數", "張數", "shares", "units", "quantity"]
CODE_HEADERS = ["代碼", "代號", "code", "symbol", "ticker"]
NAME_HEADERS = ["名稱", "name", "個股", "商品"]


def log(msg):
    print(f"[{datetime.now(TPE).strftime('%H:%M:%S')}] {msg}", flush=True)


def to_float(s):
    if s is None:
        return None
    s = str(s).replace(",", "").replace("%", "").replace(" ", "").strip()
    if s in ("", "-", "--", "N/A"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def find_date(text):
    """從頁面文字找出最可能的資料日期（取最近但不超過今天者）。"""
    today = datetime.now(TPE).date()
    candidates = []
    for pat in DATE_PATTERNS:
        for m in re.finditer(pat, text):
            try:
                d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
                if d <= today and (today - d).days < 400:
                    candidates.append(d)
            except ValueError:
                continue
        if candidates:
            break  # 優先採用較明確的模式
    return max(candidates).isoformat() if candidates else None


def header_index(headers, keywords):
    for i, h in enumerate(headers):
        hl = str(h).lower()
        if any(k in hl for k in keywords):
            return i
    return None


def parse_tables(tables):
    """在頁面所有表格中，挑出最像持股明細的一張並解析。"""
    best = []
    for tbl in tables:
        if not tbl or len(tbl) < 3:
            continue
        headers = [str(c).strip() for c in tbl[0]]
        wi = header_index(headers, WEIGHT_HEADERS)
        ci = header_index(headers, CODE_HEADERS)
        ni = header_index(headers, NAME_HEADERS)
        si = header_index(headers, SHARES_HEADERS)
        rows = tbl[1:] if wi is not None else tbl
        # 無表頭時的預設欄位順序：代碼 名稱 數量 權重
        if wi is None:
            ci, ni, si, wi = 0, 1, 2, 3
        holdings = []
        for r in rows:
            if len(r) <= wi:
                continue
            w = to_float(r[wi])
            if w is None or not (0 < w <= 100):
                continue
            code = str(r[ci]).strip() if ci is not None and ci < len(r) else ""
            name = str(r[ni]).strip() if ni is not None and ni < len(r) else ""
            shares = to_float(r[si]) if si is not None and si < len(r) else None
            if not code and not name:
                continue
            is_cash = any(k in (code + name).upper() for k in CASH_KEYWORDS)
            holdings.append({
                "code": code, "name": name, "shares": shares,
                "weight": round(w, 4), "cash_like": is_cash,
            })
        # 合理性檢查：至少5筆、權重加總在40%~120%之間
        total = sum(h["weight"] for h in holdings)
        if len(holdings) >= 5 and 40 <= total <= 120 and len(holdings) > len(best):
            best = holdings
    return best


def get_page_tables(page):
    """把頁面所有 <table> 序列化為二維陣列。"""
    return page.evaluate("""() =>
        Array.from(document.querySelectorAll('table')).map(t =>
            Array.from(t.querySelectorAll('tr')).map(tr =>
                Array.from(tr.querySelectorAll('th,td')).map(td => td.innerText.trim())
            )
        )
    """)


def try_click_tabs(page, click_texts):
    for text in click_texts or []:
        try:
            loc = page.get_by_text(text, exact=False).first
            if loc.count() > 0:
                loc.click(timeout=4000)
                page.wait_for_timeout(2500)
                return text
        except Exception:
            continue
    return None


def discover_url(page, list_url, code, timeout_ms):
    """到投信的ETF清單頁，找出含此ETF代碼的連結。"""
    page.goto(list_url, timeout=timeout_ms, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    href = page.evaluate(f"""() => {{
        const code = "{code}".toUpperCase();
        for (const a of document.querySelectorAll('a')) {{
            const t = (a.innerText || '').toUpperCase();
            const h = (a.href || '').toUpperCase();
            if (t.includes(code) || h.includes(code)) return a.href;
        }}
        return null;
    }}""")
    return href


def scrape_fund(browser, fund, issuer_cfg, timeout_s):
    timeout_ms = timeout_s * 1000
    ctx = browser.new_context(
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        locale="zh-TW",
    )
    page = ctx.new_page()
    result = {"etf": fund["code"], "name": fund["name"],
              "issuer": issuer_cfg["name"], "status": "error",
              "data_date": None, "holdings": [], "source_url": None, "error": None}
    try:
        url = (fund.get("url") or "").strip()
        if not url and issuer_cfg.get("url_template"):
            url = issuer_cfg["url_template"].format(code=fund["code"])
        if not url and issuer_cfg.get("list_url"):
            log(f"  {fund['code']} 無固定URL，到清單頁探索…")
            url = discover_url(page, issuer_cfg["list_url"], fund["code"], timeout_ms)
        if not url:
            raise RuntimeError("找不到該ETF的官網頁面（探索失敗）")
        result["source_url"] = url

        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        try:
            page.wait_for_selector(issuer_cfg.get("wait_for", "table"), timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(3000)

        holdings = parse_tables(get_page_tables(page))
        if not holdings:
            clicked = try_click_tabs(page, issuer_cfg.get("click_texts"))
            if clicked:
                log(f"  {fund['code']} 點擊分頁「{clicked}」後重新解析")
            holdings = parse_tables(get_page_tables(page))
        if not holdings:
            raise RuntimeError("頁面上找不到符合持股明細特徵的表格")

        body_text = page.evaluate("() => document.body.innerText")
        result["data_date"] = find_date(body_text)
        result["holdings"] = holdings
        result["status"] = "ok"
        log(f"  {fund['code']} 成功：{len(holdings)} 筆持股，資料日期 {result['data_date']}")
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        log(f"  {fund['code']} 失敗：{result['error']}")
    finally:
        ctx.close()
    return result


def main():
   import os
import yaml
try:
    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
except FileNotFoundError:
    cfg = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"), encoding="utf-8"))
settings = cfg.get("settings", {})
    stale_days = int(settings.get("stale_after_days", 4))
    timeout_s = int(settings.get("timeout_seconds", 45))
    today = datetime.now(TPE).date()

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for fund in cfg["funds"]:
            issuer_cfg = cfg["issuers"][fund["issuer"]]
            log(f"抓取 {fund['code']} {fund['name']}（{issuer_cfg['name']}）")
            r = scrape_fund(browser, fund, issuer_cfg, timeout_s)
            if r["status"] == "ok" and r["data_date"]:
                age = (today - datetime.fromisoformat(r["data_date"]).date()).days
                if age > stale_days:
                    r["status"] = "stale"
            results.append(r)
        browser.close()

    out = {
        "generated_at": datetime.now(TPE).strftime("%Y-%m-%d %H:%M"),
        "demo": False,
        "funds": results,
    }
    data_dir = ROOT / "docs" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "latest.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    ok = sum(1 for r in results if r["status"] == "ok")
    report = {
        "run_at": out["generated_at"],
        "summary": f"{ok}/{len(results)} 檔成功",
        "details": [{"etf": r["etf"], "issuer": r["issuer"], "status": r["status"],
                     "data_date": r["data_date"], "count": len(r["holdings"]),
                     "url": r["source_url"], "error": r["error"]} for r in results],
    }
    (data_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"完成：{report['summary']}")
    # 只要有任何一檔成功就視為部分成功（exit 0），全數失敗才報錯
    sys.exit(0 if ok > 0 else 1)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(1)
