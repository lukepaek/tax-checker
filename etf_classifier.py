"""
etf_classifier.py

tax-checker.html이 호출하는 /api/classify-ticker/<code> 엔드포인트.

흐름:
  브라우저(tax-checker.html) → 우리 Flask 서버 → 네이버 금융 ETF 목록 API
  (서버간 호출이라 CORS 문제 없음, 응답은 하루 1회만 네이버에서 새로 받아와서 캐싱)

기존 Flask 앱(app.py)에 붙이는 법:

    from etf_classifier import etf_bp
    app.register_blueprint(etf_bp)

로컬에서 단독 실행해보고 싶으면 이 파일 맨 아래 __main__ 블록을 사용하세요.
"""

import re
import time
import threading
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)

etf_bp = Blueprint("etf_classifier", __name__)

NAVER_ETF_LIST_URL = "https://finance.naver.com/api/sise/etfItemList.nhn"
CACHE_TTL_SECONDS = 24 * 60 * 60  # 하루 1회 갱신
REQUEST_TIMEOUT = 5

# 네이버는 Referer 없이 호출하면 이따금 빈 응답/차단을 줄 수 있어 브라우저처럼 보이는 헤더를 붙임
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/sise/etf.naver",
}

# ---------------------------------------------------------------------------
# ⚠ tabCode → 세제 카테고리 매핑에 대한 중요 주의사항
#
# 네이버 ETF 목록 API가 반환하는 각 종목의 "etfTabCode" 필드는 네이버
# ETF 페이지의 탭 구분(국내 시가총액형 / 국내 업종·테마 / 국내 파생 /
# 해외 주식 / 원자재 / 채권 / 기타)에 대응하는 값으로 알려져 있습니다.
#
# 세법상 "국내주식형 ETF"(매매차익 비과세)로 인정되려면 국내 주식에
# 60% 이상 투자해야 하므로, 대략 아래처럼 매핑했습니다:
#   1, 2 (국내 시가총액형 / 국내 업종·테마)      → domestic   (매매차익 비과세)
#   3, 4, 5, 6, 7 (국내파생·해외주식·원자재·채권·기타) → overseas_etf (매매차익도 배당소득세)
#
# 다만 네이버가 이 코드값을 바꾸거나, 국내파생(레버리지/인버스) 종목
# 중 실제로는 다르게 분류되는 예외가 있을 수 있습니다.
# 배포 전에 반드시 GET /api/classify-ticker/_debug 를 한 번 호출해서
# 탭코드별 종목명이 실제로 맞게 묶이는지 눈으로 확인하세요.
# ---------------------------------------------------------------------------
TAB_CODE_CATEGORY = {
    1: "domestic",
    2: "domestic",
    3: "overseas_etf",
    4: "overseas_etf",
    5: "overseas_etf",
    6: "overseas_etf",
    7: "overseas_etf",
}
DEFAULT_CATEGORY_FOR_UNKNOWN_TAB = "overseas_etf"  # 모르는 코드는 보수적으로(세금 더 나오는 쪽으로) 처리

_cache_lock = threading.Lock()
_cache = {"data": None, "fetched_at": 0.0}


def _fetch_and_build_map():
    """네이버에서 전체 ETF 목록을 받아 {종목코드: {name, tabCode, category}} 형태로 변환"""
    resp = requests.get(NAVER_ETF_LIST_URL, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    items = payload.get("result", {}).get("etfItemList", [])

    mapping = {}
    for it in items:
        code = it.get("itemcode")
        if not code:
            continue
        tab = it.get("etfTabCode")
        mapping[code] = {
            "name": it.get("itemname"),
            "tabCode": tab,
            "category": TAB_CODE_CATEGORY.get(tab, DEFAULT_CATEGORY_FOR_UNKNOWN_TAB),
        }

    if not mapping:
        raise ValueError("네이버 응답에 ETF 목록이 비어있습니다 (응답 포맷이 바뀌었을 수 있음)")

    return mapping


def get_etf_map(force_refresh=False):
    """캐시된 ETF 맵을 반환. 하루 지났으면 자동 갱신, 갱신 실패시 기존 캐시라도 계속 사용."""
    with _cache_lock:
        is_stale = (
            _cache["data"] is None
            or (time.time() - _cache["fetched_at"]) > CACHE_TTL_SECONDS
        )
        cached = _cache["data"]

    if not is_stale and not force_refresh:
        return cached

    try:
        fresh = _fetch_and_build_map()
    except Exception as e:
        logger.warning("네이버 ETF 목록 갱신 실패, 기존 캐시 유지: %s", e)
        if cached is not None:
            return cached
        raise

    with _cache_lock:
        _cache["data"] = fresh
        _cache["fetched_at"] = time.time()
    return fresh


@etf_bp.route("/api/classify-ticker/<code>")
def classify_ticker(code):
    code = (code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return jsonify({"error": "6자리 국내 종목코드만 지원합니다"}), 400

    try:
        etf_map = get_etf_map()
    except Exception as e:
        return jsonify({"error": f"네이버 ETF 목록 조회 실패: {e}"}), 502

    hit = etf_map.get(code)
    if hit:
        return jsonify(
            {
                "code": code,
                "name": hit["name"],
                "isEtf": True,
                "category": hit["category"],
                "tabCode": hit["tabCode"],
            }
        )

    # ETF 목록에 없는 6자리 코드 → 개별 국내주식으로 간주 (매매차익 비과세)
    # 주의: 존재하지 않는 코드를 넣어도 여기로 떨어져 domestic으로 표시됨
    return jsonify({"code": code, "name": None, "isEtf": False, "category": "domestic"})


@etf_bp.route("/api/classify-ticker/_debug")
def classify_debug():
    """tabCode별 종목명 샘플을 보여줌 — 매핑이 실제로 맞는지 배포 전에 한 번 확인하는 용도"""
    etf_map = get_etf_map(force_refresh=True)
    by_tab = {}
    for code, info in etf_map.items():
        by_tab.setdefault(info["tabCode"], []).append(f'{code} {info["name"]}')
    return jsonify(
        {
            "total_etf_count": len(etf_map),
            "by_tab_code": {str(tab): names[:8] for tab, names in sorted(by_tab.items(), key=lambda x: (x[0] is None, x[0]))},
        }
    )


# ---------------------------------------------------------------------------
# 현재가 · 배당수익률 조회 (/api/quote/<code>)
#
# 현재가는 네이버 비공식 JSON API로 자동 조회:
#   polling.finance.naver.com/api/realtime/domestic/stock/{code}
#
# ⚠ 배당수익률(ETF는 "분배율")은 자동 수집처를 못 찾았습니다:
#   - 네이버: ETF엔 배당수익률 필드 자체가 없음 (개별주식만 있음, 확인됨)
#   - KRX 공식 API: 분배율 필드 없음
#   - 운용사(TIGER/KODEX 등): 운용사마다 사이트가 달라 자동화하려면
#     운용사별로 각각 스크레이퍼를 만들어야 함 (일 커짐)
# 그래서 개별주식은 네이버 API로 자동 조회하고, ETF는 아래
# MANUAL_DIVIDEND_YIELD 딕셔너리에 직접 적어둔 값을 씁니다.
# 값 찾는 곳: 운용사 상품 상세페이지 (TIGER면 investments.miraeasset.com/tigeretf,
# KODEX면 kodex.com 등) — 대략 분기~반기에 한 번씩 업데이트하면 충분합니다.
#
# ⚠ "배당주기"(월배당/분기배당 등)도 마찬가지로 공개 API가 없어서,
# 프론트엔드(tax-checker.html)에서 종목명에 "월배당" 표기가 있는지로만 추정합니다.
#
# 시세는 하루 종일 캐싱하면 안 되니 TTL을 30초로 짧게 둡니다.
# ---------------------------------------------------------------------------
REALTIME_QUOTE_URL = "https://polling.finance.naver.com/api/realtime/domestic/stock/{code}"
INTEGRATION_URL = "https://m.stock.naver.com/api/stock/{code}/integration"
QUOTE_CACHE_TTL_SECONDS = 30

# 종목코드 → 연환산 배당수익률(분배율) %. 자주 보는 ETF만 채워두고 가끔 업데이트하세요.
MANUAL_DIVIDEND_YIELD = {
    # "458730": 3.8,  # TIGER 미국배당다우존스 — 예시. 실제 값으로 바꿔서 쓰세요.
    # "379800": 1.3,  # KODEX 미국S&P500
}

_quote_cache_lock = threading.Lock()
_quote_cache = {}  # code -> (fetched_at, data)


def _fetch_quote(code):
    r1 = requests.get(REALTIME_QUOTE_URL.format(code=code), headers=_HEADERS, timeout=REQUEST_TIMEOUT)
    r1.raise_for_status()
    datas = r1.json().get("datas") or []
    price_info = datas[0] if datas else {}

    r2 = requests.get(INTEGRATION_URL.format(code=code), headers=_HEADERS, timeout=REQUEST_TIMEOUT)
    r2.raise_for_status()
    total_infos = r2.json().get("totalInfos") or []
    info_map = {row.get("code"): row.get("value") for row in total_infos}

    # 수동 등록값이 있으면 그게 우선 (ETF는 API에 값이 없는 경우가 대부분이라)
    manual_yield = MANUAL_DIVIDEND_YIELD.get(code)
    if manual_yield is not None:
        dividend_yield = manual_yield
        dividend_source = "manual"
    else:
        dividend_yield = info_map.get("dividendYieldRatio")
        dividend_source = "api" if dividend_yield else None

    return {
        "code": code,
        "price": price_info.get("closePrice"),
        "changePrice": price_info.get("compareToPreviousClosePrice"),
        "changeRate": price_info.get("fluctuationsRatio"),
        "marketStatus": price_info.get("marketStatus"),
        "dividendYieldRatio": dividend_yield,
        "dividendYieldSource": dividend_source,  # "api" | "scrape" | None — 프론트에서 출처 표시용
        "dividendPerShare": info_map.get("dividend"),
    }


def get_quote(code, force_refresh=False):
    with _quote_cache_lock:
        cached = _quote_cache.get(code)
    if not force_refresh and cached and (time.time() - cached[0]) < QUOTE_CACHE_TTL_SECONDS:
        return cached[1]

    data = _fetch_quote(code)
    with _quote_cache_lock:
        _quote_cache[code] = (time.time(), data)
    return data


@etf_bp.route("/api/quote/<code>")
def quote_ticker(code):
    code = (code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return jsonify({"error": "6자리 국내 종목코드만 지원합니다"}), 400
    try:
        return jsonify(get_quote(code))
    except Exception as e:
        return jsonify({"error": f"시세 조회 실패: {e}"}), 502


# ---------------------------------------------------------------------------
# 일별 시세(차트용) 조회 (/api/chart/<code>)
#
# - 국내 6자리 코드: 네이버 fchart 비공식 XML API (일봉)
#     https://fchart.stock.naver.com/sise.nhn?symbol=CODE&timeframe=day&count=N&requestType=0
#   응답이 XML이고 각 <item data="20260101|시가|고가|저가|종가|거래량|외국인비율"/> 형태.
#
# - 해외(미국) 티커: 2단계 폴백
#     1순위) 야후 파이낸스 비공식 차트 JSON API (가장 안정적, 대부분 티커 지원)
#         https://query1.finance.yahoo.com/v8/finance/chart/{TICKER}?range=6mo&interval=1d
#     2순위) stooq.com 무료 CSV 종가 API (야후가 막히거나 없는 티커일 때 폴백)
#         https://stooq.com/q/d/l/?s={ticker}.us&i=d
#   ⚠ 두 소스 모두 반드시 일반 브라우저 헤더를 써야 합니다 — 네이버용 Referer를
#   그대로 보내면 야후/스투크가 이상한 요청으로 보고 빈 응답을 줄 수 있어서
#   국내용 _HEADERS와는 별도로 _US_HEADERS를 씁니다.
#
# 전부 비공식/무료 소스라 스키마가 예고 없이 바뀔 수 있습니다.
# 10분 캐싱 — 차트는 실시간까지는 필요 없어서 시세(30초)보다 길게 잡음.
# ---------------------------------------------------------------------------
KR_CHART_URL = "https://fchart.stock.naver.com/sise.nhn"
YAHOO_CHART_URL_TMPL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
STOOQ_CHART_URL_TMPL = "https://stooq.com/q/d/l/?s={ticker}.us&i=d"
CHART_CACHE_TTL_SECONDS = 10 * 60
CHART_POINT_COUNT = 90  # 대략 최근 4~5개월치 거래일

_US_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

_chart_cache_lock = threading.Lock()
_chart_cache = {}  # "kr:005930" / "us:AAPL" -> (fetched_at, points)


def _fetch_kr_chart(code, count=CHART_POINT_COUNT):
    params = {"symbol": code, "timeframe": "day", "count": str(count), "requestType": "0"}
    resp = requests.get(KR_CHART_URL, params=params, headers=_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    points = []
    for item in root.findall(".//item"):
        raw = item.get("data")
        if not raw:
            continue
        parts = raw.split("|")
        if len(parts) < 5:
            continue
        date_s, close_s = parts[0], parts[4]
        try:
            close = float(close_s)
        except (TypeError, ValueError):
            continue
        if not date_s or len(date_s) != 8 or close <= 0:
            continue
        points.append({"date": f"{date_s[0:4]}-{date_s[4:6]}-{date_s[6:8]}", "close": close})
    return points


def _fetch_us_chart_yahoo(ticker, count=CHART_POINT_COUNT):
    params = {"range": "6mo", "interval": "1d"}
    resp = requests.get(
        YAHOO_CHART_URL_TMPL.format(ticker=ticker),
        params=params,
        headers=_US_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    results = ((payload.get("chart") or {}).get("result")) or []
    if not results:
        err = ((payload.get("chart") or {}).get("error")) or {}
        raise ValueError(err.get("description") or "야후 파이낸스 응답에 데이터가 없습니다")

    node = results[0]
    timestamps = node.get("timestamp") or []
    quote = (((node.get("indicators") or {}).get("quote")) or [{}])[0]
    closes = quote.get("close") or []

    points = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        date_s = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        points.append({"date": date_s, "close": float(close)})
    return points[-count:]


def _fetch_us_chart_stooq(ticker, count=CHART_POINT_COUNT):
    url = STOOQ_CHART_URL_TMPL.format(ticker=ticker.lower())
    resp = requests.get(url, headers=_US_HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    lines = resp.text.strip().splitlines()
    points = []
    for line in lines[1:]:  # 첫 줄은 헤더(Date,Open,High,Low,Close,Volume)
        cols = line.split(",")
        if len(cols) < 5:
            continue
        date_s, close_s = cols[0], cols[4]
        try:
            close = float(close_s)
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        points.append({"date": date_s, "close": close})
    return points[-count:]


def _fetch_us_chart(ticker, count=CHART_POINT_COUNT):
    """야후를 먼저 시도하고, 실패하거나 빈 데이터면 stooq로 폴백."""
    errors = []
    for label, fetcher in (("야후", _fetch_us_chart_yahoo), ("stooq", _fetch_us_chart_stooq)):
        try:
            points = fetcher(ticker, count)
            if points:
                return points
            errors.append(f"{label}: 데이터 없음")
        except Exception as e:
            errors.append(f"{label}: {e}")
    raise ValueError(" / ".join(errors) if errors else "데이터를 가져오지 못했습니다")


def get_chart(market, code, force_refresh=False):
    key = f"{market}:{code}"
    with _chart_cache_lock:
        cached = _chart_cache.get(key)
        is_stale = cached is None or (time.time() - cached[0]) > CHART_CACHE_TTL_SECONDS

    if not is_stale and not force_refresh:
        return cached[1]

    try:
        points = _fetch_kr_chart(code) if market == "kr" else _fetch_us_chart(code)
    except Exception as e:
        logger.warning("차트 조회 실패(%s), 기존 캐시 유지: %s", key, e)
        with _chart_cache_lock:
            cached = _chart_cache.get(key)
        if cached is not None:
            return cached[1]
        raise

    if not points:
        raise ValueError("차트 데이터가 비어있습니다 (응답 포맷이 바뀌었을 수 있음)")

    with _chart_cache_lock:
        _chart_cache[key] = (time.time(), points)
    return points


@etf_bp.route("/api/chart/<code>")
def chart_ticker(code):
    code = (code or "").strip()
    if re.fullmatch(r"\d{6}", code):
        market = "kr"
    elif re.fullmatch(r"[A-Za-z.]{1,6}", code):
        market = "us"
        code = code.upper()
    else:
        return jsonify({"error": "지원하지 않는 티커 형식입니다 (국내 6자리 코드 또는 영문 1~6자 티커만 지원)"}), 400

    try:
        points = get_chart(market, code)
    except Exception as e:
        # 프론트엔드가 "차트 조회 실패: " 접두어를 붙이므로 여기서는 원인만 반환 (중복 방지)
        return jsonify({"error": str(e)}), 502

    return jsonify({"code": code, "market": market, "points": points})


@etf_bp.route("/api/chart/_debug/<code>")
def chart_debug(code):
    """해외 티커 하나에 대해 야후/stooq 각각 성공했는지, 몇 개 포인트가 왔는지 개별 확인용."""
    code = (code or "").strip().upper()
    out = {"code": code}
    try:
        pts = _fetch_us_chart_yahoo(code)
        out["yahoo"] = {"ok": True, "count": len(pts), "last": pts[-1] if pts else None}
    except Exception as e:
        out["yahoo"] = {"ok": False, "error": str(e)}
    try:
        pts = _fetch_us_chart_stooq(code)
        out["stooq"] = {"ok": True, "count": len(pts), "last": pts[-1] if pts else None}
    except Exception as e:
        out["stooq"] = {"ok": False, "error": str(e)}
    return jsonify(out)


if __name__ == "__main__":
    # 단독 실행: python etf_classifier.py 로 이 블루프린트만 테스트
    from flask import Flask

    logging.basicConfig(level=logging.INFO)
    app = Flask(__name__)
    app.register_blueprint(etf_bp)
    print("테스트: http://127.0.0.1:5001/api/classify-ticker/_debug")
    print("테스트: http://127.0.0.1:5001/api/classify-ticker/458730")
    print("테스트: http://127.0.0.1:5001/api/quote/458730")
    print("테스트: http://127.0.0.1:5001/api/chart/458730")
    print("테스트: http://127.0.0.1:5001/api/chart/AAPL")
    print("디버그(해외 티커 소스별 확인): http://127.0.0.1:5001/api/chart/_debug/IREN")
    app.run(port=5001, debug=True)