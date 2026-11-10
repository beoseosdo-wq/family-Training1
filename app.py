"""우리 가족 운동 관리 앱
실행: pip install -r requirements.txt && streamlit run app.py
배포: GitHub 저장소에 두 파일 업로드 → Streamlit Community Cloud에서 app.py 지정.
선택 설정(Cloud의 Settings → Secrets):
APP_PASSWORD = "가족만 아는 비밀번호"
APP_URL = "https://배포한주소.streamlit.app"
DB_PATH = "family_fitness.db"
주의: Cloud의 로컬 SQLite는 재배포/인스턴스 교체 시 사라질 수 있습니다.
장기 운영에는 외부 영구 DB로 전환하고, 그전에는 CSV 백업을 보관하세요.
"""
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
import hmac
import json
import zipfile
from datetime import timedelta
from PIL import Image, ImageOps
import sqlite3
from urllib.parse import urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import qrcode
import streamlit as st

SPORTS = ["걷기", "달리기", "줄넘기", "헬스", "요가", "자전거", "수영", "등산"]
COLUMNS = ["기록ID", "가족", "날짜", "운동 종목", "운동 시간(분)"]


def connect_db(path):
    conn = sqlite3.connect(str(path), timeout=20)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect_db(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS members (name TEXT PRIMARY KEY)")
        conn.execute("""CREATE TABLE IF NOT EXISTS workouts (
            id TEXT PRIMARY KEY, member TEXT NOT NULL REFERENCES members(name),
            day TEXT NOT NULL, sport TEXT NOT NULL,
            minutes INTEGER NOT NULL CHECK(minutes BETWEEN 1 AND 1440))""")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(workouts)")}
        for name, definition in [("category", "TEXT NOT NULL DEFAULT '미분류'"),
                                 ("photo", "BLOB"), ("effort", "INTEGER NOT NULL DEFAULT 3")]:
            if name not in cols:
                conn.execute(f"ALTER TABLE workouts ADD COLUMN {name} {definition}")
        # 최초 생성 때만 기본 구성원 등록. 이후 사용자가 수정한 목록을 유지합니다.
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        if not conn.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone():
            conn.executemany("INSERT OR IGNORE INTO members VALUES (?)", [(n,) for n in ["아빠", "엄마", "나"]])
            conn.execute("INSERT INTO settings VALUES ('initialized', '1')")


def load_data(path):
    with connect_db(path) as conn:
        members = [r[0] for r in conn.execute("SELECT name FROM members ORDER BY rowid")]
        rows = conn.execute("SELECT id, member, day, sport, minutes FROM workouts ORDER BY day DESC, rowid DESC").fetchall()
    return members, pd.DataFrame(rows, columns=COLUMNS)


def save_record(path, rid, member, day, sport, minutes, update=False):
    if not sport.strip() or len(sport.strip()) > 40:
        raise ValueError("운동 종목은 1~40자로 입력해주세요.")
    if not 1 <= int(minutes) <= 1440:
        raise ValueError("운동 시간은 1~1,440분으로 입력해주세요.")
    date.fromisoformat(str(day))
    with connect_db(path) as conn:
        if update:
            cursor = conn.execute("UPDATE workouts SET member=?, day=?, sport=?, minutes=? WHERE id=?",
                                  (member, str(day), sport.strip(), int(minutes), rid))
            if cursor.rowcount != 1:
                raise ValueError("다른 접속에서 삭제된 기록입니다. 새로고침해주세요.")
        else:
            conn.execute("INSERT INTO workouts VALUES (?, ?, ?, ?, ?)",
                         (rid, member, str(day), sport.strip(), int(minutes)))


def summarize(members, records):
    base = pd.DataFrame({"가족": members})
    if records.empty:
        base["총 운동 횟수"] = 0
        base["총 운동 시간(분)"] = 0
    else:
        grouped = records.groupby("가족").agg(**{
            "총 운동 횟수": ("기록ID", "count"), "총 운동 시간(분)": ("운동 시간(분)", "sum")}).reset_index()
        base = base.merge(grouped, on="가족", how="left").fillna(0)
        base[["총 운동 횟수", "총 운동 시간(분)"]] = base[["총 운동 횟수", "총 운동 시간(분)"]].astype(int)
    return base


def flash(message):
    st.session_state["notice"] = message
    st.rerun()


def setting(key, default=""):
    # Secrets 파일 없이 로컬에서 실행하는 경우도 지원합니다.
    try:
        return st.secrets[key]
    except (FileNotFoundError, KeyError):
        return default


# 각 종목의 대표 목적에 따른 단순 분류이며 실제 운동 효과의 전부를 뜻하지 않습니다.
CATS = ["유산소", "근력", "유연성·균형", "미분류"]
GUIDES = {
 "걷기": ("유산소", "편한 속도로 3분 → 대화 가능한 속도로 10분 → 천천히 2분", "시선은 앞을 보고 어깨에 힘을 빼세요. 보폭을 억지로 늘리지 마세요."),
 "달리기": ("유산소", "걷기 5분 → 가벼운 조깅 1분/걷기 2분을 3회 → 걷기 3분", "처음에는 속도보다 편한 호흡에 집중하세요. 무릎·발목 통증이 있으면 중단하세요."),
 "줄넘기": ("유산소", "가볍게 걷기 3분 → 줄넘기 20초/휴식 40초를 5회 → 걷기 2분", "미끄럽지 않은 바닥과 운동화를 사용하고 낮게 뛰세요. 관절 불편 시 걷기로 대체하세요."),
 "헬스": ("근력", "준비운동 5분 → 벽 푸시업 5~8회와 의자 앉았다 일어나기 5~8회를 1~2세트 → 정리 3분", "안정된 의자를 벽에 고정하세요. 반동을 피하고 힘쓸 때 숨을 내쉬세요. 무거운 기구는 지도자와 시작하세요."),
 "요가": ("유연성·균형", "편안한 호흡 2분 → 어깨·발목 가볍게 풀기 3분 → 의자를 잡고 뒤꿈치 들기 5회 → 편안한 호흡 2분", "관절을 끝까지 밀지 말고 통증 없는 범위에서 움직이세요. 균형 동작은 지지대를 잡으세요."),
 "자전거": ("유산소", "평탄한 곳에서 천천히 5분 → 편한 강도로 10분 → 천천히 3분", "헬멧을 착용하고 안장 높이와 브레이크를 확인하세요. 실내 자전거로 대체해도 좋아요."),
 "수영": ("유산소", "준비운동 후 안전요원·지도자가 있는 곳에서 익숙한 동작을 짧게 반복하고 충분히 쉬기", "수영이 처음이면 혼자 시작하지 마세요. 수심과 시설 안전수칙을 확인하세요."),
 "등산": ("유산소", "짧고 완만한 코스를 선택 → 평소보다 천천히 오르기 → 충분한 휴식 후 하산", "날씨·일몰·코스를 확인하고 물과 미끄럼 방지 신발을 준비하세요.")}


def all_records(db):
    with connect_db(db) as conn:
        return pd.read_sql_query("SELECT id, member, day, sport, minutes, category, effort, photo IS NOT NULL AS has_photo FROM workouts ORDER BY day DESC, rowid DESC", conn)


def normalized_photo(upload):
    if upload is None:
        return None
    if upload.size > 5 * 1024 * 1024:
        raise ValueError("사진은 5MB 이하로 첨부해주세요.")
    try:
        with Image.open(upload) as im:
            if im.width * im.height > 20_000_000:
                raise ValueError("사진 해상도가 너무 큽니다. 크기를 줄여주세요.")
            im = ImageOps.exif_transpose(im).convert("RGB")
            im.thumbnail((1280, 1280))
            clean = Image.new("RGB", im.size)
            clean.paste(im)
            out = BytesIO()
            clean.save(out, format="JPEG", quality=82)
            return out.getvalue()
    except (Image.DecompressionBombError, OSError) as exc:
        raise ValueError("읽을 수 없는 사진입니다. JPG 또는 PNG를 사용해주세요.") from exc


def write_workout(db, rid, member, day, sport, minutes, category, effort, photo, update=False):
    if not sport.strip() or len(sport.strip()) > 40 or not 1 <= int(minutes) <= 1440:
        raise ValueError("종목은 1~40자, 시간은 1~1,440분으로 입력해주세요.")
    if category not in CATS or not 1 <= effort <= 5:
        raise ValueError("유형 또는 체감 강도를 확인해주세요.")
    if day > datetime.now(ZoneInfo("Asia/Seoul")).date():
        raise ValueError("미래 운동은 기록할 수 없어요.")
    with connect_db(db) as conn:
        if update:
            cur = conn.execute("UPDATE workouts SET day=?, sport=?, minutes=?, category=?, effort=?, photo=COALESCE(?,photo) WHERE id=? AND member=?",
                               (str(day), sport.strip(), minutes, category, effort, photo, rid, member))
            if cur.rowcount != 1:
                raise ValueError("내 기록이 아니거나 이미 삭제된 기록입니다.")
        else:
            if photo is None:
                raise ValueError("새 기록에는 운동 인증사진이 필요해요.")
            conn.execute("INSERT INTO workouts(id,member,day,sport,minutes,category,effort,photo) VALUES(?,?,?,?,?,?,?,?)",
                         (rid, member, str(day), sport.strip(), minutes, category, effort, photo))


def balance(data):
    known = data[data.category.isin(CATS[:3])]
    totals = known.groupby("category").minutes.sum().reindex(CATS[:3], fill_value=0)
    if len(data) < 3 or not totals.sum():
        return totals, "기록이 적어 아직 편중 여부를 판단하지 않아요. 서로 다른 활동을 3회 이상 기록해보세요."
    top = totals.idxmax()
    share = float(totals.max() / totals.sum())
    missing = totals[totals == 0].index.tolist()
    message = f"분류된 운동 시간 중 {top} 비중이 {share:.0%}예요. "
    if share >= .7:
        message += "앱의 참고 기준(70%)을 넘었어요. 현재 목표와 몸 상태에 맞는지 돌아보세요. "
    else:
        message += "앱의 편중 알림 기준(70%) 이내예요. "
    if missing:
        message += "기록이 없는 유형: " + ", ".join(missing) + ". 가능하다면 짧게 경험해보세요."
    return totals, message


def backup(db):
    with connect_db(db) as conn:
        members = [r[0] for r in conn.execute("SELECT name FROM members")]
        rows = conn.execute("SELECT id,member,day,sport,minutes,category,effort,photo FROM workouts").fetchall()
    manifest = {"version": 2, "members": members, "records": []}
    out = BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for i, row in enumerate(rows):
            r = dict(zip(["id","member","day","sport","minutes","category","effort"], row[:7]))
            if row[7]:
                r["photo_file"] = f"photos/{i}.jpg"
                z.writestr(r["photo_file"], row[7])
            manifest["records"].append(r)
        z.writestr("records.json", json.dumps(manifest, ensure_ascii=False))
    return out.getvalue()


def go(page):
    st.session_state.page = page
    st.rerun()


def main():
    st.set_page_config(page_title="가족 운동", page_icon="🏃", layout="centered", initial_sidebar_state="collapsed")
    st.markdown("""<style>
    .block-container{max-width:540px;padding-top:1.4rem;padding-bottom:2rem;}
    h1{font-size:1.8rem!important;} h2{font-size:1.4rem!important;}
    .stButton>button,.stFormSubmitButton>button,.stDownloadButton>button{min-height:52px;border-radius:16px;width:100%;font-weight:650;}
    div[data-testid="stMetric"]{padding:12px;border:1px solid #e2e6f0;border-radius:16px;}
    div[data-testid="stMetricValue"]{font-size:1.6rem;}
    </style>""", unsafe_allow_html=True)
    st.title("🏃 가족 운동")
    password = str(setting("APP_PASSWORD"))
    if password and not st.session_state.get("authenticated"):
        with st.form("login"):
            entry = st.text_input("가족 비밀번호", type="password")
            if st.form_submit_button("시작하기", type="primary"):
                if hmac.compare_digest(entry.encode(), password.encode()):
                    st.session_state.authenticated = True
                    st.rerun()
                st.error("비밀번호를 확인해주세요.")
        st.stop()
    db = str(setting("DB_PATH", "family_fitness.db"))
    init_db(db)
    members, _ = load_data(db)
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()
    if st.session_state.get("member") not in members:
        st.subheader("누가 운동하나요?")
        for i in range(0,len(members),2):
            cols=st.columns(2)
            for j,name in enumerate(members[i:i+2]):
                if cols[j].button("👤 " + name, key=f"person_{name}",use_container_width=True):
                    st.session_state.member=name
                    go("홈")
        with st.expander("＋ 가족 추가"):
            with st.form("add_member",clear_on_submit=True):
                name=st.text_input("이름",max_chars=30)
                if st.form_submit_button("추가"):
                    name=name.strip()
                    if not name or name in members:
                        st.error("다른 이름을 입력해주세요.")
                    else:
                        with connect_db(db) as conn:
                            conn.execute("INSERT OR IGNORE INTO members VALUES(?)",(name,))
                        st.rerun()
        settings_panel(db,password)
        st.stop()
    me=st.session_state.member
    page=st.session_state.get("page","홈")
    records=all_records(db)
    mine=records[records.member==me].copy()
    if page not in ("운동선택","시간입력","사진첨부","완료","수정"):
        nav=st.columns(4)
        for col,label in zip(nav,["홈","달력","코치","설정"]):
            if col.button(label,key="nav_"+label,type="primary" if page==label else "secondary"):
                go(label)
    if page=="홈":
        st.subheader(f"{me}님, 오늘도 가볍게 💪")
        today_records=mine[mine.day==str(today)]
        a,b=st.columns(2)
        a.metric("오늘 운동",f"{int(today_records.minutes.sum())}분")
        b.metric("누적 운동",f"{len(mine)}회")
        st.write("")
        if st.button("＋ 운동 기록하기",type="primary"):
            st.session_state.draft={}
            st.session_state.draft_token=str(uuid4())
            go("운동선택")
        if st.button("📅 지난 운동 보기"):
            go("달력")
    elif page=="운동선택":
        st.caption("1 / 3")
        st.subheader("어떤 운동을 했나요?")
        for i in range(0,len(SPORTS),2):
            cols=st.columns(2)
            icons=["🚶","🏃","🪢","🏋️","🧘","🚲","🏊","🥾"]
            for j,sport in enumerate(SPORTS[i:i+2]):
                if cols[j].button(icons[i+j]+" "+sport,key="sport_"+sport):
                    st.session_state.draft={"sport":sport,"category":GUIDES[sport][0]}
                    go("시간입력")
        with st.expander("＋ 다른 운동"):
            with st.form("custom"):
                sport=st.text_input("운동 이름",max_chars=40)
                cat=st.selectbox("운동 유형",CATS)
                if st.form_submit_button("다음"):
                    if sport.strip():
                        st.session_state.draft={"sport":sport.strip(),"category":cat}
                        go("시간입력")
                    st.error("운동 이름을 입력해주세요.")
        if st.button("취소"):
            go("홈")
    elif page=="시간입력":
        d=st.session_state.get("draft",{})
        if not d:
            go("운동선택")
        st.caption("2 / 3 · "+d["sport"])
        st.subheader("얼마나 움직였나요?")
        with st.form("duration"):
            minutes=st.number_input("운동 시간 · 분",1,1440,int(d.get("minutes",30)),step=5)
            day=st.date_input("날짜",value=d.get("day",today),max_value=today)
            effort=st.select_slider("체감 강도",options=[1,2,3,4,5],value=d.get("effort",3),format_func=lambda n:{1:"아주 편함",2:"편함",3:"보통",4:"힘듦",5:"매우 힘듦"}[n])
            if st.form_submit_button("다음 →",type="primary"):
                d.update(minutes=minutes,day=day,effort=effort)
                st.session_state.draft=d
                go("사진첨부")
        if st.button("← 운동 다시 선택"):
            go("운동선택")
    elif page=="사진첨부":
        d=st.session_state.get("draft",{})
        if "minutes" not in d:
            go("시간입력")
        st.caption("3 / 3")
        st.subheader("사진 한 장이면 끝 📸")
        st.write(f"{d['sport']} · {d['minutes']}분 · {d['day']:%m/%d}")
        source=st.radio("사진 가져오기",["사진 선택","직접 촬영"],horizontal=True,label_visibility="collapsed")
        token=st.session_state.get("draft_token","new")
        if source=="직접 촬영":
            photo=st.camera_input("운동 사진",key="camera_"+token)
        else:
            photo=st.file_uploader("사진 선택 · 5MB 이하",type=["jpg","jpeg","png"],key="upload_"+token)
        st.caption("얼굴·위치 노출 없이. 사진은 실제 운동을 자동 검증하지 않아요.")
        if st.button("기록 저장",type="primary",disabled=photo is None):
            try:
                write_workout(db,str(uuid4()),me,d["day"],d["sport"],d["minutes"],d["category"],d["effort"],normalized_photo(photo))
                st.session_state.saved_day=d["day"]
                st.session_state.saved_summary=f"{d['sport']} · {d['minutes']}분"
                st.session_state.pop("draft",None)
                go("완료")
            except (ValueError,sqlite3.Error) as exc:
                st.error(str(exc))
        if st.button("← 시간 수정"):
            go("시간입력")
    elif page=="완료":
        st.subheader("✅ 오늘의 움직임, 저장 완료!")
        st.write(st.session_state.get("saved_summary",""))
        if st.button("홈으로",type="primary"):
            go("홈")
        if st.button("방금 기록 보기"):
            st.session_state.calendar_day=st.session_state.get("saved_day",today)
            go("달력")
    elif page=="달력":
        st.subheader("📅 운동 달력")
        selected=st.date_input("보고 싶은 날짜",key="calendar_day",value=today,max_value=today)
        daily=mine[mine.day==str(selected)]
        st.caption(f"{selected:%Y.%m.%d} · {len(daily)}회 · {int(daily.minutes.sum())}분")
        if daily.empty:
            st.info("이날은 기록이 없어요.")
        else:
            byid=daily.set_index("id")
            rid=st.selectbox("운동 선택",daily.id.tolist(),format_func=lambda k:f"{byid.loc[k,'sport']} · {byid.loc[k,'minutes']}분 · {k[:4]}")
            r=byid.loc[rid]
            st.subheader(f"{r.sport} · {r.minutes}분")
            with connect_db(db) as conn:
                image=conn.execute("SELECT photo FROM workouts WHERE id=? AND member=?",(rid,me)).fetchone()
            if image and image[0]:
                st.image(image[0],width=240)
            else:
                st.caption("인증사진 없음 · 이전 기록")
            if st.button("수정 · 삭제"):
                st.session_state.edit_id=rid
                go("수정")
        # One day and one record at a time; no unbounded history list.
    elif page=="수정":
        rid=st.session_state.get("edit_id")
        matches=mine[mine.id==rid]
        if matches.empty:
            go("달력")
        r=matches.iloc[0]
        st.subheader("기록 수정")
        with st.form("edit_compact"):
            sport=st.text_input("운동",r.sport,max_chars=40)
            mins=st.number_input("시간 · 분",1,1440,int(r.minutes))
            day=st.date_input("날짜",date.fromisoformat(r.day),max_value=today)
            cat=st.selectbox("유형",CATS,index=CATS.index(r.category) if r.category in CATS else 3)
            effort=st.slider("체감 강도",1,5,int(r.effort))
            photo=st.file_uploader("사진 교체 · 선택",type=["jpg","jpeg","png"])
            if st.form_submit_button("변경 저장",type="primary"):
                try:
                    write_workout(db,rid,me,day,sport,mins,cat,effort,normalized_photo(photo),True)
                    st.session_state.calendar_day=day
                    go("달력")
                except (ValueError,sqlite3.Error) as exc:
                    st.error(str(exc))
        with st.expander("기록 삭제"):
            confirm=st.checkbox("사진과 기록을 삭제합니다")
            if st.button("영구 삭제",disabled=not confirm):
                with connect_db(db) as conn:
                    conn.execute("DELETE FROM workouts WHERE id=? AND member=?",(rid,me))
                go("달력")
        if st.button("취소"):
            go("달력")
    elif page=="코치":
        st.subheader("🌱 나의 운동 코치")
        mode=st.radio("코치 메뉴",["기록 피드백","운동 배우기","가족 현황"],horizontal=True,label_visibility="collapsed")
        if mode=="기록 피드백":
            recent=mine[(mine.day>=str(today-timedelta(days=29))) & (mine.day<=str(today))]
            count=len(recent)
            st.metric("최근 30일",f"{count}회")
            if count<3:
                st.progress(min(count/3,1))
                st.write(f"{3-count}회 더 기록하면 피드백을 볼 수 있어요.")
            elif st.button("내 기록 분석하기",type="primary"):
                totals,message=balance(recent)
                st.write(message)
                st.dataframe(totals.rename("분").rename_axis("유형"),use_container_width=True)
                missing=int(recent.loc[recent.category=="미분류","minutes"].sum())
                if missing:
                    st.caption(f"미분류 {missing}분은 비율에서 제외했어요.")
                if totals.sum():
                    least=totals.idxmin()
                    names=[name for name,g in GUIDES.items() if g[0]==least]
                    st.write("다음엔 " + " · ".join(names[:2])+" 어때요?")
                st.caption("70%는 앱 참고 기준. 근육별 불균형이나 건강 상태를 판정하지 않아요.")
        elif mode=="운동 배우기":
            sport=st.selectbox("배워볼 운동",SPORTS)
            cat,routine,tip=GUIDES[sport]
            st.subheader(sport)
            st.write(routine)
            with st.expander("자세 · 안전"):
                st.write(tip)
                st.caption("입문 예시이며 개인 맞춤 처방이 아닙니다. 통증·어지럼·흉통 시 중단하고, 운동 제한이 있다면 전문가와 상의하세요.")
        else:
            stats=records.groupby("member").agg(횟수=("id","count"),시간_분=("minutes","sum")).reindex(members,fill_value=0)
            stats.index.name="가족"
            st.dataframe(stats,use_container_width=True)
    elif page=="설정":
        st.subheader("⚙️ 설정")
        if st.button("👤 가족 바꾸기"):
            for key in list(st.session_state.keys()):
                if key!="authenticated":
                    del st.session_state[key]
            st.rerun()
        if st.button("🔒 로그아웃"):
            st.session_state.clear()
            st.rerun()
        settings_panel(db,password)


def settings_panel(db,password):
    with st.expander("보안 · 저장 안내"):
        if not password:
            st.warning("공용 비밀번호가 없습니다. 배포 Secrets에 APP_PASSWORD를 설정하세요.")
        else:
            st.caption("가족 공용 비밀번호 사용 중")
        st.write("이름 선택은 본인 인증이 아닙니다. 주소는 가족끼리만 공유하세요.")
        st.write("사진·기록은 서버에 저장됩니다. Cloud 재배포·서버 교체 시 유실될 수 있으니 백업하세요.")
        st.caption("사진은 자기 인증 자료이며 실제 운동 여부를 검증하지 않습니다. 장기 운영에는 외부 DB·사진 저장소가 필요합니다.")
    with st.expander("백업"):
        st.caption("모든 가족의 사진이 포함돼요. 동의 후 안전하게 보관하세요. 현재 ZIP 업로드 복원 화면은 없어요.")
        if st.button("전체 백업 준비"):
            st.download_button("ZIP 다운로드",backup(db),"family_backup.zip","application/zip")
    with st.expander("접속 QR"):
        url=st.text_input("앱 주소",value=str(setting("APP_URL")),placeholder="https://your-app.streamlit.app")
        if url:
            parsed=urlparse(url.strip())
            if parsed.scheme not in ("http","https") or not parsed.netloc or len(url)>1000:
                st.error("올바른 앱 주소를 입력해주세요.")
            else:
                buf=BytesIO()
                qrcode.make(url.strip()).save(buf,format="PNG")
                st.image(buf.getvalue(),width=220)
                st.download_button("QR 다운로드",buf.getvalue(),"family_qr.png","image/png")


if __name__ == "__main__":
    main()
