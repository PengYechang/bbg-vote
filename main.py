import streamlit as st
import sqlite3
import pandas as pd
import requests
import xml.etree.ElementTree as ET
import time
import re
from html import unescape
from deep_translator import GoogleTranslator

# ==========================================
# 1. 数据库层 (架构重构：游戏库 + 关联)
# ==========================================
DB_FILE = "bgg_votes_v2.db" # 建议换个文件名以防冲突

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()

    # 1. 游戏库 (一次录入，永久保存)
    c.execute('''CREATE TABLE IF NOT EXISTS library (
                                                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                        bgg_id TEXT UNIQUE,
                                                        name TEXT,
                                                        year TEXT,
                                                        thumbnail TEXT,
                                                        description TEXT,
                                                        rating REAL,
                                                        rank TEXT,
                                                        weight REAL,
                                                        min_players INTEGER,
                                                        max_players INTEGER
                 )''')

    # 2. 投票房间
    c.execute('''CREATE TABLE IF NOT EXISTS polls (
                                                      id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                      title TEXT NOT NULL,
                                                      created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                 )''')

    # 3. 房间-游戏关联表 (投票候选项)
    c.execute('''CREATE TABLE IF NOT EXISTS poll_candidates (
                                                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                                                poll_id INTEGER,
                                                                library_id INTEGER,
                                                                FOREIGN KEY(poll_id) REFERENCES polls(id),
                                                                FOREIGN KEY(library_id) REFERENCES library(id)
                 )''')

    # 4. 投票记录
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

# --- 数据库操作函数 ---

def get_library_df():
    """获取所有库内游戏，用于 DataFrame 展示"""
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT * FROM library ORDER BY id DESC", conn)
    conn.close()
    return df

def add_game_to_library(game_data):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute('''INSERT OR IGNORE INTO library
                     (bgg_id, name, year, thumbnail, description, rating, rank, weight, min_players, max_players)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (game_data['id'], game_data['name'], game_data['year'],
                   game_data['thumbnail'], game_data['description'], game_data['rating'],
                   game_data['rank'], game_data['weight'], game_data['min_players'], game_data['max_players']))
        conn.commit()
        return True
    except Exception as e:
        print(e)
        return False
    finally:
        conn.close()

def delete_from_library(lib_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    # 级联删除：如果游戏库删了，相关的投票候选项也要删
    c.execute("DELETE FROM poll_candidates WHERE library_id = ?", (lib_id,))
    c.execute("DELETE FROM library WHERE id = ?", (lib_id,))
    conn.commit()
    conn.close()

def create_poll_with_games(title, library_ids):
    """创建一个新投票并批量加入游戏"""
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("INSERT INTO polls (title) VALUES (?)", (title,))
        poll_id = c.lastrowid

        # 批量插入关联
        data = [(poll_id, lib_id) for lib_id in library_ids]
        c.executemany("INSERT INTO poll_candidates (poll_id, library_id) VALUES (?, ?)", data)
        conn.commit()
        return True
    except Exception as e:
        print(e)
        return False
    finally:
        conn.close()

def get_polls():
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query("SELECT * FROM polls ORDER BY created_at DESC", conn)
    conn.close()
    return df

def delete_poll(poll_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM votes WHERE poll_id = ?", (poll_id,))
    c.execute("DELETE FROM poll_candidates WHERE poll_id = ?", (poll_id,))
    c.execute("DELETE FROM polls WHERE id = ?", (poll_id,))
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
    """
    复杂的联表查询：
    投票 -> 候选项 -> 游戏库
    """
    conn = sqlite3.connect(DB_FILE)

    query = '''
            SELECT
                pc.id as candidate_id,
                l.name, l.year, l.thumbnail, l.description, l.rating, l.rank,
                l.weight, l.min_players, l.max_players,
                COUNT(v.id) as count
            FROM poll_candidates pc
                     JOIN library l ON pc.library_id = l.id
                     LEFT JOIN votes v ON pc.id = v.candidate_id
            WHERE pc.poll_id = ?
            GROUP BY pc.id \
            '''
    df = pd.read_sql_query(query, conn, params=(poll_id,))

    # 获取详细投票人列表
    voters_query = "SELECT candidate_id, voter_nick FROM votes WHERE poll_id = ?"
    voters_df = pd.read_sql_query(voters_query, conn, params=(poll_id,))

    conn.close()

    if df.empty: return []

    results = df.to_dict('records')

    # 映射投票人
    voter_map = {}
    for _, row in voters_df.iterrows():
        cid = row['candidate_id']
        if cid not in voter_map: voter_map[cid] = set()
        voter_map[cid].add(row['voter_nick'])

    for r in results:
        r['voters'] = voter_map.get(r['candidate_id'], set())

    results.sort(key=lambda x: x['count'], reverse=True)
    return results

# ==========================================
# 2. API 层 (增加人数、重度解析，支持 Secrets)
# ==========================================

def contains_chinese(text):
    if not text: return False
    return bool(re.search(r'[\u4e00-\u9fff]', text))

def fetch_bgg_api(query):
    search_url = "https://boardgamegeek.com/xmlapi2/search"
    thing_url = "https://boardgamegeek.com/xmlapi2/thing"

    # Secrets 读取
    try:
        token = st.secrets["bgg"]["api_token"]
        headers = {"Authorization": f"Bearer {token}"}
    except:
        token = ""
        headers = {}
    headers["User-Agent"] = "GameLibraryManager/2.0"

    try:
        # Step 1: Search
        params_search = {"query": query, "type": "boardgame"}
        r_search = requests.get(search_url, params=params_search, headers=headers, timeout=10)
        if r_search.status_code != 200: return []
        items = ET.fromstring(r_search.content).findall("item")
        if not items: return []

        ids = [item.get("id") for item in items[:8]]
        if not ids: return []

        # Step 2: Get Details (stats=1)
        params_thing = {"id": ",".join(ids), "stats": "1"}
        r_thing = requests.get(thing_url, params=params_thing, headers=headers, timeout=10)
        root_thing = ET.fromstring(r_thing.content)

        results = []
        translator = GoogleTranslator(source='auto', target='zh-CN')

        for item in root_thing.findall("item"):
            try:
                g_id = item.get("id")

                # 名称处理 (优先中文)
                primary = "Unknown"
                cn_name = None
                for n in item.findall("name"):
                    if n.get("type") == "primary": primary = n.get("value")
                    if contains_chinese(n.get("value")): cn_name = n.get("value")

                display_name = cn_name if cn_name else translator.translate(primary)

                # 基础信息
                thumb = item.find("thumbnail")
                thumbnail = thumb.text if thumb is not None else ""
                year = item.find("yearpublished").get("value") if item.find("yearpublished") is not None else ""

                # === 新增：人数 ===
                min_p = item.find("minplayers").get("value") if item.find("minplayers") is not None else 0
                max_p = item.find("maxplayers").get("value") if item.find("maxplayers") is not None else 0

                # === 新增：统计数据 (含翻译简介、重度) ===
                stats = item.find("statistics")
                rating = 0.0
                rank_str = "N/A"
                weight = 0.0

                if stats is not None:
                    ratings = stats.find("ratings")
                    if ratings:
                        try: rating = round(float(ratings.find("average").get("value")), 1)
                        except: pass
                        try: weight = round(float(ratings.find("averageweight").get("value")), 2)
                        except: pass

                        ranks = ratings.find("ranks")
                        if ranks:
                            for rk in ranks.findall("rank"):
                                if rk.get("name") == "boardgame":
                                    val = rk.get("value")
                                    rank_str = f"No. {val}" if val.isdigit() else "-"

                # 简介翻译
                desc_node = item.find("description")
                desc_cn = "暂无简介"
                if desc_node is not None and desc_node.text:
                    full_desc = unescape(desc_node.text).replace('<br/>', '\n')
                    try: desc_cn = translator.translate(full_desc[:1000])
                    except: desc_cn = full_desc[:1000]

                results.append({
                    "id": g_id,
                    "name": display_name,
                    "year": year,
                    "thumbnail": thumbnail,
                    "description": desc_cn,
                    "rating": rating,
                    "rank": rank_str,
                    "weight": weight,
                    "min_players": min_p,
                    "max_players": max_p
                })
            except Exception as e:
                continue
        return results
    except Exception as e:
        print(e)
        return []

# ==========================================
# 3. UI 逻辑 (支持库管理)
# ==========================================
st.set_page_config(page_title="桌游助手", page_icon="🎲", layout="centered")
init_db()

# Session State
if "user_nick" not in st.session_state: st.session_state.user_nick = ""
if "admin_search_results" not in st.session_state: st.session_state.admin_search_results = []
if "is_searching" not in st.session_state: st.session_state.is_searching = False

def trigger_search():
    query = st.session_state.admin_query
    if query:
        st.session_state.is_searching = True
        try:
            results = fetch_bgg_api(query)
            st.session_state.admin_search_results = results
            if not results: st.toast("无结果", icon="🐢")
        except: st.toast("网络错误", icon="❌")
        finally: st.session_state.is_searching = False

def vote_cb(poll_id, candidate_id, nick):
    if not nick:
        st.toast("请填昵称", icon="⚠️")
        return
    if cast_vote(poll_id, candidate_id, nick): st.toast("成功", icon="🎉")
    else: st.toast("已投过", icon="✋")

# --- UI START ---
st.title("🎲 桌游投票站 2.0")
tab1, tab2 = st.tabs(["🙋 参与投票", "🔒 管理员后台"])

# ================= TAB 1: 用户投票 =================
with tab1:
    polls = get_polls()
    if polls.empty:
        st.info("暂无正在进行的投票")
    else:
        poll_map = {row['id']: f"{row['title']} ({row['created_at'][:10]})" for _, row in polls.iterrows()}
        with st.container(border=True):
            pid = st.selectbox("选择房间", options=poll_map.keys(), format_func=lambda x: poll_map[x])
            nick = st.text_input("昵称", value=st.session_state.user_nick)
            st.session_state.user_nick = nick

        st.markdown("### 🏆 候选项")
        results = get_poll_results(pid)

        for idx, cand in enumerate(results):
            rank_icon = ["🥇","🥈","🥉"][idx] if idx < 3 else f"#{idx+1}"

            with st.container(border=True):
                # 顶部：名字 + 年份
                st.markdown(f"**{rank_icon} {cand['name']}** <span style='color:grey'>({cand['year']})</span>", unsafe_allow_html=True)

                c_img, c_info = st.columns([1, 2.5])
                with c_img:
                    if cand['thumbnail']: st.image(cand['thumbnail'], use_container_width=True)
                    st.metric("票数", cand['count'])

                with c_info:
                    # 数据标签行
                    c_tag1, c_tag2, c_tag3 = st.columns(3)
                    c_tag4,c_tag5 = st.columns(2)
                    c_tag1.caption(f"BGG评分⭐ {cand['rating']}")
                    c_tag2.caption(f"BGG排名🏆 {cand['rank']}")
                    # 新增展示：重度和人数
                    c_tag3.caption(f"支持人数🏆  {cand['min_players']}-{cand['max_players']}人")
                    c_tag4.caption(f"重度(范围1~5)🧠 {cand['weight']}")
                    if cand['weight'] <= 2:
                        c_tag5.caption(f"策略级别⚽️ 轻度")
                    elif cand['weight'] <= 3:
                        c_tag5.caption(f"策略级别⚽️ 中度")
                    else:
                        c_tag5.caption(f"策略级别⚽ 重度")
                    # 简介
                    desc = cand['description'] or "无简介"
                    if len(desc) > 120:
                        st.caption(f"📝 {desc[:120]}...")
                        with st.expander("详情"): st.write(desc)
                    else:
                        st.caption(f"📝 {desc}")

                    st.write("")
                    # 投票按钮
                    has_voted = nick in cand['voters'] if nick else False
                    if has_voted:
                        st.button("✅ 已投", key=f"v_d_{cand['candidate_id']}", disabled=True, use_container_width=True)
                    else:
                        st.button("🗳️ 投一票", key=f"v_b_{cand['candidate_id']}", disabled=(not nick), type="primary", use_container_width=True,
                                  on_click=vote_cb, args=(pid, cand['candidate_id'], nick))

                if cand['voters']:
                    with st.expander(f"支持者 ({len(cand['voters'])})"):
                        st.write(", ".join(cand['voters']))

# ================= TAB 2: 管理员后台 =================
with tab2:
    admin_tab1, admin_tab2 = st.tabs(["📚 游戏库管理", "🚀 发起新投票"])

    # --- SubTab 1: 游戏库 (录入) ---
    with admin_tab1:
        st.info("先在这里搜索并添加游戏，然后在隔壁发起投票。")

        # 1. 搜索与添加
        with st.form(key="search_form", border=False):
            # vertical_alignment="bottom" 是关键，让输入框底部和按钮底部对齐
            c1, c2 = st.columns([3, 1], vertical_alignment="bottom")

            with c1:
                # 加一个 label 哪怕是空的，也能占位保证对齐更标准
                st.text_input("BGG 搜游戏", key="admin_query", placeholder="例如: 展翅翱翔, 搜不到使用英文或繁体试试")

            with c2:
                # 提交按钮
                if st.form_submit_button("🔎 搜", type="primary", use_container_width=True):
                    with st.spinner("查找中..."):
                        trigger_search()

        if st.session_state.admin_search_results:
            st.write(f"找到 {len(st.session_state.admin_search_results)} 个结果:")
            # 使用横向滚动展示搜索结果，节省空间
            for game in st.session_state.admin_search_results:
                with st.container(border=True):
                    ac1, ac2, ac3 = st.columns([1, 4, 1])
                    with ac1:
                        if game['thumbnail']: st.image(game['thumbnail'], width=60)
                    with ac2:
                        st.write(f"**{game['name']}** ({game['year']})")
                        st.caption(f"👥 {game['min_players']}-{game['max_players']}人 | 🧠 {game['weight']} | ⭐ {game['rating']}")
                    with ac3:
                        if st.button("➕ 入库", key=f"add_lib_{game['id']}", use_container_width=True):
                            if add_game_to_library(game):
                                st.toast(f"已存入: {game['name']}", icon="✅")

        st.divider()
        st.markdown("#### 📂 当前游戏库")

        # 2. 库内游戏管理 (表格展示)
        lib_df = get_library_df()
        if not lib_df.empty:
            # 简单展示表格
            st.dataframe(
                lib_df[['id', 'name', 'rating', 'weight', 'min_players', 'max_players']],
                hide_index=True,
                use_container_width=True
            )

            # 删除功能
            with st.expander("🗑️ 删除库中游戏"):
                del_id = st.selectbox("选择要删除的游戏", options=lib_df['id'], format_func=lambda x: lib_df[lib_df['id']==x]['name'].values[0])
                if st.button("确认删除 (会连带删除相关投票)", type="primary"):
                    delete_from_library(del_id)
                    st.rerun()
        else:
            st.write("库里还是空的，快去搜索添加吧。")

    # --- SubTab 2: 发起投票 (批量选择) ---
    with admin_tab2:
        st.markdown("#### 1. 设置主题")
        new_poll_title = st.text_input("投票标题", placeholder="例如: 周五重策局选哪个？")

        st.markdown("#### 2. 勾选游戏")

        lib_df = get_library_df()
        if lib_df.empty:
            st.warning("请先去“游戏库管理”添加游戏！")
        else:
            # === 修改处 2：全选/全不选逻辑 ===

            # 初始化状态控制变量
            if "select_mode" not in st.session_state: st.session_state.select_mode = False # 默认不选
            if "editor_key_seed" not in st.session_state: st.session_state.editor_key_seed = 0 # 用于强制重绘的种子

            # 功能按钮区
            col_btn1, col_btn2, col_space = st.columns([1, 1, 4])
            with col_btn1:
                if st.button("✅ 全选", use_container_width=True):
                    st.session_state.select_mode = True
                    st.session_state.editor_key_seed += 1 # 改变 key，强制下方表格刷新
                    st.rerun()
            with col_btn2:
                if st.button("⬜ 清空", use_container_width=True):
                    st.session_state.select_mode = False
                    st.session_state.editor_key_seed += 1
                    st.rerun()

            # 准备数据：根据状态设置“选择”列的默认值
            select_df = lib_df[['id', 'name', 'rating', 'weight', 'min_players', 'max_players']].copy()
            # 将 select_mode (True/False) 赋值给所有行
            select_df.insert(0, "选择", st.session_state.select_mode)

            # 渲染表格
            # key 必须包含 seed，这样点击按钮后组件会被完全重建，从而应用新的默认值
            edited_df = st.data_editor(
                select_df,
                column_config={
                    "选择": st.column_config.CheckboxColumn(required=True),
                    "id": None,
                    "name": "游戏名称",
                    "rating": "评分",
                    "weight": "重度",
                    "min_players": "Min",
                    "max_players": "Max"
                },
                disabled=["id", "name", "rating", "weight", "min_players", "max_players"],
                hide_index=True,
                use_container_width=True,
                key=f"game_selector_{st.session_state.editor_key_seed}"
            )

            # 获取最终勾选的 ID
            selected_ids = edited_df[edited_df["选择"] == True]['id'].tolist()

            st.markdown("#### 3. 创建")
            btn_col1, btn_col2 = st.columns([1, 1], vertical_alignment="bottom")
            with btn_col1:
                st.caption(f"已选 {len(selected_ids)} 个游戏")
            with btn_col2:
                if st.button("🚀 发起投票", type="primary", use_container_width=True, disabled=len(selected_ids)==0):
                    if not new_poll_title:
                        st.toast("标题不能为空", icon="⚠️")
                    else:
                        create_poll_with_games(new_poll_title, selected_ids)
                        st.success("创建成功！可前往“参与投票”页查看。")
                        time.sleep(1)

        st.divider()
        with st.popover("🗑️ 删除旧投票房间"):
            all_polls = get_polls()
            if not all_polls.empty:
                del_pid = st.selectbox("选择要删除的房间", options=all_polls['id'], format_func=lambda x: all_polls[all_polls['id']==x]['title'].values[0])
                if st.button("确认销毁"):
                    delete_poll(del_pid)
                    st.rerun()
            else:
                st.write("没有房间。")

# --- Footer ---
st.markdown("---")
# Image logic...