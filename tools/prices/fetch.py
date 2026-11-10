#!/usr/bin/env python3
"""tota 앱 '제철' 카드에 쓰는 이번 주 소매가를 받아 data/prices.json 으로 남긴다.

데이터: 공공데이터포털 '한국농수산식품유통공사_일별 도,소매 가격정보 조회'
        https://apis.data.go.kr/B552845/perDay/price (KAMIS 소매가, 시장별·날짜별)
인증키: 환경 변수 DATA_GO_KR_SERVICE_KEY (GitHub 저장소 Secrets). 디코딩 키를 넣는다.
        인코딩 키(%가 든 것)를 넣어도 한 번 풀어서 쓴다.

품목마다 최근 15일 소매가(구분코드 01)를 받아서,
- 등급은 '상품'을 먼저, 품종·단위는 가장 많은 시장이 조사한 것 하나를 고르고
- 날짜별로 전국 시장 평균을 내서
- 가장 최근 조사일 값과 그 7일 전(앞뒤 2일 안에서 가장 가까운 날) 값을 남긴다.

두부·면·떡처럼 KAMIS에 없는 가공식품은 '국가데이터처_온라인 수집 가격 정보'
(https://apis.data.go.kr/1240000/bpp_openapi, 같은 키로 활용신청)에서 받는다.
온라인 쇼핑몰 상품마다 이름에서 무게를 읽어 kg당 값을 내고, 가운데 값을 쓴다.
이 API는 한 달쯤 늦게 채워질 수 있어서 최근 70일 안에서 가장 새 값을 쓴다. 못 받아도 실패로 치지 않는다.

받기에 실패한 품목은 지난 파일의 값을 그대로 둔다. 하나도 못 받으면 파일을 안 바꾸고 실패로 끝난다.
표준 라이브러리만 쓴다 (Actions에서 설치 없이 돈다).

    DATA_GO_KR_SERVICE_KEY=... python3 tools/prices/fetch.py data/prices.json
    python3 tools/prices/fetch.py --test    # 키 없이 묶는 규칙만 확인
"""
import datetime as dt
import json
import re
import statistics
import xml.etree.ElementTree as ET
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://apis.data.go.kr/B552845/perDay/price"
RETAIL = "01"  # 구분코드: 01 소매, 02 중도매
DAYS = 15
SOURCE = "농산물유통정보(KAMIS) 전국 평균 소매가"

# (앱에서 쓰는 이름, [(부류코드, 품목코드, 품종코드 또는 None), ...]). 앞의 출처에서 못 받으면 다음 출처.
# 이름은 앱 HomeSlot.seasons 재료 이름, TodayMenu 재료 이름과 같아야 한다.
def one(ctgry, item, vrty=None):
    return [(ctgry, item, vrty)]


ITEMS = [
    # 제철 재료
    ("귤", one("400", "415")), ("시금치", one("200", "213")), ("굴", one("600", "644")),
    ("딸기", one("200", "226")), ("미나리", one("200", "252")), ("참외", one("200", "222")),
    ("양파", one("200", "245", "00")), ("감자", one("100", "152")), ("오이", one("200", "223")),
    ("수박", one("200", "221")), ("복숭아", one("400", "413")), ("포도", one("400", "414")),
    ("배", one("400", "412")), ("꽃게", one("600", "656")), ("전어", one("600", "663")),
    ("사과", one("400", "411")), ("고구마", one("100", "151")), ("배추", one("200", "211")),
    # 늘 사는 채소·과일
    ("대파", one("200", "246", "00")), ("무", one("200", "231")), ("당근", one("200", "232")),
    ("양배추", one("200", "212")), ("바나나", one("400", "418")),
    ("애호박", one("200", "224", "01")), ("콩나물", one("200", "265")), ("부추", one("200", "254")),
    ("깻잎", one("200", "253")), ("상추", one("200", "214")), ("풋고추", one("200", "242", "00")),
    ("깐마늘", one("200", "258")), ("느타리버섯", one("300", "315", "00")), ("새송이버섯", one("300", "317")),
    ("팽이버섯", one("300", "316")), ("브로콜리", one("200", "280")), ("파프리카", one("200", "256")),
    # 고기·계란 (소매 품목이 없으면 축산 품목)
    ("돼지고기 앞다리", [("500", "514", "04"), ("500", "4304", "25")]),
    ("삼겹살", [("500", "514", "00"), ("500", "4304", "27")]),
    ("돼지고기 목살", [("500", "514", "02"), ("500", "4304", "68")]),
    ("소고기 불고기", [("500", "512", "13"), ("500", "512", "23")]),
    ("소고기 국거리", [("500", "512", "14"), ("500", "4301", "40")]),
    ("닭고기", [("500", "515", None), ("500", "9901", "99")]),
    ("계란", [("500", "516", None), ("500", "9903", "23")]),
    # 생선·해산물
    ("고등어", one("600", "611")), ("갈치", one("600", "613")), ("오징어", one("600", "619")),
    ("동태", one("600", "615")), ("새우", one("600", "654")), ("홍합", one("600", "658")),
    ("바지락", one("600", "661")),
]


def service_key():
    key = os.environ.get("DATA_GO_KR_SERVICE_KEY", "").strip()
    if not key:
        sys.exit("DATA_GO_KR_SERVICE_KEY 가 없어요 (저장소 Settings → Secrets and variables → Actions)")
    return urllib.parse.unquote(key) if "%" in key else key


def get_json(params, tries=3):
    url = API + "?" + urllib.parse.urlencode(params)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                body = r.read().decode("utf-8")
            return json.loads(body)
        except urllib.error.HTTPError as e:
            # 키 문제(403 등)는 다시 해도 같다. 게이트웨이 메시지를 그대로 보여 준다
            text = e.read().decode("utf-8", "replace")
            try:
                gateway_error(json.loads(text))
            except (ValueError, AttributeError):
                pass
            if e.code < 500 or attempt == tries - 1:
                raise RuntimeError(f"HTTP {e.code}: {text[:200]}") from None
            time.sleep(2 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            if attempt == tries - 1:
                raise RuntimeError(f"요청 실패: {type(e).__name__}: {str(e)[:200]}") from None
            time.sleep(2 * (attempt + 1))


def gateway_error(payload):
    """공공데이터포털 게이트웨이 오류 ({"OpenAPI_ServiceResponse": {"cmmMsgHeader": ...}}) 면 알아보기 쉽게 멈춘다."""
    header = (payload.get("OpenAPI_ServiceResponse") or {}).get("cmmMsgHeader")
    if header:
        hint = {
            "SERVICE_KEY_IS_NOT_REGISTERED_ERROR": "키가 틀렸거나 아직 활용 승인이 안 됐어요 (승인 뒤 1시간쯤 걸릴 수 있어요)",
            "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS_ERROR": "하루 호출 한도를 넘었어요",
        }.get(header.get("errMsg"), "")
        raise RuntimeError(f"포털 오류 {header.get('returnReasonCode')} {header.get('errMsg')} {hint}".strip())


def rows_of(payload):
    """응답에서 item 목록만. {"response": {...}} 로 싸여 오든 아니든, item이 하나면 객체로 오든 받는다."""
    gateway_error(payload)
    root = payload.get("response", payload)
    header = root.get("header") or {}
    code = str(header.get("resultCode", "00"))
    if code not in ("00", "0", "000", "INFO-000"):
        raise RuntimeError(f"API 오류 {code}: {header.get('resultMsg')}")
    body = root.get("body") or {}
    items = body.get("items") or {}
    item = items.get("item", []) if isinstance(items, dict) else items
    if isinstance(item, dict):
        item = [item]
    return list(item or []), int(body.get("totalCount") or 0)


def fetch_item(key, ctgry, item, vrty, start, end):
    rows, page = [], 1
    while True:
        params = {
            "serviceKey": key, "returnType": "JSON", "pageNo": page, "numOfRows": 1000,
            "cond[exmn_ymd::GTE]": start, "cond[exmn_ymd::LTE]": end,
            "cond[se_cd::EQ]": RETAIL, "cond[ctgry_cd::EQ]": ctgry, "cond[item_cd::EQ]": item,
        }
        if vrty:
            params["cond[vrty_cd::EQ]"] = vrty
        payload = get_json(params)
        got, total = rows_of(payload)
        rows += got
        if not got or len(rows) >= total or page >= 20:
            return rows
        page += 1


def number(text):
    try:
        return float(str(text).replace(",", "").strip())
    except ValueError:
        return None


def unit_label(row):
    size = number(row.get("unit_sz"))
    unit = (row.get("unit") or "").strip()
    if size is None:
        return unit
    size_text = str(int(size)) if size == int(size) else f"{size:g}"
    return f"{size_text}{unit}" if not unit.startswith(size_text) else unit


def summarize(name, rows):
    """시장별 행들 → {key, unit, price, weekAgo, date}. 쓸 만한 게 없으면 None."""
    rows = [r for r in rows if str(r.get("se_cd", RETAIL)) == RETAIL and (number(r.get("exmn_dd_prc")) or 0) > 0]
    if not rows:
        return None
    groups = {}
    for r in rows:
        g = (r.get("vrty_cd"), r.get("grd_nm"), r.get("unit"), r.get("unit_sz"))
        groups.setdefault(g, []).append(r)
    latest = max(r["exmn_ymd"] for r in rows)

    def rank(item):
        (_, grade, _, _), rs = item
        on_latest = sum(1 for r in rs if r["exmn_ymd"] == latest)
        return (grade == "상품", on_latest, len(rs))

    _, chosen = max(groups.items(), key=rank)

    def average(rs, field):
        values = [v for v in (number(r.get(field)) for r in rs) if v and v > 0]
        return round(sum(values) / len(values)) if values else None

    by_day = {}
    for r in chosen:
        by_day.setdefault(r["exmn_ymd"], []).append(r)
    day = max(by_day)
    d = dt.datetime.strptime(day, "%Y%m%d").date()
    out = {"key": name, "unit": unit_label(chosen[0]), "price": average(by_day[day], "exmn_dd_prc"), "date": d.isoformat()}
    per_kg = average(by_day[day], "exmn_dd_cnvs_prc")
    if per_kg:
        out["perKg"] = per_kg
    for gap in (0, -1, 1, -2, 2):
        target = (d - dt.timedelta(days=7 + gap)).strftime("%Y%m%d")
        if target in by_day:
            week_ago = average(by_day[target], "exmn_dd_prc")
            if week_ago:
                out["weekAgo"] = week_ago
            break
    return out


# ---------- 가공식품: 국가데이터처 온라인 수집 가격 ----------

ONLINE_API = "https://apis.data.go.kr/1240000/bpp_openapi"
ONLINE_SOURCE = "국가데이터처 온라인 수집 가격"
ONLINE_DAYS = 70
# (앱 재료 이름, 품목 이름에 들어갈 말, 상품 이름에 꼭 있어야 할 말 중 하나, 빼야 할 말)
ONLINE_ITEMS = [
    ("두부", ["두부"], ["두부"], ["순두부", "연두부", "유부", "과자", "스낵", "면", "콩물", "강아지"]),
    ("칼국수 면", ["국수", "면"], ["칼국수"], ["라면", "컵", "스프", "육수"]),
    ("떡", ["떡"], ["떡볶이떡", "떡볶이 떡", "떡볶이용", "밀떡", "쌀떡"], ["소스", "양념", "밀키트", "컵", "즉석"]),
    ("어묵", ["어묵"], ["어묵"], ["탕", "소스", "꼬치"]),
    ("당면", ["당면"], ["당면"], ["잡채밀키트", "컵", "만두"]),
    ("마른미역", ["미역"], ["미역"], ["줄기", "미역국", "즉석", "라면", "다시마"]),
]
WEIGHT = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|킬로|g|그램)(?![a-z])", re.I)
COUNT = re.compile(r"(?:[x×*]\s*(\d+)(?!\s*(?:g|kg))|(\d+)\s*(?:개|입|팩|봉|봉지|모|ea|EA)(?![가-힣]))")


def grams_of(name):
    """상품 이름에서 전체 무게(g). '300g x 3', '1kg', '(380g*2개)'. 못 읽으면 None."""
    weights = [(float(n), u.lower()) for n, u in WEIGHT.findall(name)]
    if not weights:
        return None
    n, u = weights[0]
    grams = n * 1000 if u in ("kg", "킬로") else n
    counts = [int(a or b) for a, b in COUNT.findall(WEIGHT.sub(" ", name, count=1))]
    count = counts[0] if counts and 1 <= counts[0] <= 40 else 1
    total = grams * count
    return total if 20 <= total <= 10000 else None


def online_rows(text):
    """XML 응답 → item 사전 목록. 게이트웨이·API 오류면 RuntimeError."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        try:
            gateway_error(json.loads(text))
        except (ValueError, AttributeError):
            pass
        raise RuntimeError(f"알 수 없는 응답: {text[:200]}") from None
    err = root.findtext(".//errMsg") or root.findtext(".//returnAuthMsg")
    if err:
        reason = root.findtext(".//returnReasonCode") or ""
        hint = "이 API에 활용신청이 안 됐거나 승인 대기 중이에요" if "NOT_REGISTERED" in err else ""
        raise RuntimeError(f"포털 오류 {reason} {err} {hint}".strip())
    code = (root.findtext(".//resultCode") or "00").strip()
    if code not in ("00", "0", "000", "INFO-000", "INFO-200"):
        raise RuntimeError(f"API 오류 {code}: {root.findtext('.//resultMsg')}")
    total = int((root.findtext(".//totalCount") or "0").strip() or 0)
    return [{c.tag: (c.text or "").strip() for c in item} for item in root.iter("item")], total


def get_online(path, params, tries=3):
    url = f"{ONLINE_API}/{path}?" + urllib.parse.urlencode(params)
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=40) as r:
                return online_rows(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code < 500 or attempt == tries - 1:
                try:
                    online_rows(text)
                except RuntimeError as inner:
                    raise inner from None
                raise RuntimeError(f"HTTP {e.code}: {text[:200]}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == tries - 1:
                raise RuntimeError(f"요청 실패: {type(e).__name__}: {str(e)[:200]}") from None
        time.sleep(2 * (attempt + 1))


def online_paged(key, path, extra):
    rows, page = [], 1
    while True:
        got, total = get_online(path, {"serviceKey": key, "pageNo": page, "numOfRows": 1000, **extra})
        rows += got
        if not got or len(rows) >= total or page >= 30:
            return rows
        page += 1


def summarize_online(name, rows, must, avoid):
    """상품별 행 → {key, unit:'1kg', price, perKg, weekAgo?, date, source}. 쓸 만한 게 없으면 None.
    상품 이름에 must 중 하나가 있고 avoid가 없는 것만, 무게를 읽을 수 있는 것만. 날짜마다 kg당 값의 가운데 값."""
    by_day = {}
    for r in rows:
        title = r.get("pn", "")
        if not any(m in title for m in must) or any(a in title for a in avoid):
            continue
        grams = grams_of(title)
        prices = [number(r.get(f)) for f in ("dp", "sp")]
        price = next((p for p in prices if p and p > 0), None)
        day = (r.get("sd") or "").replace("-", "")[:8]
        if not grams or not price or len(day) != 8:
            continue
        by_day.setdefault(day, []).append(price / grams * 1000)
    if not by_day:
        return None

    def window(end, days=7):
        """end부터 거꾸로 days일 동안의 값 (온라인 값은 하루치가 적어서 한 주를 묶는다)."""
        e = dt.datetime.strptime(end, "%Y%m%d").date()
        keys = {(e - dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(days)}
        return [v for k in keys for v in by_day.get(k, [])]

    latest = max(by_day)
    now = window(latest)
    if len(now) < 3:
        return None
    # 가운데 값이라 묶음 오표기 같은 튀는 값에 덜 흔들린다
    per_kg = round(statistics.median(now) / 10) * 10
    d = dt.datetime.strptime(latest, "%Y%m%d").date()
    out = {"key": name, "unit": "1kg", "price": per_kg, "perKg": per_kg, "date": d.isoformat(), "source": ONLINE_SOURCE}
    before = window((d - dt.timedelta(days=7)).strftime("%Y%m%d"))
    if len(before) >= 3:
        out["weekAgo"] = round(statistics.median(before) / 10) * 10
    return out


def fetch_online(key, today):
    """가공식품 재료들. 실패해도 빈 목록 (KAMIS 값만으로도 파일은 쓴다)."""
    try:
        catalog = online_paged(key, "getPriceItemList", {})
    except RuntimeError as e:
        print(f"온라인 가격 품목 목록: {e}", file=sys.stderr)
        return []
    print("온라인 가격 품목: " + ", ".join(f"{r.get('in')}({r.get('ic')})" for r in catalog), file=sys.stderr)
    start = (today - dt.timedelta(days=ONLINE_DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    out = []
    for name, item_words, must, avoid in ONLINE_ITEMS:
        codes = [r.get("ic") for r in catalog if any(w in (r.get("in") or "") for w in item_words)]
        got = None
        for code in codes[:3]:
            try:
                got = summarize_online(name, online_paged(key, "getPriceInfo", {"itemCode": code, "startDate": start, "endDate": end}), must, avoid)
            except RuntimeError as e:
                print(f"{name} (온라인 {code}): {e}", file=sys.stderr)
            if got:
                break
        if got:
            print(f"{name}: kg {got['perKg']:,}원 (1주 전 {got.get('weekAgo')}) {got['date']} 온라인")
            out.append(got)
        else:
            print(f"{name}: 온라인 값 없음 (품목 {codes or '못 찾음'})", file=sys.stderr)
    return out


def main(path):
    key = service_key()
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).date()
    start = (today - dt.timedelta(days=DAYS)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    try:
        old = {i["key"]: i for i in json.load(open(path, encoding="utf-8")).get("items", [])}
    except (OSError, ValueError):
        old = {}
    items, fresh = [], 0
    for name, sources in ITEMS:
        got = None
        for ctgry, item, vrty in sources:
            try:
                got = summarize(name, fetch_item(key, ctgry, item, vrty, start, end))
            except RuntimeError as e:
                print(f"{name} ({ctgry}/{item}): {e}", file=sys.stderr)
            if got:
                break
        if got:
            fresh += 1
            print(f"{name}: {got['unit']} {got['price']:,}원 (kg {got.get('perKg')}, 1주 전 {got.get('weekAgo')}) {got['date']}")
            items.append(got)
        elif name in old:
            print(f"{name}: 못 받아서 지난 값 유지", file=sys.stderr)
            items.append(old[name])
        else:
            print(f"{name}: 데이터 없음", file=sys.stderr)
    if fresh == 0:
        sys.exit("하나도 못 받았어요. 키·승인 상태를 확인해 주세요. 파일은 그대로 둡니다.")
    online = {i["key"]: i for i in fetch_online(key, today)}
    for name, *_ in ONLINE_ITEMS:
        if name in online:
            items.append(online[name])
        elif name in old:
            print(f"{name}: 못 받아서 지난 값 유지", file=sys.stderr)
            items.append(old[name])
    board = {
        "version": 1,
        "source": SOURCE,
        "surveyDate": max(i["date"] for i in items if i.get("source") is None),
        "items": items,
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(board, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"{fresh}/{len(ITEMS)} 품목 새로 받음 → {path}")


def self_test():
    def row(day, market, price, grade="상품", vrty="00", unit="개", size="10", kg=None):
        return {"exmn_ymd": day, "se_cd": "01", "vrty_cd": vrty, "grd_nm": grade, "unit": unit,
                "unit_sz": size, "mrkt_cd": market, "exmn_dd_prc": price, "exmn_dd_cnvs_prc": kg}
    rows = [row("20261107", "a", "4,000", kg="3,700"), row("20261107", "b", "3,960", kg="3,660"),
            row("20261031", "a", "4,400"), row("20261031", "b", "4,200"),
            row("20261107", "a", "3,000", grade="중품"), row("20261107", "c", "9,999", vrty="01", size="5")]
    s = summarize("귤", rows)
    assert s == {"key": "귤", "unit": "10개", "price": 3980, "perKg": 3680, "weekAgo": 4300, "date": "2026-11-07"}, s
    # 7일 전이 없으면 하루 앞뒤에서 찾는다
    s = summarize("배추", [row("20261107", "a", "3120", unit="포기", size="1"), row("20261101", "a", "3500", unit="포기", size="1")])
    assert s["weekAgo"] == 3500 and s["unit"] == "1포기", s
    # 1주 전이 아예 없으면 weekAgo 없음
    assert "weekAgo" not in summarize("굴", [row("20261107", "a", "18000", unit="kg", size="1")])
    # 응답 모양: response로 싸인 것, item이 하나인 것
    assert rows_of({"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": {"x": 1}}, "totalCount": 1}}}) == ([{"x": 1}], 1)
    assert rows_of({"header": {"resultCode": "00"}, "body": {"items": "", "totalCount": 0}}) == ([], 0)
    try:
        rows_of({"OpenAPI_ServiceResponse": {"cmmMsgHeader": {"errMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR", "returnReasonCode": "30"}}})
        raise AssertionError("게이트웨이 오류를 놓침")
    except RuntimeError as e:
        assert "승인" in str(e), e
    # 온라인 상품 이름에서 무게 읽기
    assert grams_of("국산콩 부침두부 300g") == 300
    assert grams_of("찌개두부 300g x 3개") == 900
    assert grams_of("(380g*2) 두부") == 760
    assert grams_of("떡볶이떡 1kg") == 1000
    assert grams_of("두부 2입 (300g)") == 600
    assert grams_of("두부 한 모") is None
    xml = """<response><header><resultCode>00</resultCode><resultMsg>OK</resultMsg></header><body><items>
<item><ic>A1</ic><in>두부</in><pi>1</pi><pn>부침두부 300g</pn><sp>2000</sp><dp>1800</dp><sd>20261001</sd></item>
<item><ic>A1</ic><in>두부</in><pi>2</pi><pn>찌개두부 300g x 2</pn><sp>3600</sp><dp></dp><sd>20261001</sd></item>
<item><ic>A1</ic><in>두부</in><pi>3</pi><pn>국산 두부 500g</pn><sp>3000</sp><dp>0</dp><sd>20260930</sd></item>
<item><ic>A1</ic><in>두부</in><pi>4</pi><pn>순두부 350g</pn><sp>1200</sp><dp></dp><sd>20261001</sd></item>
<item><ic>A1</ic><in>두부</in><pi>5</pi><pn>두부 한 모</pn><sp>1500</sp><dp></dp><sd>20261001</sd></item>
</items><numOfRows>10</numOfRows><pageNo>1</pageNo><totalCount>5</totalCount></body></response>"""
    rows, total = online_rows(xml)
    assert total == 5 and rows[0]["pn"] == "부침두부 300g", rows
    s = summarize_online("두부", rows, ["두부"], ["순두부"])
    # 6000, 6000, 6000원/kg → 6,000원. 순두부·무게 없는 건 뺀다
    assert s == {"key": "두부", "unit": "1kg", "price": 6000, "perKg": 6000, "date": "2026-10-01", "source": ONLINE_SOURCE}, s
    assert summarize_online("두부", rows[:2], ["두부"], []) is None  # 상품이 셋보다 적으면 안 믿는다
    try:
        online_rows("<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>")
        raise AssertionError("게이트웨이 오류를 놓침")
    except RuntimeError as e:
        assert "30" in str(e), e
    print("ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--test"]:
        self_test()
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else "data/prices.json")
