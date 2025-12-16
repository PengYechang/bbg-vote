import streamlit as st
import sqlite3
import pandas as pd
import requests
import xml.etree.ElementTree as ET
import time
import base64
import os
import re
from html import unescape
from deep_translator import GoogleTranslator

# ==========================================
# 1. 数据库与逻辑层
# ==========================================
DB_FILE = "bgg_votes.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS polls (
                                                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                      title TEXT NOT NULL,
                                                      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                 )''')

    # 扩展 candidates 表，增加 description, rating, bgg_rank
    c.execute('''CREATE TABLE IF NOT EXISTS candidates (
                                                           id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                           poll_id INTEGER,
                                                           bgg_id TEXT,
                                                           name TEXT,
                                                           year TEXT,
                                                           thumbnail TEXT,
                                                           description TEXT,
                                                           rating REAL,
                                                           bgg_rank TEXT,
                                                           FOREIGN KEY(poll_id) REFERENCES polls(id)
                 )''')

    c.execute('''CREATE TABLE IF NOT EXISTS votes (
                                                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                      poll_id INTEGER,
                                                      candidate_id INTEGER,
                                                      voter_nick TEXT,
                                                      timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                                                      UNIQUE(poll_id, candidate_id, voter_nick)
                 )''')
    conn.commit()
    conn.close()

def create_poll(title):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO polls (title) VALUES (?)", (title,))
    conn.commit()
    conn.close()

def get_polls():
    conn = sqlite3.connect(DB_FILE)
    # 按照创建时间倒序
    df = pd.read_sql_query("SELECT * FROM polls ORDER BY created_at DESC", conn)
    conn.close()
    return df

def delete_poll(poll_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM votes WHERE poll_id = ?", (poll_id,))
    c.execute("DELETE FROM candidates WHERE poll_id = ?", (poll_id,))
    c.execute("DELETE FROM polls WHERE id = ?", (poll_id,))
    conn.commit()
    conn.close()

def add_candidate_to_db(poll_id, game_data):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id FROM candidates WHERE poll_id=? AND bgg_id=?", (poll_id, game_data['id']))
    if not c.fetchone():
        c.execute('''INSERT INTO candidates
                         (poll_id, bgg_id, name, year, thumbnail, description, rating, bgg_rank)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                  (poll_id, game_data['id'], game_data['name'], game_data['year'],
                   game_data['thumbnail'], game_data['description'], game_data['rating'], game_data['rank']))
        conn.commit()
    conn.close()

def delete_candidate(candidate_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM votes WHERE candidate_id = ?", (candidate_id,))
    c.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
    conn.commit()
    conn.close()

def cast_vote(poll_id, candidate_id, nick):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO votes (poll_id, candidate_id, voter_nick) VALUES (?, ?, ?)",
                  (poll_id, candidate_id, nick))
        conn.commit()
        success = True
    except sqlite3.IntegrityError:
        success = False
    conn.close()
    return success

def get_poll_results(poll_id):
    conn = sqlite3.connect(DB_FILE)
    candidates_df = pd.read_sql_query("SELECT * FROM candidates WHERE poll_id = ?", conn, params=(poll_id,))
    votes_df = pd.read_sql_query("SELECT candidate_id, COUNT(*) as count FROM votes WHERE poll_id = ? GROUP BY candidate_id", conn, params=(poll_id,))
    voters_df = pd.read_sql_query("SELECT candidate_id, voter_nick FROM votes WHERE poll_id = ?", conn, params=(poll_id,))
    conn.close()

    if candidates_df.empty: return []

    merged = pd.merge(candidates_df, votes_df, left_on='id', right_on='candidate_id', how='left')
    merged['count'] = merged['count'].fillna(0).astype(int)
    results = merged.to_dict('records')

    voter_map = {}
    for _, row in voters_df.iterrows():
        cid = row['candidate_id']
        if cid not in voter_map: voter_map[cid] = set()
        voter_map[cid].add(row['voter_nick'])

    for r in results:
        r['voters'] = voter_map.get(r['id'], set())

    results.sort(key=lambda x: x['count'], reverse=True)
    return results


# ==========================================
# 2. API 服务 (中文适配版)
# ==========================================
def contains_chinese(text):
    """检查字符串是否包含中文字符"""
    if not text: return False
    return bool(re.search(r'[\u4e00-\u9fff]', text))

def translate_text(text, target='zh-CN'):
    """调用 Google 翻译"""
    if not text: return ""
    try:
        # 限制长度以加快速度并防止报错，通常前500个字符足够了解游戏
        text_to_translate = text[:800]
        return GoogleTranslator(source='auto', target=target).translate(text_to_translate)
    except Exception as e:
        print(f"Translation failed: {e}")
        return text # 如果翻译失败，回退到原文

def fetch_bgg_api(query):
    search_url = "https://boardgamegeek.com/xmlapi2/search"
    thing_url = "https://boardgamegeek.com/xmlapi2/thing"

    try:
        # 读取 toml 中 [bgg] 下的 api_token
        # 注意：如果本地没有 secrets.toml 文件或云端未配置，这里会报错
        token = st.secrets["bgg"]["api_token"]
    except Exception:
        #以此防止如果没有配置key导致程序崩坏，给予提示或设为空
        token = ""
        print("Warning: BGG Token not found in secrets.")

    headers = {
        "User-Agent": "GameNightVoter/1.1 (Personal Project)",
        "Authorization": f"Bearer {token}"
    }

    try:
        # Step 1: Search
        # BGG 支持中文搜索（例如搜“卡坦岛”能搜到），但返回的列表通常还是以英文为主
        params_search = {"query": query, "type": "boardgame"}
        r_search = requests.get(search_url, params=params_search, headers=headers, timeout=10)

        if r_search.status_code != 200: return []
        root_search = ET.fromstring(r_search.content)
        items = root_search.findall("item")
        if not items: return []

        # 提取前 8 个 ID (翻译比较慢，减少数量以提升体验)
        ids = [item.get("id") for item in items[:8]]
        if not ids: return []

        # Step 2: Batch Get Details
        ids_str = ",".join(ids)
        params_thing = {"id": ids_str, "stats": "1"}
        r_thing = requests.get(thing_url, params=params_thing, headers=headers, timeout=10)

        if r_thing.status_code != 200: return []
        root_thing = ET.fromstring(r_thing.content)

        results = []

        # 实例化翻译器（复用对象稍微快一点）
        translator = GoogleTranslator(source='auto', target='zh-CN')

        for item in root_thing.findall("item"):
            try:
                g_id = item.get("id")

                # --- 1. 处理名称 (优先找官方中文名) ---
                primary_name = "Unknown"
                chinese_name = None

                # 遍历所有 name 标签
                for name_node in item.findall("name"):
                    if name_node.get("type") == "primary":
                        primary_name = name_node.get("value")

                    # 检查是否包含中文
                    val = name_node.get("value")
                    if contains_chinese(val):
                        chinese_name = val # 找到了官方录入的中文名

                # 决策：如果有官方中文名用官方的，没有则机器翻译 Primary Name
                if chinese_name:
                    display_name = chinese_name
                else:
                    # 只有当名字是英文时才去翻译，避免重复翻译
                    display_name = translator.translate(primary_name)

                # 为了方便确认，如果翻译了，保留一下原名在括号里（可选）
                # if display_name != primary_name and not chinese_name:
                #     display_name = f"{display_name} ({primary_name})"

                # --- 2. 图片与年份 ---
                thumb_node = item.find("thumbnail")
                thumbnail = thumb_node.text if thumb_node is not None else ""

                year_node = item.find("yearpublished")
                year = year_node.get("value") if year_node is not None else "N/A"

                # --- 3. 处理描述 (强制翻译) ---
                desc_node = item.find("description")
                description_cn = "暂无简介"

                if desc_node is not None and desc_node.text:
                    full_desc = unescape(desc_node.text).replace('<br/>', '\n')
                    # 翻译简介 (耗时操作)
                    try:
                        # 截取前 500 字符翻译，不用全翻，为了速度
                        description_cn = translator.translate(full_desc[:500])
                    except:
                        description_cn = full_desc[:500] # 失败返英文

                # --- 4. 评分与排名 ---
                stats_node = item.find("statistics")
                rating = 0.0
                rank_val = "N/A"
                if stats_node is not None:
                    ratings = stats_node.find("ratings")
                    if ratings is not None:
                        avg_node = ratings.find("average")
                        if avg_node is not None:
                            try: rating = round(float(avg_node.get("value")), 1)
                            except: pass
                        ranks = ratings.find("ranks")
                        if ranks is not None:
                            for rk in ranks.findall("rank"):
                                if rk.get("name") == "boardgame":
                                    val = rk.get("value")
                                    rank_val = f"No. {val}" if val.isdigit() else "Unranked"
                                    break

                results.append({
                    "id": g_id,
                    "name": display_name, # 这里存入的是中文名
                    "year": year,
                    "thumbnail": thumbnail,
                    "description": description_cn, # 这里存入的是中文简介
                    "rating": rating,
                    "rank": rank_val
                })
            except Exception as e:
                print(f"Parse error: {e}")
                continue
        return results

    except Exception as e:
        print(f"API Error: {e}")
        return []

# ==========================================
# 3. 移动端 UI 设计
# ==========================================

st.set_page_config(page_title="桌游投票", page_icon="🎲", layout="centered")
init_db()

# 状态管理
if "user_nick" not in st.session_state: st.session_state.user_nick = ""
if "admin_search_results" not in st.session_state: st.session_state.admin_search_results = []
if "is_searching" not in st.session_state: st.session_state.is_searching = False

# 辅助函数：格式化时间
def format_poll_label(row):
    # 将 SQLite 的时间字符串转换为更易读的格式（截取日期部分即可）
    date_part = row['created_at'].split(' ')[0]
    return f"{row['title']} ({date_part})"

# 回调函数
def trigger_search():
    query = st.session_state.admin_query
    if query:
        st.session_state.is_searching = True
        try:
            results = fetch_bgg_api(query)
            st.session_state.admin_search_results = results
            if not results:
                st.toast("无结果 (请检查网络或关键词)", icon="🐢")
        except Exception as e:
            st.toast(f"Error: {e}", icon="❌")
        finally:
            st.session_state.is_searching = False

def admin_add_game_callback(poll_id, game):
    add_candidate_to_db(poll_id, game)
    st.toast(f"已添加: {game['name']}", icon="✅")

def user_vote_callback(poll_id, candidate_id, nick):
    if not nick:
        st.toast("请填写昵称", icon="⚠️")
        return
    success = cast_vote(poll_id, candidate_id, nick)
    if success: st.toast("投票成功", icon="🎉")
    else: st.toast("已投过该项", icon="✋")

# --- UI 结构 ---
st.title("🎲 桌游投票站")
tab1, tab2 = st.tabs(["🙋 参与投票", "🔧 发起与管理"])

# Tab 1: 用户投票
with tab1:
    polls = get_polls()
    if polls.empty:
        st.info("暂无投票，请去隔壁创建一个！")
    else:
        # 修改点 2：显示创建时间
        poll_map = {row['id']: format_poll_label(row) for _, row in polls.iterrows()}

        with st.container(border=True):
            selected_poll_id = st.selectbox(
                "选择房间",
                options=poll_map.keys(),
                format_func=lambda x: poll_map[x]
            )
            user_nick = st.text_input("你的昵称 (必填)", value=st.session_state.user_nick, placeholder="是谁在投票？")
            st.session_state.user_nick = user_nick

        st.markdown("### 🏆 实时排名")
        results = get_poll_results(selected_poll_id)

        if not results:
            st.info("等待管理员添加桌游...")
        else:
            for idx, cand in enumerate(results):
                rank_icon = f"#{idx+1}"
                if idx == 0: rank_icon = "🥇"
                elif idx == 1: rank_icon = "🥈"
                elif idx == 2: rank_icon = "🥉"

                # 处理可能为空的旧数据字段
                rating_display = cand.get('rating', 0.0) or 0.0
                rank_display = cand.get('bgg_rank', 'N/A') or 'N/A'
                desc_display = cand.get('description', '暂无简介') or '暂无简介'

                with st.container(border=True):
                    # 标题行：排名 + 名称 + 年份
                    st.markdown(f"**{rank_icon} {cand['name']}** <span style='color:grey; font-size:0.8em'>({cand['year']})</span>", unsafe_allow_html=True)

                    c_img, c_stat = st.columns([1, 2])
                    with c_img:
                        if cand['thumbnail']: st.image(cand['thumbnail'], use_container_width=True)
                        else: st.write("🖼️")

                        # 票数显示移到图片下方，更显眼
                        st.metric("当前票数", cand['count'])

                    with c_stat:
                        # 修改点 3：显示评分、排名和简介
                        sub_c1, sub_c2 = st.columns(2)
                        sub_c1.caption(f"⭐ BGG {rating_display}")
                        sub_c2.caption(f"🏆 {rank_display}")

                        st.caption(f"📝 {desc_display}")

                        st.write("") # Spacer

                        has_voted = user_nick in cand['voters'] if user_nick else False
                        if has_voted:
                            st.button("✅ 已投", key=f"v_done_{cand['id']}", disabled=True, use_container_width=True)
                        else:
                            st.button(
                                "🗳️ 投一票",
                                key=f"v_btn_{cand['id']}",
                                disabled=(not user_nick),
                                type="primary",
                                use_container_width=True,
                                on_click=user_vote_callback,
                                args=(selected_poll_id, cand['id'], user_nick)
                            )

                    if cand['voters']:
                        with st.expander(f"支持者 ({len(cand['voters'])})"):
                            st.write(", ".join(list(cand['voters'])))

# Tab 2: 管理员后台
with tab2:
    with st.expander("➕ 发起新投票", expanded=False):
        new_title = st.text_input("主题名称", placeholder="例如: 周五桌游夜")
        if st.button("创建", use_container_width=True):
            if new_title:
                create_poll(new_title)
                st.success("创建成功")
                time.sleep(0.5)
                st.rerun()
    st.markdown("---")
    polls = get_polls()
    if not polls.empty:
        # 同样应用时间格式化
        poll_map_admin = {row['id']: format_poll_label(row) for _, row in polls.iterrows()}
        manage_pid = st.selectbox("管理哪个房间？", options=poll_map_admin.keys(), format_func=lambda x: poll_map_admin[x])

        with st.popover("❌ 删除此房间"):
            if st.button("确认删除", type="primary", use_container_width=True):
                delete_poll(manage_pid)
                st.rerun()

        st.markdown("#### 🔍 搜索添加")

        # 修改点 1：恢复输入框
        with st.form(key="search_form", border=False):
            col_search_input, col_search_btn = st.columns([3, 1])

            with col_search_input:
                # 这里的 key="admin_query" 会自动绑定到 session_state
                st.text_input(
                    "输入桌游名",
                    key="admin_query",
                    placeholder="例如: 展翅翱翔, 卡坦岛...",
                    label_visibility="collapsed"
                )

            with col_search_btn:
                # 注意：在 form 内部必须使用 form_submit_button
                submitted = st.form_submit_button("🔎 搜", use_container_width=True, type="primary")

            if submitted:
                with st.spinner("正在请求 BGG ..."):
                    trigger_search()

        if st.session_state.admin_search_results:
            st.caption(f"找到 {len(st.session_state.admin_search_results)} 个结果")
            for game in st.session_state.admin_search_results:
                with st.container(border=True):
                    c1, c2, c3 = st.columns([1, 3, 1])
                    with c1:
                        if game['thumbnail']: st.image(game['thumbnail'], use_container_width=True)
                    with c2:
                        # 搜索结果也展示详细信息，方便确认
                        st.markdown(f"**{game['name']}** ({game['year']})")
                        st.caption(f"⭐ {game['rating']} | 🏆 {game['rank']}")
                        st.caption(game['description'][:120] + "...")
                    with c3:
                        st.button("➕", key=f"add_{game['id']}", on_click=admin_add_game_callback, args=(manage_pid, game), use_container_width=True)

        st.markdown("#### 📋 已添加桌游")
        curr_games = get_poll_results(manage_pid)
        for cg in curr_games:
            with st.container(border=True):
                col_a, col_b = st.columns([4, 1])
                with col_a:
                    st.write(f"**{cg['name']}**")
                with col_b:
                    if st.button("🗑️", key=f"del_{cg['id']}"):
                        delete_candidate(cg['id'])
                        st.rerun()
    else:
        st.info("请先创建投票。")

# --- Footer ---
st.markdown("---")
# st.caption("Data provided by **BoardGameGeek XML API 2**. This is a non-commercial fan project.")
# st.caption("Review terms at: https://boardgamegeek.com/using_the_xml_api")

image_path = "images/powered-bgg.webp"
# 检查文件是否存在
if not os.path.exists(image_path):
    st.error(f"找不到文件: {image_path}")
else:
    # 2. 读取文件并转码
    with open(image_path, "rb") as f:
        img_data = f.read()
        b64_data = base64.b64encode(img_data).decode()
        # 自动判断 MIME 类型 (这里假定是 webp，如果是 png 改成 image/png)
        mime_type = "image/webp"

    # 3. 嵌入 HTML
    st.html(f"""
    <a href="https://boardgamegeek.com/">
      <img src="data:{mime_type};base64,{b64_data}" width="160" alt="Powered by BGG">
    </a>
    """)
