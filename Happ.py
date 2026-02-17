import streamlit as st
import json
from openai import OpenAI

# ==============================
# Streamlit設定
# ==============================
st.set_page_config(page_title="発達支援相談AIエージェント", layout="centered")

# ==============================
# パスワード認証
# ==============================
PASSWORD = "forest2025"

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.markdown("<h2 style='text-align:center;'>🌿 発達支援相談AIエージェント</h2>", unsafe_allow_html=True)
    pwd = st.text_input("パスワードを入力してください", type="password")
    if st.button("ログイン"):
        if pwd == PASSWORD:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("パスワードが違います。")
    st.stop()

# ==============================
# OpenAI設定（Secrets）
# ==============================
API_KEY = st.secrets.get("OPENAI_API_KEY", "")
if not API_KEY:
    st.error("OPENAI_API_KEY が設定されていません。Streamlit Secrets に追加してください。")
    st.stop()

client = OpenAI(api_key=API_KEY)

# ==============================
# JSON読み込み（乳幼児対応 v2.1）
# ==============================
KB_FILE = "nd_kb_v2_infant_v2.1.json"

with open(KB_FILE, "r", encoding="utf-8") as f:
    kb = json.load(f)

# ==============================
# セッション初期化（会話 + 収集情報）
# ==============================
if "messages" not in st.session_state:
    st.session_state.messages = []  # (text, sender) sender in {"user","bot"}

if "profile" not in st.session_state:
    # 会話の中で埋めていく「情報スロット」
    st.session_state.profile = {
        "age_or_grade": "",        # 年齢/学年
        "setting": "",             # 困りが主に出る場所（家庭/園/学校/外出など）
        "main_concern": "",        # いちばん困っていること
        "frequency_severity": "",  # どのくらい/どの程度
        "triggers": "",            # きっかけ（いつ/何の後/何があると）
        "what_tried": "",          # すでに試したこと
        "strengths": "",           # できていること/得意/安心材料
        "parent_state": "",        # 保護者の疲れ・不安度（主観）
    }

if "last_category" not in st.session_state:
    st.session_state.last_category = None

# ==============================
# カテゴリ判定（キーワード一致）
# ==============================
def score_categories(text: str):
    scores = []
    for cat in kb.get("categories", []):
        score = 0
        for kw in cat.get("nlp_keywords", []):
            if kw and kw in text:
                score += 1
        scores.append((cat.get("name", "不明"), score, cat))
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores

# ==============================
# JSONから「支援の材料」を抽出（形式差に強く）
# ==============================
def extract_support_materials(cat: dict):
    """
    いろんなJSON形式に耐えるため、recommended_supports の中身を広めに拾って
    LLMに「材料」として渡す（出典は渡さない/表示しない）
    """
    supports = cat.get("recommended_supports", {}) or {}
    materials = []

    # パターン1: immediate/short_term/long_term が list[dict] で入っている
    for k in ["immediate", "short_term", "long_term", "long_term_community_professional"]:
        v = supports.get(k)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    desc = item.get("description")
                    rat = item.get("rationale")
                    if desc:
                        materials.append(f"・{desc}" + (f"（意図/理由: {rat}）" if rat else ""))

    # パターン2: home_immediate / school_immediate など list[str] で入っている
    for k, v in supports.items():
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            # ラベルを少しわかりやすく
            label = k.replace("_", " ")
            for s in v[:6]:
                materials.append(f"・[{label}] {s}")

    # 追加で典型/コミュニケーションTIPSも材料として渡す
    for s in cat.get("typical_tendencies", [])[:6]:
        if isinstance(s, str) and s.strip():
            materials.append(f"・(傾向) {s}")

    for s in cat.get("communication_tips_for_guardians", [])[:6]:
        if isinstance(s, str) and s.strip():
            materials.append(f"・(声かけ) {s}")

    for s in cat.get("risk_signals", [])[:4]:
        if isinstance(s, str) and s.strip():
            materials.append(f"・(注意サイン) {s}")

    # 念のため重複除去
    seen = set()
    uniq = []
    for m in materials:
        if m not in seen:
            uniq.append(m)
            seen.add(m)

    return uniq[:18]  # 多すぎるとプロンプトが重くなるので上限

# ==============================
# 収集スロットの更新（簡易）
# ==============================
def update_profile_from_user_text(user_text: str):
    """
    厳密抽出はせず、保護者が書いてきた内容をprofileに「メモ」的に追記していく。
    ここは後で強化してもOK。
    """
    # main_concern が空なら入れる
    if not st.session_state.profile["main_concern"]:
        st.session_state.profile["main_concern"] = user_text[:80]

    # parent_state が空なら、疲れ/不安っぽい語があれば入れる
    if not st.session_state.profile["parent_state"]:
        keywords = ["しんどい", "つらい", "疲れ", "不安", "限界", "イライラ", "心配", "泣きたい"]
        if any(k in user_text for k in keywords):
            st.session_state.profile["parent_state"] = "不安や疲れがある様子"

# ==============================
# 次に聞く質問を作る（足りないスロット中心）
# ==============================
def build_followup_questions(profile: dict):
    q = []
    if not profile["age_or_grade"]:
        q.append("お子さんの年齢（または学年）を教えてください。")
    if not profile["setting"]:
        q.append("困りごとは主にどこで目立ちますか？（家庭／園・学校／外出先 など）")
    if not profile["frequency_severity"]:
        q.append("それは週にどれくらいの頻度で、どの程度困りますか？（例：毎日、10分以上続く など）")
    if not profile["triggers"]:
        q.append("起きやすい“きっかけ”はありますか？（切り替え、疲れ、空腹、音、予定変更 など）")
    if not profile["what_tried"]:
        q.append("すでに試した工夫があれば教えてください（うまくいった/いかなかった両方）。")
    if not profile["strengths"]:
        q.append("逆に、落ち着いている場面・得意なこと・安心できるものはありますか？")
    if not profile["parent_state"]:
        q.append("保護者としてのしんどさは今どれくらいですか？（0〜10 でもOK）")

    return q[:3]  # いきなり多いと負担なので最大3つ

# ==============================
# GPT回答生成（出典なし・聞き役・具体策・質問）
# ==============================
def generate_response(history, category_name, user_input, materials, profile):
    # 履歴は直近数ターンのみ
    history_text = "\n".join(
        [f"保護者: {m[0]}" if m[1] == "user" else f"AI: {m[0]}" for m in history[-6:]]
    )

    # 次の質問
    followups = build_followup_questions(profile)
    followup_text = "\n".join([f"- {x}" for x in followups]) if followups else "- ここまで聞いて大丈夫です。今いちばんしんどい場面を一つだけ教えてください。"

    # JSON材料
    mat_text = "\n".join(materials) if materials else "（支援材料が少ないため、一般的な環境調整と声かけを中心に提案する）"

    prompt = f"""
あなたは保護者支援専門の、やさしく実務的な発達支援カウンセラーです。
「診断」ではなく「家庭でできる工夫の選択肢を増やす」ことが目的です。
保護者のストレスを下げる“聞き役”も大事にし、責めない言葉で寄り添ってください。

重要ルール：
- 出典やガイドライン名、組織名（例：文科省、NICE、学会名など）を絶対に書かない。
- 「受診してください」「相談してください」だけで終わらず、今日からできる具体策を必ず入れる。
- 断定しない（〜かもしれません、〜の可能性、など）。
- 文章は会話調で、読みやすい改行を入れる。
- 文字数は目安 500〜800字（短すぎない）。

【これまでの相談履歴】
{history_text}

【保護者からの今回の相談】
{user_input}

【推定される特性（参考）】
{category_name}

【これまでに集まっている情報（メモ）】
- 年齢/学年: {profile.get("age_or_grade","")}
- 場所: {profile.get("setting","")}
- 困りごと: {profile.get("main_concern","")}
- 頻度/程度: {profile.get("frequency_severity","")}
- きっかけ: {profile.get("triggers","")}
- 試したこと: {profile.get("what_tried","")}
- できていること/強み: {profile.get("strengths","")}
- 保護者の状態: {profile.get("parent_state","")}

【この特性で使える支援の材料（JSON由来）】
{mat_text}

あなたの出力は次の構成で：
1) 共感（保護者の気持ちの受け止め）
2) 背景の理解（専門用語なし。努力不足ではない、の方向）
3) 今日からできる工夫（家庭で3〜5個、具体例つき）
4) 園・学校で頼める配慮（2〜3個、言い方例も）
5) 注意サイン（1〜2個だけ、怖がらせず短く）
6) 次に私が知りたいこと（質問を1〜3個。下の質問案から選んで自然に）

【質問案】
{followup_text}
"""

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
    )
    return response.choices[0].message.content.strip()

# ==============================
# UIスタイル（吹き出し）
# ==============================
st.markdown("""
<style>
body { background-color: #fff7ed; font-family: 'Zen Maru Gothic', sans-serif; }

.user-bubble {
    background: #dff4ff;
    padding: 14px;
    margin: 10px 0;
    text-align: right;
    border-radius: 18px 18px 0px 18px;
    border: 1px solid #96c7e6;
    max-width: 85%;
    margin-left: auto;
    white-space: pre-wrap;
    word-break: break-word;
}

.bot-bubble {
    background: #fffdf8;
    padding: 14px;
    margin: 10px 0;
    text-align: left;
    border-radius: 18px 18px 18px 0px;
    border: 1px solid #e5c7a5;
    max-width: 85%;
    margin-right: auto;
    white-space: pre-wrap;
    word-break: break-word;
}

.title {
    font-size: 30px;
    font-family: 'Zen Maru Gothic';
    text-align: center;
    font-weight: 700;
    color: #405c3d;
}
.small-note {
    color: #6b7280;
    font-size: 0.9rem;
}
</style>
""", unsafe_allow_html=True)

st.markdown('<div class="title">🌿 発達支援相談AIエージェント</div>', unsafe_allow_html=True)
st.markdown('<div class="small-note">※診断ではなく、家庭でできる工夫の選択肢を増やすための相談です。</div>', unsafe_allow_html=True)

# ==============================
# チャット履歴表示
# ==============================
for msg, sender in st.session_state.messages:
    bubble = "user-bubble" if sender == "user" else "bot-bubble"
    st.markdown(f'<div class="{bubble}">{msg}</div>', unsafe_allow_html=True)

# ==============================
# 入力欄（複数行・送信後に確実にクリア）
# ==============================
if "user_input" not in st.session_state:
    st.session_state.user_input = ""

def submit():
    user_text = st.session_state.get("user_input", "").strip()
    if not user_text:
        st.warning("何か入力してください。")
        return

    # まず履歴に追加
    st.session_state.messages.append((user_text, "user"))

    # profile更新（簡易）
    update_profile_from_user_text(user_text)

    # 判定用テキスト（履歴も少し混ぜる：インタラクティブに効く）
    recent_user_texts = " ".join([m[0] for m in st.session_state.messages if m[1] == "user"][-3:])
    judge_text = (recent_user_texts + " " + user_text).strip()

    scores = score_categories(judge_text)
    selected_name, _, selected_category = scores[0] if scores else ("（推定不可）", 0, {})

    st.session_state.last_category = selected_name

    # JSON材料抽出
    materials = extract_support_materials(selected_category)

    with st.spinner("AIエージェントが考えています…"):
        try:
            answer = generate_response(
                st.session_state.messages,
                selected_name,
                user_text,
                materials,
                st.session_state.profile
            )
        except Exception as e:
            st.error(f"エラー: {e}")
            return

    # AI回答を履歴へ（出典は付けない）
    st.session_state.messages.append((answer, "bot"))

    # 入力欄クリア（この方式は Streamlit Cloud でも安定）
    st.session_state["user_input"] = ""
    st.rerun()

st.text_area(
    "ご相談内容を入力してください（改行OK）",
    height=180,               # ←ここで入力欄を大きく
    placeholder="例）園で切り替えが苦手で泣いてしまう／家で落ち着きがなくて困っている…など",
    key="user_input"
)

col1, col2 = st.columns([3, 1])
with col1:
    st.button("送信 🌱", on_click=submit, use_container_width=True)
with col2:
    if st.button("リセット", use_container_width=True):
        st.session_state.messages = []
        st.session_state.last_category = None
        st.session_state.profile = {
            "age_or_grade": "",
            "setting": "",
            "main_concern": "",
            "frequency_severity": "",
            "triggers": "",
            "what_tried": "",
            "strengths": "",
            "parent_state": "",
        }
        st.session_state["user_input"] = ""
        st.rerun()
