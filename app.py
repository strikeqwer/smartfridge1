# -*- coding: utf-8 -*-
"""
🧊 냉장고 유통기한 자동 관리 웹앱  (single-file / app.py)

[실행 방법]
  1) 라이브러리 설치
       pip install streamlit google-genai pillow
  2) 실행
       streamlit run app.py
  3) 앱 좌측 사이드바에 Google Gemini API Key 입력 (또는 Secrets/환경변수 GEMINI_API_KEY 설정)

* 이미지 인식은 Google Gemini(무료 티어)를 사용합니다: gemini-2.5-flash (기본).
  Google AI Studio(aistudio.google.com)에서 카드 없이 무료 키를 발급받을 수 있습니다.
"""

import os
import io
import json
import sqlite3
import html as html_lib
from datetime import date, datetime, timedelta

import streamlit as st
from PIL import Image
from google import genai
from google.genai import types

# ──────────────────────────────────────────────────────────────────────────────
# 기본 설정
# ──────────────────────────────────────────────────────────────────────────────
DB_PATH = "fridge.db"

# 이미지 인식을 지원하는 Gemini 무료 티어 모델들 (첫 번째가 기본값)
MODEL_OPTIONS = [
    "gemini-2.5-flash",        # 균형형 기본 추천 (무료)
    "gemini-2.5-flash-lite",   # 더 빠르고 한도 넉넉 (무료)
]

# 식품 카테고리 (선택용)
CATEGORIES = ["유제품", "가공식품", "신선식품", "냉동식품", "채소", "과일", "정육", "수산", "음료", "기타"]

st.set_page_config(
    page_title="스마트 냉장고",
    page_icon="🧊",
    layout="centered",
    initial_sidebar_state="expanded",
)

# ──────────────────────────────────────────────────────────────────────────────
# 스타일 (Light / 모던 모바일 느낌)  — CSS는 f-string 아님(중괄호 그대로)
# ──────────────────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
      .stApp { background: #f5f6f8; }
      .main .block-container { padding-top: 1.2rem; padding-bottom: 3rem; max-width: 720px; }
      h1, h2, h3 { color: #1f2937; letter-spacing: -0.02em; }
      .hero {
        background: linear-gradient(135deg, #4f8cff 0%, #7aa8ff 100%);
        color: #fff; border-radius: 20px; padding: 22px 24px; margin-bottom: 18px;
        box-shadow: 0 10px 24px rgba(79,140,255,0.28);
      }
      .hero h1 { color:#fff; margin:0; font-size: 1.5rem; }
      .hero p { margin: 6px 0 0; opacity: .92; font-size: .92rem; }
      .item-card {
        background:#ffffff; border-radius:16px; padding:14px 16px; margin-bottom:0;
        box-shadow: 0 2px 10px rgba(17,24,39,0.06);
      }
      .row-top { display:flex; justify-content:space-between; align-items:center; }
      .item-name { font-weight:700; font-size:1.02rem; color:#111827; }
      .item-sub  { color:#6b7280; font-size:.82rem; margin-top:4px; }
      .badge {
        color:#fff; font-weight:700; font-size:.82rem; padding:4px 12px;
        border-radius:999px; white-space:nowrap;
      }
      .cat-chip {
        display:inline-block; background:#eef2ff; color:#4f46e5; font-size:.72rem;
        padding:2px 9px; border-radius:999px; margin-right:6px;
      }
      .metric-box {
        background:#fff; border-radius:16px; padding:14px; text-align:center;
        box-shadow:0 2px 10px rgba(17,24,39,0.06);
      }
      .metric-num { font-size:1.6rem; font-weight:800; color:#111827; }
      .metric-lbl { font-size:.78rem; color:#6b7280; }
      div.stButton > button { border-radius:10px; font-weight:600; }
    </style>
    """,
    unsafe_allow_html=True,
)

# ──────────────────────────────────────────────────────────────────────────────
# 데이터베이스 (SQLite)
# ──────────────────────────────────────────────────────────────────────────────
def get_conn():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_name TEXT NOT NULL,
                category TEXT,
                purchase_date TEXT,
                expiration_date TEXT,
                created_at TEXT
            )
            """
        )
        conn.commit()


def add_item(item_name, category, purchase_date, expiration_date):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO items (item_name, category, purchase_date, expiration_date, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (item_name, category, purchase_date, expiration_date,
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()


def get_all_items():
    with get_conn() as conn:
        cur = conn.execute(
            "SELECT id, item_name, category, purchase_date, expiration_date FROM items"
        )
        rows = cur.fetchall()
    items = []
    for r in rows:
        items.append({
            "id": r[0], "item_name": r[1], "category": r[2],
            "purchase_date": r[3], "expiration_date": r[4],
        })
    return items


def update_expiration(item_id, new_exp):
    with get_conn() as conn:
        conn.execute("UPDATE items SET expiration_date=? WHERE id=?", (new_exp, item_id))
        conn.commit()


def delete_item(item_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM items WHERE id=?", (item_id,))
        conn.commit()


# ──────────────────────────────────────────────────────────────────────────────
# 유통기한 상태 계산
# ──────────────────────────────────────────────────────────────────────────────
def days_left(exp_str):
    try:
        exp = date.fromisoformat(str(exp_str))
    except (ValueError, TypeError):
        return None
    return (exp - date.today()).days


def status_of(d):
    """(라벨, 색상) 반환. ≤3일=빨강, ≤7일=주황, 그 외=초록"""
    if d is None:
        return ("정보없음", "#9ca3af")
    if d < 0:
        return (f"만료 D+{abs(d)}", "#ef4444")
    if d == 0:
        return ("D-Day", "#ef4444")
    if d <= 3:
        return (f"D-{d}", "#ef4444")
    if d <= 7:
        return (f"D-{d}", "#f59e0b")
    return (f"D-{d}", "#22c55e")


# ──────────────────────────────────────────────────────────────────────────────
# 이미지 → 바이트 (Gemini 입력용)
# ──────────────────────────────────────────────────────────────────────────────
def prepare_image(uploaded_file):
    """업로드 이미지를 RGB/JPEG로 정규화 후 바이트로 반환. (media_type, image_bytes, 미리보기이미지)"""
    img = Image.open(uploaded_file).convert("RGB")
    max_side = 1600
    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize((int(img.size[0] * ratio), int(img.size[1] * ratio)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return "image/jpeg", buf.getvalue(), img


# ──────────────────────────────────────────────────────────────────────────────
# 프롬프트
# ──────────────────────────────────────────────────────────────────────────────
SYSTEM_PROMPT = (
    "너는 마트 영수증 이미지에서 식품 정보를 추출하는 전문 AI다. "
    "지시를 정확히 지키고, 요청한 JSON 배열 외에는 어떤 텍스트도 출력하지 않는다."
)


def build_user_prompt(purchase_day: str) -> str:
    return f"""아래 영수증 이미지를 분석해 다음 규칙을 지켜 결과를 반환해라.

규칙:
1. 영수증에서 식재료 및 식품 목록만 추출해라. (봉투값, 할인, 포인트, 비식품 등은 제외)
2. 구매일(purchase_date)은 {purchase_day} 로 설정해라.
3. 각 식품의 일반적인 보관 플로우를 고려해 소비기한(expiration_date)을 예측해라.
   예시) 우유는 구매일+10일, 두부는 구매일+14일, 냉동식품은 구매일+90일,
        신선육/생선 구매일+3~4일, 채소/과일 구매일+7일, 계란 구매일+20일,
        라면/통조림 등 상온 가공식품 구매일+180일 등. 상식적인 값으로 판단해라.
4. category 는 '유제품','가공식품','신선식품','냉동식품','채소','과일','정육','수산','음료','기타' 중 하나로 지정해라.
5. 날짜는 반드시 'YYYY-MM-DD' 형식으로 작성해라.

응답은 반드시 아래 JSON 배열 형식으로만 반환하고,
다른 설명이나 마크다운 텍스트(```json 등)는 절대 붙이지 마라.

[
  {{"item_name": "서울우유 1L", "purchase_date": "{purchase_day}", "expiration_date": "...", "category": "유제품"}},
  {{"item_name": "국산콩 두부", "purchase_date": "{purchase_day}", "expiration_date": "...", "category": "가공식품"}}
]"""


def extract_json_array(text: str):
    """모델 응답 텍스트에서 JSON 배열만 안전하게 파싱."""
    t = text.strip()
    # 혹시 붙었을 수 있는 코드펜스 제거
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    # 대괄호 범위만 추출
    start = t.find("[")
    end = t.rfind("]")
    if start != -1 and end != -1 and end > start:
        t = t[start:end + 1]
    return json.loads(t)


# (이전 영수증 단일 인식 함수는 제거됨 — 현재는 제품+유통기한 2장 세트 방식만 사용)


def extract_json_object(text: str):
    """모델 응답에서 JSON 객체 하나만 안전 파싱."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end != -1 and end > start:
        t = t[start:end + 1]
    return json.loads(t)


def pair_user_prompt(purchase_day: str) -> str:
    return f"""위 두 이미지는 '한 개의 식품'에 대한 것이다.
- 첫 번째 이미지: 제품 사진 (브랜드/제품명을 읽어라)
- 두 번째 이미지: 그 제품의 유통기한/소비기한이 인쇄된 라벨 사진

규칙:
1. item_name: 첫 번째 사진에서 제품명을 읽어라. (예: "농심 스윙칩 오리지널", "서울우유 1L")
2. expiration_date: 두 번째 사진의 날짜를 읽어 'YYYY-MM-DD'로 변환해라.
   '2026.07.20', '26.07.20', '20260720', '2026년 7월 20일', 'EXP 20.07.26' 등 어떤 형식이든 정규화하고,
   2자리 연도는 20xx로 해석해라. 날짜를 도저히 못 읽으면 null 로 둬라.
3. category: '유제품','가공식품','신선식품','냉동식품','채소','과일','정육','수산','음료','기타' 중 하나.
4. purchase_date 는 {purchase_day} 로 설정해라.

반드시 아래 JSON 객체 '하나만' 반환하고, 다른 설명이나 마크다운(```json 등)은 절대 붙이지 마라.

{{"item_name": "...", "purchase_date": "{purchase_day}", "expiration_date": "YYYY-MM-DD 또는 null", "category": "..."}}"""


def call_gemini_pair(api_key, model, prod, exp, purchase_day):
    """제품 사진(prod)+유통기한 사진(exp) 한 쌍을 Gemini로 보내 dict 하나 반환. prod/exp = (mime_type, image_bytes)"""
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents=[
            "다음은 한 개의 식품에 대한 두 장의 사진입니다.",
            "[제품 사진]",
            types.Part.from_bytes(data=prod[1], mime_type=prod[0]),
            "[유통기한 라벨 사진]",
            types.Part.from_bytes(data=exp[1], mime_type=exp[0]),
            pair_user_prompt(purchase_day),
        ],
        config=types.GenerateContentConfig(
            system_instruction=("너는 식품 제품 사진과 유통기한 라벨 사진에서 정보를 뽑는 전문 AI다. "
                                "요청한 JSON 객체 하나만 출력한다."),
            response_mime_type="application/json",
        ),
    )
    return extract_json_object(resp.text or "")


def receipt_user_prompt() -> str:
    return """이 이미지는 마트/편의점 영수증이다.

규칙:
1. 영수증에서 '식재료 및 식품' 품목만 골라서 추출해라.
2. 다음은 반드시 제외해라: 봉투값/쇼핑백, 할인·에누리, 적립/포인트, 결제수단·카드정보, 합계·부가세,
   그리고 식품이 아닌 물건(세제, 휴지, 주방용품, 위생용품, 문구 등).
3. 각 품목의 category 를 다음 중 하나로 지정해라:
   '유제품','가공식품','신선식품','냉동식품','채소','과일','정육','수산','음료','기타'.
4. 유통기한은 넣지 마라. (유통기한은 사용자가 직접 입력한다)
5. 품목명이 영수증에 축약돼 있으면 알아보기 쉬운 이름으로 살짝 정리해도 된다.

반드시 아래 JSON 배열만 반환하고, 다른 설명이나 마크다운(```json 등)은 절대 붙이지 마라.
식재료가 하나도 없으면 빈 배열 [] 을 반환해라.

[{"item_name": "서울우유 1L", "category": "유제품"}, {"item_name": "국산콩 두부", "category": "가공식품"}]"""


def call_gemini_receipt(api_key, model, media_type, image_bytes):
    """영수증 이미지 하나에서 식재료 목록(JSON 배열)을 추출. 각 원소 = {item_name, category}"""
    client = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=media_type),
            receipt_user_prompt(),
        ],
        config=types.GenerateContentConfig(
            system_instruction=("너는 영수증에서 식재료·식품만 골라내는 전문 AI다. "
                                "요청한 JSON 배열만 출력한다."),
            response_mime_type="application/json",
        ),
    )
    return extract_json_array(resp.text or "")


# ──────────────────────────────────────────────────────────────────────────────
# 접속 비밀번호 잠금 (Secrets의 APP_PASSWORD 사용, 미설정 시 잠금 해제)
# ──────────────────────────────────────────────────────────────────────────────
def check_password():
    try:
        expected = st.secrets.get("APP_PASSWORD", "")
    except Exception:
        expected = ""
    if not expected:            # 비밀번호를 설정하지 않았으면 그냥 통과
        return
    if st.session_state.get("auth_ok"):
        return
    st.markdown(
        '<div class="hero"><h1>🔒 스마트 냉장고</h1>'
        '<p>접속 비밀번호를 입력해주세요.</p></div>',
        unsafe_allow_html=True,
    )
    pw = st.text_input("접속 비밀번호", type="password")
    if pw:
        if pw == expected:
            st.session_state["auth_ok"] = True
            st.rerun()
        else:
            st.error("비밀번호가 올바르지 않습니다.")
    st.stop()


check_password()

# ──────────────────────────────────────────────────────────────────────────────
# 사이드바
# ──────────────────────────────────────────────────────────────────────────────
init_db()

def _load_default_key():
    """Streamlit Secrets → 환경변수 순으로 Gemini API 키 자동 로드."""
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        try:
            k = st.secrets.get(name, "")
            if k:
                return k
        except Exception:
            pass
        if os.environ.get(name):
            return os.environ.get(name)
    return ""


with st.sidebar:
    st.markdown("### ⚙️ 설정")
    _default_key = _load_default_key()
    if _default_key:
        api_key = _default_key
        st.success("API Key 설정됨 ✅ (무료)")
    else:
        api_key = st.text_input(
            "Google Gemini API Key",
            type="password",
            help="배포 시 Secrets에 GEMINI_API_KEY 를 넣으면 이 칸이 사라집니다. (무료·카드 불필요)",
        )
    model = st.selectbox("모델 선택", MODEL_OPTIONS, index=0)
    st.caption("Gemini 무료 티어로 사진을 인식합니다.")
    st.divider()
    st.caption("🔴 3일 이내 · 🟡 7일 이내 · 🟢 여유")

# ──────────────────────────────────────────────────────────────────────────────
# 헤더
# ──────────────────────────────────────────────────────────────────────────────
st.markdown(
    """
    <div class="hero">
      <h1>🧊 스마트 냉장고</h1>
      <p>영수증만 찍으면 유통기한을 자동으로 관리해 드려요.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

tab_inv, tab_scan = st.tabs(["📊 내 냉장고", "🧾 영수증 스캔"])

# ──────────────────────────────────────────────────────────────────────────────
# 탭 1: 인벤토리
# ──────────────────────────────────────────────────────────────────────────────
with tab_inv:
    items = get_all_items()

    # 유통기한 임박순 정렬 (정보없음은 맨 뒤)
    def sort_key(it):
        d = days_left(it["expiration_date"])
        return (d is None, d if d is not None else 999999)

    items.sort(key=sort_key)

    total = len(items)
    soon = sum(1 for it in items if (days_left(it["expiration_date"]) is not None
                                     and days_left(it["expiration_date"]) <= 3))
    week = sum(1 for it in items if (days_left(it["expiration_date"]) is not None
                                     and 3 < days_left(it["expiration_date"]) <= 7))

    c1, c2, c3 = st.columns(3)
    for col, num, lbl in (
        (c1, total, "전체 품목"),
        (c2, soon, "🔴 임박(3일)"),
        (c3, week, "🟡 주의(7일)"),
    ):
        col.markdown(
            f'<div class="metric-box"><div class="metric-num">{num}</div>'
            f'<div class="metric-lbl">{lbl}</div></div>',
            unsafe_allow_html=True,
        )

    st.write("")

    # ➕ 직접 추가 (영수증에 없어도 수동 등록)
    with st.expander("➕ 직접 추가하기"):
        ac = st.columns([2, 1])
        add_name = ac[0].text_input("품목명", key="add_name", placeholder="예: 계란 한판")
        add_cat = ac[1].selectbox("카테고리", CATEGORIES, key="add_cat")
        add_exp = st.date_input("유통기한", value=date.today() + timedelta(days=7),
                                key="add_exp", format="YYYY-MM-DD")
        if st.button("추가", type="primary", key="add_btn"):
            if add_name.strip():
                add_item(add_name.strip(), add_cat, date.today().isoformat(), add_exp.isoformat())
                st.success(f"'{add_name.strip()}'을(를) 추가했어요!")
                st.rerun()
            else:
                st.warning("품목명을 입력해 주세요.")

    if not items:
        st.info("아직 등록된 식재료가 없어요. '영수증 스캔' 탭에서 영수증을 올려보세요! 🧾")
    else:
        for it in items:
            d = days_left(it["expiration_date"])
            label, color = status_of(d)
            name = html_lib.escape(str(it["item_name"]))
            cat = html_lib.escape(str(it["category"] or "기타"))
            pdate = html_lib.escape(str(it["purchase_date"] or "-"))
            edate = html_lib.escape(str(it["expiration_date"] or "-"))

            with st.container(border=True):
                st.markdown(
                    f"""
                    <div class="row-top">
                      <div>
                        <span class="item-name">{name}</span><br>
                        <span class="item-sub"><span class="cat-chip">{cat}</span>
                        구매 {pdate} · 소비기한 {edate}</span>
                      </div>
                      <span class="badge" style="background:{color};">{label}</span>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                with st.expander("✏️ 수정 / 삭제"):
                    try:
                        cur_val = date.fromisoformat(str(it["expiration_date"]))
                    except (ValueError, TypeError):
                        cur_val = date.today()

                    ec1, ec2 = st.columns([2, 1])
                    new_date = ec1.date_input(
                        "소비기한(달력에서 수정)",
                        value=cur_val,
                        key=f"date_{it['id']}",
                        format="YYYY-MM-DD",
                    )
                    ec2.write("")
                    ec2.write("")
                    if ec2.button("💾 저장", key=f"save_{it['id']}", use_container_width=True):
                        update_expiration(it["id"], new_date.isoformat())
                        st.success("저장했어요!")
                        st.rerun()

                    if st.button("🗑️ 삭제", key=f"del_{it['id']}", use_container_width=True):
                        delete_item(it["id"])
                        st.rerun()

# ──────────────────────────────────────────────────────────────────────────────
# 탭 2: 영수증 스캔
# ──────────────────────────────────────────────────────────────────────────────
with tab_scan:
    st.markdown("#### 🧾 영수증 스캔")
    st.caption("영수증 사진을 올리면 식재료·식품만 자동으로 골라내 목록을 만들어요. "
               "유통기한은 저장 전에 달력에서 직접 선택하면 됩니다.")

    uploaded = st.file_uploader(
        "마트/편의점 영수증 사진을 올려주세요",
        type=["jpg", "jpeg", "png", "webp", "gif"],
    )

    purchase_day = st.date_input("구매일", value=date.today(), format="YYYY-MM-DD")

    st.caption("💡 키/가입 없이 화면만 체험하려면 아래 '데모로 체험하기'를 누르세요.")

    # 데모(샘플) — API 키·사진 없이 무료로 앱 전체 기능 체험 (식재료 목록만)
    if st.button("🧪 데모로 체험하기 (무료·키 불필요)", use_container_width=True):
        st.session_state["scanned"] = [
            {"item_name": "서울우유 1L", "category": "유제품"},
            {"item_name": "국산콩 두부", "category": "가공식품"},
            {"item_name": "대파 한단", "category": "채소"},
            {"item_name": "삼겹살 500g", "category": "정육"},
            {"item_name": "냉동만두", "category": "냉동식품"},
        ]
        st.success("데모 식재료 목록을 불러왔어요! 아래에서 유통기한을 정하고 저장해보세요.")

    if uploaded is not None:
        media_type, img_bytes, preview = prepare_image(uploaded)
        st.image(preview, caption="업로드한 영수증", use_container_width=True)

        if st.button("🔍 영수증에서 식재료 자동 추출", type="primary", use_container_width=True):
            if not api_key:
                st.info("실제 영수증 인식은 Gemini API 키가 필요해요(무료·카드 불필요). "
                        "화면만 볼 거면 위 '데모로 체험하기'를 이용하세요.")
            else:
                with st.spinner("영수증에서 식재료를 골라내는 중..."):
                    try:
                        result = call_gemini_receipt(api_key, model, media_type, img_bytes)
                        if not isinstance(result, list):
                            raise ValueError("응답이 목록 형식이 아닙니다.")
                        if result:
                            st.session_state["scanned"] = result
                            st.success(f"식재료 {len(result)}개를 골라냈어요! 아래에서 확인·수정하고 유통기한을 정하세요.")
                        else:
                            st.warning("영수증에서 식재료를 찾지 못했어요. 사진이 선명한지 확인하거나, 아래에서 직접 추가하세요.")
                    except Exception as e:
                        st.error(f"인식에 실패했어요. 사진이 선명한지 확인 후 다시 시도해 주세요. ({e})")

    # 추출 결과 확인·수정·저장 (유통기한은 달력에서 직접 선택)
    if st.session_state.get("scanned"):
        st.divider()
        st.markdown("#### 📝 목록 확인 · 유통기한 선택")
        st.caption("이름/카테고리를 고치거나, 필요 없는 건 '포함'을 꺼서 빼세요. 유통기한은 달력에서 선택합니다.")
        scanned = st.session_state["scanned"]

        edited = []
        for i, obj in enumerate(scanned):
            name = str(obj.get("item_name", ""))
            cat = str(obj.get("category", "기타"))
            cat_idx = CATEGORIES.index(cat) if cat in CATEGORIES else CATEGORIES.index("기타")
            try:
                exp_default = date.fromisoformat(str(obj.get("expiration_date")))
            except (ValueError, TypeError):
                exp_default = date.today() + timedelta(days=7)

            with st.container(border=True):
                r1 = st.columns([3, 1])
                new_name = r1[0].text_input("품목명", value=name, key=f"nm_{i}")
                include = r1[1].checkbox("포함", value=True, key=f"inc_{i}")
                r2 = st.columns([1, 1])
                new_cat = r2[0].selectbox("카테고리", CATEGORIES, index=cat_idx, key=f"cat_{i}")
                new_exp = r2[1].date_input("유통기한(달력에서 선택)", value=exp_default,
                                           key=f"exp_{i}", format="YYYY-MM-DD")
            edited.append({
                "include": include, "item_name": new_name, "category": new_cat,
                "purchase_date": purchase_day.isoformat(), "expiration_date": new_exp.isoformat(),
            })

        cbtn = st.columns([1, 1])
        if cbtn[0].button("🧊 냉장고에 저장", type="primary", use_container_width=True):
            saved = 0
            for e in edited:
                if e["include"] and e["item_name"].strip():
                    add_item(e["item_name"].strip(), e["category"], e["purchase_date"], e["expiration_date"])
                    saved += 1
            st.session_state.pop("scanned", None)
            st.success(f"{saved}개 품목을 저장했어요! '내 냉장고' 탭에서 확인하세요. 🎉")
            st.rerun()

        if cbtn[1].button("↩️ 목록 지우기", use_container_width=True):
            st.session_state.pop("scanned", None)
            st.rerun()
