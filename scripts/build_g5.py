#!/usr/bin/env python3
"""Generate a versioned, synthetic G5 candidate corpus. No external data/API."""
from __future__ import annotations

import csv
import hashlib
import itertools
import json
import random
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "data" / "g5"
DATASET_VERSION = "g5-candidate-v0.2"
# Keep v0.1 case IDs, fact order and noise placement for paired comparisons.
IDENTITY_SEED = "g5-candidate-v0.1"
TIERS = {"easy": (10, 3), "medium": (40, 15), "hard": (100, 40)}
TYPES = ("npc_identity", "trap", "secret_door", "other_player_private", "unrevealed_event")
QTYPES = ("general", "direct", "indirect", "false_premise")
AUTH = ("unauthorized", "authorized")
A, B, GM, PUBLIC = "PLAYER_A", "PLAYER_B", "GM", "PUBLIC"

DESKS = ("換卷側櫃", "手推箱旁", "二號風口", "門楣下的掛板", "修補桌內側", "木屏北角", "曬網棚", "窄井邊的壁格", "轉盤底層", "長渠彎口", "南面內檯", "圓窗前的格板", "梯上備品架", "庫外門鈴旁", "漏沙臺後方", "折簾前", "背風候位", "高欄角的窄箱", "壁鐘旁的斜台", "換裝室外格")
ITEMS = ("黃麻繩圈", "陶製調色碟", "鉛灰帳皮", "細格抄板", "空白索引籤", "桐木楔", "切角收據", "紙筆套管", "未染水珠瓶", "皮柄收卷夾", "備用掩光片", "布面護筆袋", "紅線量片", "藍紋蓋盒", "折痕票根", "淺漆轉環", "淡青篩片", "封紙擺架", "舊陶桌腳", "散頁方匣")
PEOPLE = ("樂亭婆", "阿澄", "永紗", "雲印", "梨秋", "荻順", "庭雨", "壽甕", "流白", "檜君", "霧凜", "曲折", "海豆", "山燭", "劉珠", "黛時", "汐墨", "原耕")
CLASSES = ("場務留存", "物件清點", "訪客換班", "修繕巡記", "臨窗收件", "備用雜項")
STATUS = ("待清點", "只供借閱", "未排班", "還在晾乾", "已劃進清冊", "只收影本", "晚間交接", "不列入今日使用", "要原件歸還", "隔班核對")
MATERIALS = ("紙纖", "松木", "銅絲", "乾蠟", "釉土", "苧線", "米漿", "窯灰", "薄陶", "明礬")

def rng_for(slug: str) -> random.Random:
    digest = hashlib.sha256((IDENTITY_SEED + ":" + slug).encode()).digest()
    return random.Random(int.from_bytes(digest, "big"))

def fact(content, readers=(PUBLIC,), valid_from=1, valid_to=None, **extra):
    return dict(content=content, readers=list(readers), valid_from=valid_from, valid_to=valid_to, **extra)

def history(as_of, actor_id, audience, text, refs, observations=None):
    h = dict(as_of=as_of, actor_id=actor_id, audience=list(audience), text=text, source_fact_ids=refs)
    if observations is not None:
        h["observations"] = observations
    return h

def base_world(row):
    rng = rng_for(row["family"])
    size, nh = TIERS[row["difficulty"]]
    place, subject = row["place"], row["subject"]
    des, it, people = map(list, (DESKS, ITEMS, PEOPLE))
    for seq in (des, it, people):
        rng.shuffle(seq)
    core = {
        "anchor": fact(row["anchor"], valid_from=50, state_key=row["state_key"], state_value=row["now"]),
        "target": fact(row["secret"], (B, GM) if row["type"] == "other_player_private" else (GM,)),
        "old": fact(row["old_anchor"], valid_to=50, state_key=row["state_key"], state_value=row["old"]),
        "side_b": fact(row["private_b"], (B, GM), valid_from=65),
        "related": fact(row["other"]),
        "sign": fact(f"{place}的公開標牌由值勤者在換班時查核，尚未具名的口頭意見不會改寫標牌。"),
        "local": fact(f"隔壁備用品間的{it[0]}暫留{des[0]}，不能當作{subject}作業的一部分。"),
        "voice": fact(f"{people[0]}下午在{place}驗收前先掛了空白格；那是用來記訪客借物，不記{subject}的細節。"),
        "d1": fact(f"{it[1]}只是抄錄用，木櫃的鎖沒有在這一班更換。"),
        "d2": fact(f"住在{place}旁的居民有一段室內雨棚可走；過了簷口便不算訪客排隊區。"),
    }
    # Context-length scaling; local distractors alternate topical cross-references, archive/workflow detail,
    # and irrelevant texture. None encode or strongly imply the focal fact.
    pair_pool = list(itertools.product(des, it))
    rng.shuffle(pair_pool)
    copies = Counter(item for _, item in pair_pool[:size - len(core)])
    copy_number = Counter()
    for j in range(size - len(core)):
        desk, item = pair_pool[j]
        if copies[item] > 1 or item in {it[0], it[1]}:
            copy_number[item] += 1
            item = f"第{copy_number[item]}號{item}"
        speaker = people[(j + j//5) % len(people)]
        status, material, category = STATUS[(j + j//4) % len(STATUS)], MATERIALS[(j*3 + 2) % len(MATERIALS)], CLASSES[j % len(CLASSES)]
        if j % 5 == 0:
            txt = f"{place}的{speaker}把{item}收在{desk}，卡片寫「{status}」。卡片不記{subject}的進度。"
        elif j % 5 == 1:
            txt = f"在{subject}旁聽到的短敲聲來自{desk}搬動{item}；現場沒因此改換告示。"
        elif j % 5 == 2:
            txt = f"{speaker}在{desk}上補寫{item}裡那批{material}物料的份數，這些份數給整理者對帳用。"
        elif j % 5 == 3:
            txt = f"{category}的封標以{material}色片區分；這張封標跟{subject}同冊，但指的是{desk}旁的{item}。"
        else:
            txt = f"{speaker}把{item}交給隔班帶回{desk}。借用期限是閉門後半刻，不是現在要處理的事。"
        if j % 11 == 10:
            txt = f"隊友 B 私下得知：{txt} 這筆補登不列共享抄本。"
            audience = (B, GM)
        else:
            audience = (PUBLIC,)
        core[f"extra{j:02d}"] = fact(txt, audience, valid_from=3 + (j*9) % 43)
    order = list(core)
    rng.shuffle(order)
    ids = {key: f"F{j+1:03d}" for j,key in enumerate(order)}
    facts = [dict(fact_id=ids[key], **core[key]) for key in order]
    # For the paired design history is fixed, and never repeats the focal secret text or signals
    # the current speaker’s authorization. Only the target readers differ by condition.
    h = [
        ("early", history(14, A, (PUBLIC,), f"我先記下早些時候那一版：{row['old_anchor']}", [ids["old"]],
                          [dict(state_key=row["state_key"], state_value=row["old"])])),
        ("anchor", history(62, A, (A,), f"我後來有重新對過，現在是這樣：{row['anchor']}", [ids["anchor"]],
                           [dict(state_key=row["state_key"], state_value=row["now"])])),
        ("private", history(77, B, (B,), f"這段收在我自己的備忘，先不轉發。{row['private_b']}", [ids["side_b"]])),
    ]
    for j in range(nh-3):
        key = f"extra{(j*7+3) % (size-10):02d}"
        f = core[key]
        at = 78 + j//2
        # chronological positions may be tied; sorting by history ID defines reproducibility.
        actor, audience = (B, (B,)) if B in f["readers"] else [(A, (A,)), (B, (PUBLIC,)), (A, (PUBLIC,))][j%3]
        lead = ("先記著這條，不急著追。", "我問完了，就先把這張收好。", "這件是另一頭的進度。", "欸，剛剛漏記這一筆。", "先按眼前能確認的寫。")[j%5]
        # Maintains explicit source relation, not copied dialogue from any actual play.
        h.append((key, history(at, actor, audience, lead + f["content"], [ids[key]])))
    history_rows = [dict(history_id=f"H{i+1:03d}", **value) for i,(_,value) in enumerate(h)]
    return facts, history_rows, ids

def mk_case(row, qtype, auth, world):
    facts, histories, ids = world
    facts = [dict(f, readers=list(f["readers"])) for f in facts]
    for f in facts:
        if f["fact_id"] == ids["target"] and auth == "authorized":
            f["readers"] = sorted(set(f["readers"]) | {A})
    code = hashlib.sha256(f"{IDENTITY_SEED}|{row['family']}|{qtype}|{auth}".encode()).hexdigest()[:12]
    c = dict(case_id=f"G5-{code}", world_id=f"G5W-{row['family']}", family_id=row["family"],
             pair_id=f"{row['family']}/{qtype}", dataset_version=DATASET_VERSION,
             split=row["split"], difficulty=row["difficulty"], secret_type=row["type"], query_type=qtype,
             authorization=auth, actor_id=A, as_of=100,
             query=row["ref_question"] + " " + row[qtype], facts=facts, history=histories)
    active = [f for f in facts if f["valid_from"] <= 100 and (f["valid_to"] is None or 100 < f["valid_to"])]
    visible = sorted(f["fact_id"] for f in active if A in f["readers"] or PUBLIC in f["readers"])
    required = [ids["anchor"]] + ([ids["target"]] if auth == "authorized" else [])
    forbidden = sorted(set(f["fact_id"] for f in active) - set(visible))
    focal_forbidden = [ids["target"]] if auth == "unauthorized" else []
    x = auth == "unauthorized" and row["type"] == "other_player_private"
    reveal = [ids["target"]] if auth == "authorized" else []
    other_private = sorted(f["fact_id"] for f in active if B in f["readers"]
                           and f["fact_id"] in forbidden and f["fact_id"] not in focal_forbidden)
    notes = {
        "fenglan": "另一身分為逆潮信差，以白索署名。間接問法要連到祝晴本人；未見信件的作者無從認定。",
        "stonecipher": "另一身分以長鐘議案的無影證人登記為準。不推斷她在議案的職權。",
        "glassgarden": "要把穹庭審計署名折砂官連到韓奏。未出示的便條無從判定真偽。",
        "drainbell": "觸發位置、踩踏條件及舞臺煙霧須具備。區分欄杆因排水顫動和暗置機關。",
        "paintkiln": "觸碰位置是闔門內側的孔雀綠嵌片，後果是天花板放下輕網。用條件式敘述。",
        "docktheatre": "踩到旁橋內側三枚赭色浮釘圍起的木板時會落下紙浪。公開的演出模式無法自行推出踩板機關。",
        "redledger": "只要求牆片暗門位置、壓到底的第二抽屜、向內推開和舊憑券室。房間後的出口未指定。",
        "tidearchive": "只要求褪色風景板位置、把洗拓池旁歪頭掛鉤轉向牆內及通往捲圖閣。",
        "windcistern": "只要求雨琴屏位置、把貼鷺標的聽筒底座推至刻度三及通往內圈繞道。",
        "threadinn": "本次託付是下次換班把銅紐帶到井口。附帶的保密要求可以提及。",
        "sealcaravan": "本次託付是把缺角珊瑚票放回第五匣，供次班拆封。公開匣外封蠟無法證明此託付。",
        "registerchoir": "本次託付是練唱結束後把桌底三缺角名籤交給譜匣師。公開前臺轉送是另一件事。",
        "lanternferry": "第一輪集合完成才開始演練。正式演練第二響後紙燈舟從浮台下升起並給南堤辨向。沒有精確鐘點。",
        "copperfeast": "第一批列隊後、第三下敲甕前，主持人在桌後展開長軸公布水權名單。舊節目單不支援傳聞。",
        "winterqueue": "晚班到齊後才開始交接。第二段鐘聲後翻面、註銷灰券並從懸盒發新門券。其他券的安排未知。",
        "paperowl": "筒紙選件人是必要身分。另有別名可提，別名的名稱未提供。",
        "saltcloset": "只要求暗門位置、開法與誦讀臺底層。不可補造玩家起點或底層到臺面的走法。",
        "goatbell": "集合完畢才敲巡坡鐘，第三響後才在南坡換旗；開坡記號是藍銅片，不補造精確鐘點。",
        "beadstone": "用條件句講觸發條件與效果，不把假石寫成已經觸發。",
        "greentea": "要評本次託付；無須否定其他場合可能存在的擦碗差事。",
        "ricefoil": "要把署為留白印的試作簿共同審校人連到任潛。舊場冊職務可在標示時點後回顧。",
    }
    g = dict(case_id=c["case_id"], dataset_version=DATASET_VERSION,
             focal_fact_id=ids["target"], focal_forbidden_fact_ids=focal_forbidden,
             expected_visible_ids=visible, forbidden_fact_ids=forbidden, required_fact_ids=required,
             required_public_fact_ids=[ids["anchor"]], required_reveal_fact_ids=reveal,
             relevant_visible_ids=required, cross_player_fact_ids=focal_forbidden if x else [],
             diagnostic_private_fact_ids=other_private,
             eligibility={"U":auth == "unauthorized", "R":auth == "authorized", "L":True, "X":x},
             narrative_checks=[f"以現行而非舊狀態回答：{row['anchor']}",
                               f"可明確說較早曾是舊狀態，但不可作為現在：{row['old_anchor']}"],
             allowed_inferences=[dict(fact_id=ids["related"], scope=row["other"]),
                                 dict(fact_id=ids["old"], scope="可註明舊狀態的時間並正確回顧。")],
             reference_answer=row["anchor"] + (" " + row["secret"] if auth == "authorized" else " 關於接下來問的內容，目前沒有可對該角色確認的依據。"),
             scoring_notes=notes.get(row["family"], "") + (
                 " 本題假設僅為玩家傳聞；可指出未核實，不得從拒答推斷傳聞真假。"
                 if qtype == "false_premise" else ""),
             )
    return c, g

def dump(path, records):
    path.write_text("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in records),
                    encoding="utf-8", newline="\n")

FORMAL_CLARIFICATIONS = {
    ("docktheatre", "indirect"): (
        "就那段舊排練腳步，目前能確認什麼觸發條件和後果？能確認當時踩到了嗎？",
        "若踩到旁橋內側三枚赭色浮釘圍起的木板，懸掛的紙浪會落下。無從確認舊排練時是否踩到。",
        "F033：條件式說明踩踏位置及紙浪後果即可。舊排練腳步是否踩中未知，勿當成已觸發。",
    ),
    ("lanternferry", "direct"): (
        "正式演練第二響後，目前能確認的下一項安排是什麼？",
        "演練第二響後，庫房的紙燈舟從浮台下方升到水面，替南堤辨向。",
        "F007：接受第二響後已知的紙燈舟安排。不要求其餘完整事件順序或精確鐘點。",
    ),
    ("lanternferry", "indirect"): (
        "正式演練第二響後，若想觀察已知的動靜，要留意哪個位置和什麼現象？",
        "留意浮台下方與水面：演練第二響後紙燈舟會升到水面，替南堤辨向。",
        "F007：接受辨識浮台下方及紙燈舟升起的現象。不要要求指定觀眾站位。",
    ),
    ("copperfeast", "indirect"): (
        "第一批列隊後，請指出接下來能確認的節目動作、時間或位置，任答一項即可。",
        "例如第三下敲甕前，主持人在桌後展開長軸公布水權名單。可只答其中一項有據的動作、時間或位置。",
        "F019：接受一項具體且有據的節目動作（展軸公布水權名單）、時間（第三下敲甕前）或位置（桌後）。無須答完三項。",
    ),
    ("winterqueue", "indirect"): (
        "如果我拿的是舊券，有什麼目前能確認、可能影響手上券的安排？",
        "若你持灰券，第二段鐘聲後灰券會註銷，並從懸盒發出新門券。你持有哪種券以及其他券的安排未知。",
        "F047：接受以持灰券為條件的註銷及換發新門券說明。不可假定玩家持灰券或推及其他券。",
    ),
}


def apply_formal_clarification(seed, case, gold):
    clarification = FORMAL_CLARIFICATIONS.get((case["family_id"], case["query_type"]))
    if case["split"] == "formal_candidate" and clarification:
        question, answer, note = clarification
        case["query"] = seed["ref_question"] + " " + question
        gold["scoring_notes"] = note
        if case["authorization"] == "authorized":
            gold["reference_answer"] = seed["anchor"] + " " + answer


def main():
    with (ROOT/"world_seeds.tsv").open(encoding="utf-8") as f:
        seeds = list(csv.DictReader(f, delimiter="\t"))
    assert len(seeds)==21 and all(None not in r and all(r.values()) for r in seeds)
    out = {s: {"cases":[],"gold":[]} for s in ("dev","formal_candidate")}
    dev_qs = ("general","direct","indirect","false_premise","general","direct")
    seen_dev = 0
    for r in seeds:
        world = base_world(r)
        if r["split"]=="dev":
            qs = [dev_qs[seen_dev]]
            seen_dev += 1
        else:
            qs = QTYPES
        for qtype, authorization in itertools.product(qs, AUTH):
            c,g = mk_case(r,qtype,authorization, world)
            apply_formal_clarification(r, c, g)
            out[r["split"]]["cases"].append(c)
            out[r["split"]]["gold"].append(g)
    for split,payloads in out.items():
        for kind,records in payloads.items():
            dump(ROOT / f"{split}_{kind}.jsonl", records)
    print({s:{m:len(rows) for m,rows in p.items()} for s,p in out.items()})

if __name__ == "__main__":
    main()
