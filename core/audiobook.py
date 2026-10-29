"""听书：云盘有声书的识别、排序、扫描与播放进度

约定目录结构（最多 3 层）：
    有声书/书名 - 作者/[卷名/]音频文件

集数识别按可信度分级，避免把书名里的数字当成集数
（例如「三体3 死神永生 第01集」要取 01，不能取 3）。
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from .tianyi import TianyiClient, TianyiError

AUDIO_EXT = (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac", ".wma", ".ape", ".opus")
# 这些格式浏览器解不了（Chrome/Safari/安卓都不支持），能列出来但放不了
UNSUPPORTED_EXT = (".wma", ".ape", ".amr", ".mid", ".midi")

MAX_PAGES = 40          # 单目录最多翻 40 页，防止异常死循环
PAGE_SIZE = 500         # 实测服务端接受 500，469 集一次就能取全
MAX_VOLUME_DEPTH = 1    # 书下面允许一层卷目录

# ============================ 中文数字 ============================

_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}


def cn_to_int(s: str) -> int:
    """十二 → 12，一百零五 → 105，两千零一 → 2001"""
    if not s:
        return -1
    total = section = number = 0
    for ch in s:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            section += (number or 1) * _CN_UNIT[ch]
            number = 0
        elif ch == "万":
            total += (section + number) * 10000
            section = number = 0
        else:
            return -1
    return total + section + number


# ============================ 集数识别 ============================

_UNIT_WORDS = "集章回讲节期课话篇段季幕"
_CN_NUM = "零〇一二三四五六七八九十百千万两"

# ⓪ 卷标记：第一部 / 卷二 / 第3季（与集号组合成多段，避免多卷混平铺时排序错乱）
RE_VOLUME = re.compile(rf"第\s*([0-9]+|[{_CN_NUM}]+)\s*[部卷季册]")
# ① 第N集 / 第 N 章 / 第十二回
RE_DI = re.compile(rf"第\s*([0-9]+|[{_CN_NUM}]+)\s*[{_UNIT_WORDS}]")
# ② 12集 / 03章（无"第"字）
RE_UNIT = re.compile(rf"(?<![0-9A-Za-z])([0-9]{{1,4}})\s*[{_UNIT_WORDS}]")
# ③ S01E02
RE_SXXEXX = re.compile(r"(?i)\bS(\d{1,2})\s*[._\-\s]?\s*E[P]?(\d{1,4})\b")
# ④ Chapter 5 / Part 3 / Vol 2 / Track 07
RE_EN_WORD = re.compile(
    r"(?i)\b(?:chapter|chap|ch|part|track|vol|volume|book|lesson|episode)\s*[._\-]?\s*(\d{1,4})\b")
# ⑤ EP01 / E12
RE_EP = re.compile(r"(?i)(?<![A-Za-z])E[P]?\s*[._\-]?\s*(\d{1,4})(?![0-9])")
# ⑥ 紧凑多段：1-01 / 01_02 / 2.3
RE_MULTI = re.compile(r"(?<![0-9])(\d{1,3})[-_.](\d{1,4})(?![0-9])")
# ⑦ 前导零：001 / 0007
RE_ZERO = re.compile(r"(?<![0-9])(0\d{1,4})(?![0-9])")
# ⑧ 开头：括号包住的数字（【12】终局 / (3) 归来），或数字后跟分隔符（1.xxx / 12 破壁人）
RE_HEAD_BRACKET = re.compile(r"^\s*[\[\(（【]\s*(\d{1,4})\s*[\]\)）】]")
RE_HEAD = re.compile(r"^\s*(\d{1,4})\s*[.、,，_\-\s）)]")
# ⑨ 结尾裸数字：xxx 12 / xxx(12)
RE_TAIL = re.compile(r"[\s._\-、,，（(【\[]\s*(\d{1,4})\s*[\]\)）】]?\s*$")

# 特殊词：不靠数字也能归位
_PRE_WORDS = ("序言", "序章", "序", "前言", "楔子", "引子", "开篇", "导言",
              "开场", "引言", "出版说明", "内容简介")
_POST_WORDS = ("后记", "尾声", "附录", "大结局", "番外", "结语", "彩蛋",
               "致谢", "完结", "终章", "the end", "ending")


def _is_audio(name: str) -> bool:
    return name.lower().endswith(AUDIO_EXT)


def extract_index(name: str) -> tuple[tuple[int, ...] | None, str]:
    """从文件名提取序号。返回 (序号元组, 识别方式)，识别不出返回 (None, "")

    多段序号（1-01、S01E02）返回多元组，按层级比较，
    所以「第二卷第01集」不会排在「第一卷第99集」前面。
    """
    s = name.strip()

    def _num(raw: str) -> int:
        return int(raw) if raw.isdigit() else cn_to_int(raw)

    # 先看有没有卷标记（第一部/卷二/第3季），有就让集号带上它
    vol = None
    mv = RE_VOLUME.search(s)
    if mv:
        v = _num(mv.group(1))
        if v >= 0:
            vol = v

    def wrap(k: tuple) -> tuple:
        return (vol,) + k if vol is not None else k

    # 文件最前面的编号（1. / 10、/ 12 空格）最可信：
    # 「10.旁說·第一回作者註」应该取 10，不能被标题里的「第一回」带跑。
    # 限制 1~3 位，避免把「1984 第一部 第1章」的年份当集号。
    m = re.match(r"^\s*(\d{1,3})\s*[.、,，_\-\s]", s)
    if m:
        return wrap((int(m.group(1)),)), "开头编号"

    m = RE_DI.search(s)
    if m:
        n = _num(m.group(1))
        if n >= 0:
            return wrap((n,)), "第N集"

    m = RE_UNIT.search(s)
    if m:
        return wrap((int(m.group(1)),)), "N集"

    m = RE_SXXEXX.search(s)
    if m:
        return wrap((int(m.group(1)), int(m.group(2)))), "SxxExx"

    m = RE_EN_WORD.search(s)
    if m:
        return wrap((int(m.group(1)),)), "英文序号"

    m = RE_EP.search(s)
    if m:
        return wrap((int(m.group(1)),)), "EP"

    m = RE_MULTI.search(s)
    if m:
        return (int(m.group(1)), int(m.group(2))), "多段"

    m = RE_ZERO.search(s)
    if m:
        return wrap((int(m.group(1)),)), "前导零"

    m = RE_HEAD_BRACKET.search(s)
    if m:
        return wrap((int(m.group(1)),)), "开头数字"

    m = RE_HEAD.search(s)
    if m:
        return wrap((int(m.group(1)),)), "开头数字"

    m = RE_TAIL.search(s)
    if m:
        return wrap((int(m.group(1)),)), "结尾数字"

    # 整个名字就是一个数字（如 012.mp3）
    if s.isdigit() and len(s) <= 5:
        return wrap((int(s),)), "纯数字"

    # 只有卷标记没有集号（卷目录名本身，如「第一卷 地球往事」）
    if vol is not None:
        return (vol,), "卷"

    # 纯中文数字（如「十二」）
    bare = s.strip(" ._-、,，()（）[]【】")
    if bare and all(c in _CN_DIGIT or c in _CN_UNIT or c == "万" for c in bare):
        n = cn_to_int(bare)
        if n >= 0:
            return (n,), "中文数字"

    return None, ""


def natural_key(s: str) -> tuple:
    """自然排序键：第2集排在第10集前面"""
    parts = re.split(r"(\d+)", (s or "").lower())
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts if p != "")


def order_key(name: str) -> tuple:
    """排序键：开头类(序/前言) → 有序号 → 无序号 → 结尾类(后记/番外)

    注意：只要识别出序号就按序号排，不再看特殊词。
    否则「0001 开场」「1-01 序」这类正片会被当成前言排到最前面。
    """
    idx, _ = extract_index(name)
    if idx is not None:
        return (1, idx, natural_key(name))

    low = (name or "").lower()
    head = low[:8]
    tail = low[-8:]

    if any(w in head for w in _PRE_WORDS):
        return (0, (-10 ** 9,), natural_key(name))
    if any(w in tail for w in _POST_WORDS):
        return (3, (10 ** 9,), natural_key(name))
    return (2, (10 ** 8,), natural_key(name))


# ============================ 扫描 ============================

def _list_once(client: TianyiClient, folder_id: str) -> tuple[list, list, int]:
    """翻一遍目录，返回 (子文件夹, 文件, 服务端报的总数)"""
    folders: list = []
    files: list = []
    seen: set[str] = set()
    total = 0
    dry = 0                      # 连续几页没捞到新文件

    for page in range(1, MAX_PAGES + 1):
        d = client.list_page(folder_id, page, PAGE_SIZE)
        if page == 1:
            folders = d["folders"]
            total = d["total"]

        batch = [f for f in d["files"] if f["id"] not in seen]
        for f in batch:
            seen.add(f["id"])
        files.extend(batch)

        if not d["files"]:
            break
        if not batch:
            dry += 1
            if dry >= 2:         # 服务端忽略了 pageNum，再翻也是同一页
                break
            continue
        dry = 0

        if total and len(files) >= total - len(folders):
            break                # count 含子文件夹，减掉才算对上
        if not total and len(d["files"]) < PAGE_SIZE:
            break                # 连 count 都没给，只能靠短页判断

    return folders, files, total


def list_all(client: TianyiClient, folder_id: str) -> tuple[list, list]:
    """取全目录。天翼偶尔会少返回一两条，所以对不上 count 就重拉，最多三次。

    只认 fileListAO.count（目录里真实的文件数），不看「这一页多长」。
    """
    best_folders: list = []
    best_files: list = []
    total = 0
    for _ in range(3):
        folders, files, total = _list_once(client, folder_id)
        if not best_files or len(files) > len(best_files):
            best_folders, best_files = folders, files
        if not total or len(best_files) >= total - len(best_folders):
            break
    return best_folders, best_files


# 常见姓氏：用来判断「A - B」里哪一边是人名。
# 作者的真名几乎都以姓氏开头，书名很少这样，这比看长度靠谱。
_SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚"
    "程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓"
    "牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙"
    "叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴郁胥能苍双"
    "闻莘党翟谭贡劳逄姬申扶堵冉宰郦雍却璩桑桂濮牛寿通边扈燕冀郏浦尚农"
    "温别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘"
    "匡国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空"
    "曾毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公"
    # 简体/异体与复姓首字
    "肖付闫覃冼盘麦零慕容"
)

# 出现这些词的多半是书名，不是人名
_NOT_NAME = ("全集", "有声", "小说", "广播剧", "评书", "相声", "完结", "全本",
             "精校", "合集", "外传", "演义", "讲坛", "读书", "原著")
_TITLE_TAIL = ("传", "记", "录", "志", "史", "集", "演义", "传奇", "往事", "风云")

_SEPS = (" - ", " – ", " — ", "—", "－", " _ ", "  ")


def _looks_like_author(s: str) -> bool:
    s = (s or "").strip()
    if not (2 <= len(s) <= 4):
        return False
    if any(ch.isdigit() for ch in s):
        return False
    low = s.lower()
    if any(w in low for w in _NOT_NAME):
        return False
    if any(s.endswith(w) for w in _TITLE_TAIL):
        return False
    return s[0] in _SURNAMES


def _split_once(raw: str) -> tuple[str, str]:
    """按第一个分隔符切成两半，切不开返回两个空串"""
    raw = (raw or "").strip()
    for sep in _SEPS:
        if sep in raw:
            a, b = raw.split(sep, 1)
            if a.strip() and b.strip():
                return a.strip(), b.strip()
    return "", ""


def author_hints(raws) -> set:
    """从整批文件夹名里猜出哪些词是「作者」。

    天翼里很多是她自己起的名，姓氏表兜不住（比如「盗辛」）。
    但作者名会在同一批里反复出现在分隔符左边 —— 统计一下就知道：
    「盗辛 - 屌丝道士」「盗辛 - 废土巫师」…… 「盗辛」出现 11 次，就是作者。
    """
    from collections import Counter
    cnt = Counter()
    for raw in raws:
        left, right = _split_once(raw)
        if left and 2 <= len(left) <= 5 and not any(ch.isdigit() for ch in left):
            cnt[left] += 1
    return {k for k, n in cnt.items() if n >= 2}


def split_name(raw: str, order: str = "auto", hints: set | None = None) -> tuple[str, str]:
    """把文件夹名拆成 (书名, 作者)。两种顺序都认。

    order:
      auto   —— 看哪边像人名（常见姓氏开头、2~4 字、不含书名常见词）
      title  —— 明确书名在前：「三体 - 刘慈欣」
      author —— 明确作者在前：「刘慈欣 - 三体」
    hints  —— 整批统计出来的作者名集合，auto 模式下优先信它
    """
    raw = (raw or "").strip()

    left, right = _split_once(raw)

    if not left:
        # 三体（刘慈欣） / 三体(作者:刘慈欣)
        m = re.match(r"^(.+?)\s*[（(]\s*(?:作者|著|播讲|演播|主播)?\s*[:：]?\s*([^）)]+)[）)]\s*$", raw)
        if m:
            return m.group(1).strip(), m.group(2).strip()
        # [刘慈欣] 三体
        m = re.match(r"^[\[【]\s*([^\]】]{2,20})\s*[\]】]\s*(.+)$", raw)
        if m:
            return m.group(2).strip(), m.group(1).strip()
        return raw, ""

    if order == "author":
        return right, left
    if order == "title":
        return left, right

    # 同一批目录里反复出现的那个词，八成是作者
    if hints:
        li, ri = left in hints, right in hints
        if li and not ri:
            return right, left
        if ri and not li:
            return left, right

    la = _looks_like_author(left)
    ra = _looks_like_author(right)
    if ra and not la:
        return left, right          # 三体 - 刘慈欣
    if la and not ra:
        return right, left          # 刘慈欣 - 三体

    # 都不像人名：看哪边更像书名。左边短、右边长或带括号的，多半是「作者 - 书名」
    if len(left) <= 4 and (len(right) >= 6 or re.search(r"[（(\[【]", right)):
        return right, left

    # 剩下的按有声书的常见习惯：书名 - 作者
    return left, right


# 无卷章节的卷键：排在任何真实分卷之前
VKEY_ROOT = (-1, (-10 ** 9,), ())

# 解说、花絮类内容：同一集号时排在正片后面（听完正文再听讲解）
_EXTRA_WORDS = ("旁说", "旁說", "撷秀", "擷秀", "片花", "花絮", "预告", "預告",
                "彩蛋", "解说", "解說", "导读", "導讀", "注释", "註釋")


def _is_extra(title: str) -> bool:
    low = (title or "").lower()
    return any(w in low for w in _EXTRA_WORDS)


def _sort_key(title: str) -> tuple:
    """(类别, 集号, 正片0/解说1, 自然排序)"""
    cls, num, nat = order_key(title)
    return (cls, num, 1 if _is_extra(title) else 0, nat)


# 上下集/前后篇：同一回分成两段时用
_PART_RE = re.compile(r"[\s._\-]*(上|中|下|前|后|续)[\s集回章篇部]*$")


def _short_title(title: str) -> str:
    """去掉文件名里重复的书名前缀。

    「蔣勳 - 細說紅樓夢 - 第01回上」→「第01回上」
    只在切掉后仍能认出集数时才切，避免把「第01集 - 科学边界」切成「科学边界」。
    """
    if " - " not in title:
        return title
    head, tail = title.rsplit(" - ", 1)
    if len(head) < 4:
        return title
    idx, _ = extract_index(tail)
    if idx is None:
        return title
    return tail.strip()


def _make_chapter(f: dict, volume: str, vkey: tuple) -> dict:
    name = f["name"]
    full = name.rsplit(".", 1)[0] if "." in name else name
    title = _short_title(full)
    idx, how = extract_index(title)

    mp = _PART_RE.search(title)
    part = mp.group(1) if mp else ""

    num = None
    if idx:
        num = idx[0] if len(idx) == 1 else idx[-1]
    label = ""
    if num is not None:
        label = str(num) + part
    elif part:
        label = part

    return {
        "id": f["id"],
        "name": title,
        "full": full,
        "size": f.get("size", 0),
        "mtime": f.get("mtime", ""),
        "volume": volume,
        "num": num,
        "label": label,
        "part": part,
        "multi": list(idx) if idx else None,
        "how": how,
        # 集号 → 正片/解说 → 文件名。role 必须排在文件名自然序之前，
        # 否则「1.旁說…」会靠文件名抢到正文前面。
        "key": _sort_key(title),
        "vkey": vkey,
        "playable": not name.lower().endswith(UNSUPPORTED_EXT),
    }


def count_book(client: TianyiClient, folder_id: str, _depth: int = 0) -> int:
    """数一本书有多少集，子卷里的也算上。

    不翻页数文件，而是用服务端给的 count（目录条目总数）减去子文件夹数。
    翻页接口偶尔会少返回一两条，count 却一直准 —— 拿它算，刷新几次数字都不会跳。
    """
    d = client.list_page(folder_id, 1, PAGE_SIZE)
    total = int(d["total"] or 0)
    folders = d["folders"]
    files = d["files"]
    # 目录里的文件条数（count 含子文件夹，减掉）
    n = max(0, total - len(folders))
    # 封面图、说明.txt 这些不算集数
    n = max(0, n - sum(1 for f in files if not _is_audio(f["name"])))

    if _depth < 2:
        for v in folders:
            try:
                n += count_book(client, v["id"], _depth + 1)
            except TianyiError:
                continue
    return n


def fill_counts(client: TianyiClient, books: list, workers: int = 6) -> list:
    """并发把 books 里每本书的 count 填上，就地改，返回没数成的 id"""
    todo = [b for b in books if b.get("id")]
    if not todo:
        return []
    from concurrent.futures import ThreadPoolExecutor

    def one(b):
        try:
            return b["id"], count_book(client, b["id"]), ""
        except Exception as e:                       # 单本失败不影响别的
            return b["id"], 0, str(e)

    failed = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for bid, n, err in ex.map(one, todo):
            if n:
                for b in todo:
                    if b["id"] == bid:
                        b["count"] = n
                        break
            else:
                failed.append({"id": bid, "why": err or "没数到音频"})
    return failed


def scan_book(client: TianyiClient, folder_id: str, as_single: bool = False) -> dict:
    """扫描一本书的章节。

    as_single=True 时把这个目录本身当成一整本书（根目录散装音频的情况）。
    """
    folders, files = list_all(client, folder_id)
    chapters: list[dict] = []

    if as_single:
        for f in files:
            if _is_audio(f["name"]):
                chapters.append(_make_chapter(f, "", VKEY_ROOT))
    else:
        # 书目录下直属的音频（有卷目录时，这部分算"卷外"，排在最前）
        for f in files:
            if _is_audio(f["name"]):
                chapters.append(_make_chapter(f, "", VKEY_ROOT))

        if folders:
            volumes = sorted(folders, key=lambda x: order_key(x["name"]))
            for v in volumes:
                try:
                    _, sub = list_all(client, v["id"])
                except TianyiError:
                    continue
                vkey = order_key(v["name"])     # 完整键：卷号 + 特殊词
                for f in sub:
                    if _is_audio(f["name"]):
                        chapters.append(_make_chapter(f, v["name"], vkey))

    chapters.sort(key=lambda c: (c["vkey"], c["key"]))
    for i, c in enumerate(chapters):
        c["index"] = i

    return {
        "count": len(chapters),
        "chapters": chapters,
        "has_volume": any(c["volume"] for c in chapters),
    }


def scan_shelf(client: TianyiClient, root_name: str, name_order: str = "auto") -> dict:
    """书架：只列书（子文件夹），不进去数集数，保证秒开"""
    root_id = client.find_folder(root_name, "-11")
    if not root_id:
        return {"root": root_name, "exists": False, "books": []}

    folders, files = list_all(client, root_id)
    hints = author_hints([f["name"] for f in folders]) if name_order == "auto" else set()
    books = []
    for f in folders:
        name, author = split_name(f["name"], name_order, hints)
        books.append({
            "id": f["id"],
            "raw": f["name"],
            "name": name,
            "author": author,
            "single": False,
        })

    loose = [f for f in files if _is_audio(f["name"])]
    if loose:
        books.append({
            "id": root_id,
            "raw": "未分类",
            "name": "未分类",
            "author": "",
            "single": True,
            "count": len(loose),
        })

    books.sort(key=lambda b: natural_key(b["name"]))
    return {"root": root_name, "root_id": root_id, "exists": True, "books": books}


# ============================ 播放进度（服务端，多设备同步） ============================

class Progress:
    """progress.json：{book_id: {index, file_id, pos, dur, count, updated}}"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return
        try:
            d = json.loads(self.path.read_text("utf-8"))
            if isinstance(d, dict):
                self._data = d
        except Exception:
            self._data = {}

    def _flush(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False), "utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    def all(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}

    def get(self, book_id: str) -> dict:
        with self._lock:
            return dict(self._data.get(book_id) or {})

    def set(self, book_id: str, **kw) -> dict:
        with self._lock:
            cur = dict(self._data.get(book_id) or {})
            cur.update({k: v for k, v in kw.items() if v is not None})
            cur["updated"] = int(__import__("time").time())
            self._data[book_id] = cur
            self._flush()
            return dict(cur)

    def clear(self, book_id: str) -> bool:
        with self._lock:
            if book_id in self._data:
                self._data.pop(book_id)
                self._flush()
                return True
            return False
