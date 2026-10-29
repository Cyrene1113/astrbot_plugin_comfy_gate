# -*- coding: utf-8 -*-
"""astrbot_plugin_comfy_gate — ComfyUI 出图闸门

把 comfyui-pipeline skill 的固定流程封装成一个 LLM 工具 `comfyui_render`：
群友在群里用自然语言点单，管理员在私聊里可指定工作流精细出图，
工具内部自动完成「探活 → 选工作流 → 改提示词 → 出图 → 交付」。

设计要点（汐小姐要求，随迭代更新）：
- 自然语言调动：只暴露 prompt / negative / workflow 三个参数，其余全部内部固定。
- 只跑固定流程：不改基准工作流，复制临时文件上改，出图后即删。
- 群聊：任何群友可用，固定走快速出图版（fast_workflow 配置）。
- 非群聊：仅管理员可出图；非管理员调用返回 noop（不执行任何操作），
  由 LLM 按工具说明当作调用未发生、自然继续对话（D1 拍板，2026-10-24）。
- 工作流指定：实时目录模糊匹配 + 外置 data/workflow_config.json（D3）。
- 提示词：槽位模板 + 角色 LoRA 身份槽让位（D5）；受理回执区分启动/队列状态（D6）。
- 权限隔离：不暴露任何 shell / python 能力，全局管理员限制不受影响。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path

from astrbot.api import logger, llm_tool
from astrbot.api.event import AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_workspaces_path
from astrbot.core.workspace import normalize_umo_for_workspace

# ── 复用 skill 共用库（只读复用，不复制逻辑） ──────────────────────────
# P8d：skill 脚本目录外置到 data/workflow_config.json 的 skill_scripts_dir，
# 目录缺失时抛出带指引的 ImportError，不再报裸 ModuleNotFoundError
_PLUGIN_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_DEFAULT_SKILL_SCRIPTS = r"C:\Users\14476\.astrbot\data\skills\comfyui-pipeline\scripts"


def _read_workflow_config_raw() -> dict:
    """import 前的轻量配置读取（不做默认合并，读不到返回空 dict）。"""
    try:
        with open(
            os.path.join(_PLUGIN_DATA_DIR, "workflow_config.json"), "r", encoding="utf-8-sig"
        ) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_SKILL_SCRIPTS = str(
    _read_workflow_config_raw().get("skill_scripts_dir") or _DEFAULT_SKILL_SCRIPTS
).strip()
if not os.path.isdir(_SKILL_SCRIPTS):
    raise ImportError(
        f"[astrbot_plugin_comfy_gate] comfyui-pipeline skill 脚本目录不存在：{_SKILL_SCRIPTS}；"
        "请在 data/workflow_config.json 的 skill_scripts_dir 填写正确路径"
    )
if _SKILL_SCRIPTS not in sys.path:
    sys.path.insert(0, _SKILL_SCRIPTS)

import comfy_common as cc  # noqa: E402
import start_comfyui as sc  # noqa: E402

# ── 负面词/正向词均由工作流本体承载（汐小姐要求：改工作流，不在插件做运行时判定） ──
# ── 固定工作流（汐小姐要求：群聊出图定位轻便娱乐，固定只用快速出图版，绝对路径写死） ──
# 注：仅作动态解析失败时的过渡兜底，群聊正常走 data/workflow_config.json 的 fast_workflow 实时匹配
_FAST_WF_PATH = r"F:\ComfyUI-aki\ComfyUI-aki-v3.2\ComfyUI\user\default\workflows\空白基准_wail_快速出图V1.json"

# ── 工作流配置（【2026-10-24 拍板 D3】指定方式去硬编码：目录实时模糊匹配 + 外置配置文件） ──
# 工作流改名/增删/挪目录只改 data/workflow_config.json，不动代码；aliases 仅用于
# 「口语名不是任何文件名子串」的场合，值填目标文件名子串，可空。
_WF_DIR = r"F:\ComfyUI-aki\ComfyUI-aki-v3.2\ComfyUI\user\default\workflows"
_WORKFLOW_CONFIG_PATH = os.path.join(_PLUGIN_DATA_DIR, "workflow_config.json")
_DEFAULT_WORKFLOW_CONFIG = {
    "workflow_dir": _WF_DIR,
    "skill_scripts_dir": _DEFAULT_SKILL_SCRIPTS,
    "private_default": "高清修复",
    "fast_workflow": "快速出图",
    "max_concurrent": 2,  # P4：全局同时出图上限（含管理员）
    "cooldown_seconds": 30,  # P4：每会话冷却秒数（仅非管理员）
    "aliases": {},
}


def _load_workflow_config() -> dict:
    """加载工作流配置：优先外置 JSON，缺项用内置默认补齐，读不到整体退回默认。"""
    cfg = dict(_DEFAULT_WORKFLOW_CONFIG)
    cfg["aliases"] = dict(_DEFAULT_WORKFLOW_CONFIG["aliases"])
    try:
        with open(_WORKFLOW_CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key in ("workflow_dir", "private_default", "fast_workflow", "skill_scripts_dir"):
                if isinstance(data.get(key), str) and data[key].strip():
                    cfg[key] = data[key].strip()
            for key in ("max_concurrent", "cooldown_seconds"):
                try:
                    val = int(data.get(key))
                    if val >= 0:
                        cfg[key] = val
                except (TypeError, ValueError):
                    pass
            aliases = data.get("aliases")
            if isinstance(aliases, dict):
                cfg["aliases"] = {
                    str(k): str(v)
                    for k, v in aliases.items()
                    if str(k).strip() and str(v).strip()
                }
    except Exception as exc:
        logger.warning(f"工作流配置读取失败，退回内置默认：{exc}")
    return cfg

# ── 中文提示词自动翻译（固定流程：用当前 LLM provider 转英文 danbooru tag） ──
# S1（2026-10-24 拍板）：槽位模板与工具 docstring 保持同一份，约束翻译输出顺序；
# L1'（第七轮拍板）：槽位二分——自由槽积极补全；身份槽仅在命中注册 LoRA 角色时让位，
# 命中判断由插件侧 _match_role_lora 在翻译前确定性完成，导向两种翻译指令（附加说明动态拼接）。
_TRANSLATE_SYSTEM = (
    "你是 Stable Diffusion / ComfyUI 的提示词翻译器。"
    "把用户的中文画面描述翻译成英文 Danbooru tag 列表，半角逗号分隔。"
    "槽位顺序（严格按此排序输出）："
    "人数 → 角色 → 发型发色 → 服饰 → 饰品 → 表情 → 动作姿势 → 道具 → 场景背景 → 构图镜头 → 光线氛围。"
    "槽位分两类："
    "① 自由槽（人数/服饰/饰品/表情/动作姿势/道具/场景背景/构图镜头/光线氛围）："
    "原文提到的细节一个不漏保留，没提到的要积极补全（每槽 1~3 个 tag，让画面完整、有设计感）；"
    "② 身份槽（发型发色/瞳色/瞳孔特征/角色专属特征件）：默认同样积极补全；"
    "若下方有【附加说明】要求让位，则按附加说明执行。"
    "其他规则："
    "1. 不要主动补质量词/画风词（如 masterpiece、best quality），工作流基准词已自带，重复会稀释权重；"
    "但用户明确要求的画风（如水彩、厚涂）要保留；"
    "2. 角色名原样保留不翻译（角色 tag 由系统按 LoRA 映射自动处理，禁止猜测角色 tag）；"
    "3. 只使用常见 Danbooru tag（如 1girl, cat ears, holding ice cream cone）；"
    "只输出 tag 文本本身，不要句子、不要解释、不要引号、不要 Markdown 代码块。"
)

# 命中注册角色时动态附加（{_name_} 占位）：身份槽让位，显式变更走正向新特征
_TRANSLATE_ROLE_YIELD = (
    "【附加说明】本次画面命中已注册角色「{_name_}」，系统会自动挂载其专属 LoRA："
    "身份槽（发型发色/瞳色/瞳孔特征/角色专属特征件）一律让位——"
    "不要输出该角色的固有外貌 tag（其外貌由 LoRA 与工作流基准词承载，写了会和 LoRA 打架）；"
    "仅当用户明确要求改变这些特征时（如染成黑发），才输出新特征 tag；"
    "角色名「{_name_}」本身保留原样，系统需要它识别挂载。"
)


# P8a：需要触发翻译的 CJK 范围（韩文不纳入，2026-10-24 拍板）
_CJK_RANGES = (
    ("\u3400", "\u4dbf"),  # CJK 扩展 A
    ("\u4e00", "\u9fff"),  # CJK 基本区
    ("\u3040", "\u30ff"),  # 日文假名（平/片）
    ("\uf900", "\ufaff"),  # CJK 兼容表意
)


def _has_cjk(text: str) -> bool:
    """是否含需要触发翻译的 CJK 字符（扩展A/基本区/假名/兼容区；韩文不纳入，2026-10-24 拍板）。"""
    return any(lo <= ch <= hi for ch in (text or "") for lo, hi in _CJK_RANGES)

_SUBMIT_TIMEOUT = 600.0  # 出图等待上限（秒）

# 【2026-10-24 拍板 D1-B】非管理员/不可用场景的极简中性返回值：
# 插件不执行任何操作，LLM 按 docstring 约定当作调用未发生、自然继续原话题
_TOOL_NOOP = "noop：出图工具在当前会话不可用，未执行任何操作。"


def _data_dir() -> str:
    """插件数据目录（仅作兜底；出图优先走会话工作区）。"""
    try:
        d = StarTools.get_data_dir()
        if d:
            return str(d)
    except Exception:
        pass
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _session_workspace_dir(event: AstrMessageEvent) -> str | None:
    """从事件解析当前会话工作区根目录。

    受限用户只能读写 workspace 和 temp 目录；出图必须落在
    ``<workspaces>/<normalized_umo>/comfy_out`` 下，框架才能读取并发送。
    """
    try:
        umo = event.unified_msg_origin
        root = Path(get_astrbot_workspaces_path()) / normalize_umo_for_workspace(umo)
        return str(root.resolve(strict=False))
    except Exception:
        return None


def _is_admin(event: AstrMessageEvent) -> bool:
    """判断当前发送者是否为全局管理员（【汐小姐要求】私聊出图只对管理员开放）。"""
    try:
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            return bool(checker())
    except Exception:
        pass
    return False


def _resolve_workflow(keyword: str, aliases: dict | None = None, wf_dir: str | None = None):
    """按关键词解析工作流文件（动态：实时读目录模糊匹配，改名/增删无需改代码）。

    先过外置别名表（口语名 → 目标子串，值同样走模糊匹配），再对目录内
    .json 文件名做子串匹配；匹配不到返回候选清单。
    keyword 为空返回 (None, None)，由调用方决定默认工作流。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return None, None
    kw = keyword.lower()
    for alias, target in (aliases or {}).items():
        if alias and alias.lower() in kw:
            kw = str(target).strip().lower()
            break
    directory = wf_dir or _WF_DIR
    try:
        # 排除点开头的 .bak_*.json 等备份残留（实测目录里存在，会污染匹配与候选清单）
        files = [
            f
            for f in os.listdir(directory)
            if f.lower().endswith(".json") and not f.startswith(".")
        ]
    except OSError as exc:
        return None, f"工作流目录读不到：{exc}"
    if not files:
        return None, "工作流目录里没有 .json 文件"
    # 文件名子串命中（大小写不敏感）
    hits = [f for f in files if kw in f.lower().replace(".json", "")]
    if len(hits) == 1:
        return os.path.join(directory, hits[0]), None
    if len(hits) > 1:
        cands = "、".join(f.replace(".json", "") for f in sorted(hits))
        return None, f"匹配到多个工作流：{cands}，说具体一点哦～"
    # 没命中：给候选清单
    cands = "、".join(f.replace(".json", "") for f in sorted(files)[:12])
    return None, f"没找到叫「{kw}」的工作流，人家这里有的：{cands}"


def _wf_display_name(base_path: str | None) -> str:
    """工作流展示名：取文件名（不含扩展名），对动态路径永不失配。"""
    return Path(os.path.basename(base_path or _FAST_WF_PATH)).stem


def _queue_brief() -> str:
    """队列快照一句话（D6 受理回执用）；拿不到返回空串。

    审计修正（v1.7.1）：不走 cc.queue_status()（其底层 api_get_json 默认
    60s 超时），改用 5s 上界的直接查询——本函数在工具「秒回」路径上，
    极端挂起不能拖住 LLM 回复。
    """
    try:
        data = cc.api_get_json("/queue", timeout=5)
        running = data.get("queue_running") or []
        pending = data.get("queue_pending") or []
        return f"当前队列：{len(running)} 张生成中、{len(pending)} 张排队等待。"
    except Exception:
        return ""


# ── 角色 LoRA 自动匹配表（【汐小姐要求】触发词与权重必须预配置，不现场瞎编） ──────
# 独立配置文件优先，插件内置默认表兜底。加角色只改表、不动代码。
_ROLE_LORA_MAP_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "character_lora_map.json"
)
_DEFAULT_ROLE_LORA_MAP = {
    "昔涟": {
        "keywords": ["昔涟", "大昔涟", "迷迷", "德谬歌", "cyrene", "cyrene_(demiurge)", "cyrene (demiurge)"],
        "lora": "ILL_角色_大昔涟v2.safetensors",
        "trigger": "cyrene_(demiurge)",
        "weight": 0.75,
        "text_encoder_weight": 0.6,
    },
    "遐蝶": {
        "keywords": ["遐蝶", "castorice", "castorice_(honkai_star_rail)", "castorice (honkai star rail)"],
        "lora": "ILL_角色_遐蝶.safetensors",
        "trigger": "castorice_(honkai_star_rail)",
        "weight": 0.75,
        "text_encoder_weight": 0.75,
    },
    "风堇": {
        "keywords": ["风堇", "hyacine", "hyacine (honkai: star rail)"],
        "lora": "ILL_角色_风堇.safetensors",
        "trigger": "hyacine (honkai: star rail)",
        "weight": 0.75,
        "text_encoder_weight": 0.75,
    },
    "赛飞儿": {
        "keywords": ["赛飞儿", "cipher", "cipher (honkai: star rail)"],
        "lora": "ILL_角色_赛飞儿.safetensors",
        "trigger": "cipher (honkai: star rail)",
        "weight": 0.75,
        "text_encoder_weight": 0.75,
    },
}


def _load_role_lora_map() -> dict:
    """加载角色 LoRA 映射表：优先外部 JSON，读不到就退回内置默认表。"""
    try:
        with open(_ROLE_LORA_MAP_PATH, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        if isinstance(data, dict) and data:
            return data
    except Exception as exc:
        logger.warning(f"角色 LoRA 映射表读取失败，退回内置表：{exc}")
    return dict(_DEFAULT_ROLE_LORA_MAP)


def _match_role_lora(prompt: str) -> tuple | None:
    """在提示词里识别角色关键词，返回 (角色名, 映射配置)；没命中返回 None。"""
    text = (prompt or "").lower()
    if not text.strip():
        return None
    for name, cfg in _load_role_lora_map().items():
        for kw in cfg.get("keywords", []):
            if kw and kw.lower() in text:
                return name, cfg
    return None


def _norm_tag(s: str) -> str:
    """tag 归一化：剥权重括号与 :权重、压空白、转小写（L2 去重比对用）。"""
    s = re.sub(r"[()\\]", "", s or "")
    s = re.sub(r":[0-9.]+", "", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _dedup_tags(new_tags: str, existing: str) -> str:
    """L2：滤除 new_tags 中已存在于 existing 的 tag（归一化后精确匹配）。

    只治精确重复（如基准词已含 cyrene_(demiurge)/1girl/pink hair），
    不治冲突（LLM 写 black hair 而基准是 pink hair 时不会删——冲突由
    L1' 身份槽让位在源头预防）。
    复核修正（v1.6.1）：基准词按逗号拆成归一化集合后精确比对，
    旧版对整段基准词做子串匹配会误伤（long hair 被 very long hair
    误删、smile 被 smiling 误删）。
    """
    base_set = {_norm_tag(t) for t in (existing or "").split(",")}
    base_set.discard("")
    kept = []
    for raw in (new_tags or "").split(","):
        tag = raw.strip()
        if not tag:
            continue
        if _norm_tag(tag) in base_set:
            continue
        kept.append(tag)
    return ", ".join(kept)


def _strip_llm_output(text: str) -> str:
    """清理 LLM 返回的裸文本：剥外层引号/空白/Markdown 代码块包裹。

    复核修正（v1.6.1）：原先在衔接语与翻译两处逐字重复，收敛为单一实现。
    """
    out = str(text or "").strip()
    out = out.strip("` \t\r\n\"'")
    if out.lower().startswith("```"):
        out = out[3:]
        if "```" in out:
            out = out.split("```")[0]
    return out.strip("` \t\r\n\"'")


def _attach_role_lora(workflow: dict, info: dict, prompt: str, extra_match_text: str = "") -> str:
    """自动挂角色 LoRA（【汐小姐要求】触发词+权重预配置）。

    命中角色时：
    - 主正向词末尾补触发词（已存在则不重复补）；
    - 向 LoRA 堆的 lora_str 追加/更新对应角色 LoRA（保留原有画风 LoRA）。
    没命中或工作流没有 LoRA 堆时静默返回，什么都不改。
    返回命中角色名（无命中返回空串），供日志使用。

    P3：先匹配（可能已翻译的）prompt，再匹配翻译前原文 extra_match_text，
    保证映射表里的中文关键词（昔涟/德谬歌等）也能命中。
    """
    matched = _match_role_lora(prompt) or _match_role_lora(extra_match_text)
    if not matched:
        return ""
    name, cfg = matched
    stack_id = info.get("lora_stack")
    if not stack_id:
        logger.info(f"comfyui_render 命中角色「{name}」，但工作流没有 LoRA 堆，跳过挂载")
        return name

    lora_file = cfg.get("lora", "")
    trigger = cfg.get("trigger", "")
    weight = float(cfg.get("weight", 0.75))
    te_weight = float(cfg.get("text_encoder_weight", weight))

    # 1) 触发词补进主正向（只补一次，不重复）
    main_pos = info.get("main_pos")
    if main_pos and trigger:
        cur = cc.get_text(workflow, main_pos) or ""
        if trigger.lower() not in cur.lower():
            new_text = f"{cur.strip().rstrip(',')}, {trigger}" if cur.strip() else trigger
            cc.set_text(workflow, main_pos, new_text)

    # 2) 更新 LoRA 堆：保留原有条目，命中角色条目追加或更新
    try:
        items = json.loads(workflow[stack_id]["inputs"].get("lora_str") or "[]")
        if not isinstance(items, list):
            items = []
    except Exception:
        items = []
    updated = False
    for it in items:
        if isinstance(it, dict) and it.get("lora") == lora_file:
            it["weight"] = weight
            it["text_encoder_weight"] = te_weight
            updated = True
            break
    if not updated:
        items.append({"lora": lora_file, "weight": weight, "text_encoder_weight": te_weight})
    workflow[stack_id]["inputs"]["lora_str"] = json.dumps(items, ensure_ascii=False)
    logger.info(
        f"comfyui_render 自动挂角色 LoRA：{name} → {lora_file} "
        f"(trigger={trigger}, weight={weight}/{te_weight})"
    )
    return name


def _randomize_seed(workflow: dict) -> None:
    for nid in cc.seed_nodes(workflow):
        node = workflow.get(nid)
        if isinstance(node, dict) and "seed" in node.get("inputs", {}):
            node["inputs"]["seed"] = uuid.uuid4().int % (2**53)


_LAUNCH_LOCK = threading.Lock()  # P2：冷启动「探活→拉起→等待」全程互斥，防并发双拉起


def _submit(
    prompt: str,
    negative: str,
    ws_dir: str | None = None,
    base_path: str | None = None,
    role_match_text: str = "",
) -> tuple[str, str, str]:
    """固定流程（提交阶段）：探活 → 选定工作流 → 临时文件改词 → 提交出图任务。

    【汐小姐要求】只提交不等待：拿到 prompt_id 立刻返回，轮询等待全部
    挪到后台任务，避免挤占对话。

    Args:
        role_match_text: 翻译前的原始提示词（P3：角色 LoRA 匹配时与翻译后文本先后比对）

    Returns:
        (prompt_id, out_dir, stem)
    """
    # 出图/临时目录：优先会话工作区，兜底插件数据目录
    # P5：先建根目录再写启动日志，避免工作区目录不存在时 FileNotFoundError
    base_dir = ws_dir or _data_dir()
    os.makedirs(base_dir, exist_ok=True)
    out_dir = os.path.join(base_dir, "comfy_out")
    tmp_dir = os.path.join(base_dir, "comfy_tmp")

    # 1. 探活；没起则拉起并等就绪（P2：加锁防并发双拉起）
    with _LAUNCH_LOCK:
        if not cc.is_up():
            log_path = os.path.join(base_dir, "comfyui_launch.log")
            sc.launch([], log_path)
            if not cc.wait_up(deadline=300.0, interval=3.0):
                raise RuntimeError(f"ComfyUI 在 300s 内没有就绪，日志见 {log_path}")

    # 2. 选定工作流：群聊固定快速出图；私聊管理员可指定（【汐小姐要求】）
    if not base_path:
        base_path = _FAST_WF_PATH
    if not os.path.exists(base_path):
        raise RuntimeError(f"找不到工作流：{base_path}")

    # 3. 复制基准到临时文件（不碰基准本体）
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_path = os.path.join(
        tmp_dir, f"_tmp_gate_{int(time.time())}_{uuid.uuid4().hex[:6]}.json"
    )
    shutil.copy2(base_path, tmp_path)

    try:
        # 4. 改提示词 + 随机 seed
        # 【汐小姐要求】正向：保留工作流原有提示词不动，模型新词只追加到末尾，不整段覆写。
        # 【汐小姐要求】负面：保留工作流原有负面词，新词只追加到末尾，不整段覆写；不填则沿用工作流自带负面词。
        wf = cc.normalize_ids(cc.load_workflow(tmp_path))
        info = cc.analyze(wf)
        main_pos, main_neg = info.get("main_pos"), info.get("main_neg")
        prompt = (prompt or "").strip()

        existing_pos = cc.get_text(wf, main_pos) if main_pos else ""
        existing_pos = (existing_pos or "").strip().rstrip(",")
        # L2：tag 级去重（只治重复，不治冲突）：用户词中已存在于基准词的 tag 滤除后再追加
        prompt = _dedup_tags(prompt, existing_pos)
        # 拼接：工作流原有词 + 模型新词（去重后可能为空，为空说明基准词已完整覆盖）
        if existing_pos and prompt:
            new_pos = f"{existing_pos}, {prompt}"
        else:
            new_pos = prompt or existing_pos
        pos_ok = cc.set_text(wf, main_pos, new_pos) if main_pos else False
        if not pos_ok:
            raise RuntimeError("工作流里定位不到主正向节点，无法写入提示词")
        neg_text = (negative or "").strip()
        if main_neg and neg_text:
            # 【汐小姐要求】负面同样只追加：保留工作流原有负面词，新词只加到末尾，不整段覆写
            existing_neg = cc.get_text(wf, main_neg)
            existing_neg = (existing_neg or "").strip().rstrip(",")
            new_neg = f"{existing_neg}, {neg_text}" if existing_neg else neg_text
            if not cc.set_text(wf, main_neg, new_neg):
                logger.warning("工作流里定位不到主负面节点，跳过负面词写入")
        # 【汐小姐要求】自动挂角色 LoRA：识别提示词里的角色，回填触发词+LoRA 堆
        # （权重/触发词全部来自预配置映射表，不现场编；没命中则静默不动）
        # P3：翻译后文本 + 翻译前原文都参与匹配，中文关键词才能命中
        _attach_role_lora(wf, info, prompt, role_match_text)
        _randomize_seed(wf)
        cc.save_workflow(tmp_path, wf)

        # 5. 提交任务，拿到 prompt_id 立即返回（不在此等待）
        os.makedirs(out_dir, exist_ok=True)
        stem = "comfy_" + time.strftime("%Y%m%d_%H%M%S")
        resp = cc.api_post_json("/prompt", {"prompt": wf, "client_id": "comfy_gate"})
        if resp.get("error"):
            raise RuntimeError(f"ComfyUI 拒绝了任务：{resp['error']}")
        pid = resp.get("prompt_id")
        if not pid:
            raise RuntimeError("ComfyUI 没返回 prompt_id")
        return pid, out_dir, stem
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@register(
    "astrbot_plugin_comfy_gate",
    "昔涟",
    "ComfyUI 出图闸门：群聊自然语言出图；私聊管理员可指定工作流精细出图。",
    "1.7.1",
)
class IrmiaComfyGatePlugin(Star):
    """ComfyUI 群聊出图闸门插件主类。

    核心能力通过 LLM 工具 ``comfyui_render`` 暴露：群友用自然语言点单，
    工具内部固定执行「探活 → 选工作流 → 临时文件改词 → 出图 → 清理」。
    """

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        # 持有后台出图任务引用，防止被垃圾回收
        self._bg_tasks: set[asyncio.Task] = set()
        # 私聊工作流粘性状态（umo → 上次显式指定的关键词）：
        # 仅显式指定成功时更新，无 TTL，重启清空（2026-10-24 拍板：失效语义最简）
        self._sticky_wf: dict[str, str] = {}
        # P4：并发与冷却状态（管理员免冷却，但同样受全局并发上限约束）
        self._render_slots: int = 0
        self._last_render_ts: dict[str, float] = {}
        # P1：注入互斥锁，串行化「读历史→追加→写回」，消除并发注入交叠
        self._inject_lock = asyncio.Lock()

    def _spawn_bg_task(self, coro) -> asyncio.Task:
        """创建后台任务并持有引用，完成后自动移除。"""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    @llm_tool(name="comfyui_render")
    async def comfyui_render(
        self,
        event: AstrMessageEvent,
        prompt: str,
        negative: str = "",
        workflow: str = "",
    ):
        """用 ComfyUI 生成图片。仅在有人明确要求画图/出图/生图/来一张XX时才调用（如"画一张猫娘""出张图"）；只是聊到"画"字、描述画面、讨论图片或没有明确生成请求时，绝不调用。

        【群聊】任何群友可用，固定走「快速出图版」（轻量娱乐向，单次采样直接出 1 张）。
        【私聊】仅管理员可实际出图。收到以 noop 开头的工具结果 = 本次调用未执行任何操作：请当作调用没发生过，直接自然继续原来的话题，不要向用户转述 noop 或工具细节；仅当用户明确要求画图时，才顺口提一句群聊里可以画。管理员指定工作流：workflow 参数填工作流名字（工作流目录文件名的关键词，以实时目录为准，改名/增删无需固定对应），找不到时工具会返回当前候选清单，照着清单里的名字重新指定即可；不传则自动沿用本会话上次使用的工作流，本会话首次出图用默认工作流。需要精细控制（换服装、改负面、修手脸）时，把要用的工作流名和画面描述一起放进 prompt 说明。

        【提示词】prompt 按以下槽位顺序输出英文 danbooru tag（半角逗号分隔）：人数 → 角色 → 发型发色 → 服饰 → 饰品 → 表情 → 动作姿势 → 道具 → 场景背景 → 构图镜头 → 光线氛围。槽位分两类：① 自由槽（人数/服饰/饰品/表情/动作姿势/道具/场景背景/构图镜头/光线氛围）：用户提到的细节一个不漏，没提到的积极补全（每槽 1~3 个 tag），让画面完整有设计感；② 身份槽（发型发色/瞳色/瞳孔特征/角色专属特征件）：画泛指或原创人物时照常补全；画系统注册的 LoRA 角色（如昔涟、遐蝶、风堇、赛飞儿等，出图时会自动挂载专属 LoRA）时默认不写——其外貌由 LoRA 与工作流基准词承载，自己再写外貌词会和 LoRA 打架；仅当用户明确要求改变其固有外貌时（如"染成黑发"），正向写新特征，并把原特征（如 pink hair）填进 negative 参数压制。其他规则：不补质量词/画风词（工作流已自带，用户明确要求的画风除外）；角色名原样保留（系统据此自动挂 LoRA）；只写 tag 不写句子。中文可直接写，会自动翻译。示例（泛指人物）：1girl, red hair, gradient hair, cat ears, maid dress, white apron, smile, holding ice cream cone, melting ice cream, kitchen, upper body, soft lighting

        【交付】出图后插件会在后台自动把图片静默推送到当前会话，图片到达即交付；你只需要用文字自然回应（如「画好啦♪」），【禁止】再用任何方式重复发送图片，避免同一张图出现两遍。中途没有任何进度播报，不需要预告时间；只有出错时系统才会额外发一条失败消息。

        【限流】非管理员出图有会话冷却（默认30秒），全局同时出图有上限（默认2张）；返回「出图队列已满」「出图冷却中」开头的结果时，把意思自然转述给用户让其稍后再试，不要立即重复调用。管理员出图不受冷却限制。

        Args:
            prompt(string): 画面描述；优先按槽位顺序给英文 danbooru tag（人数→角色→发型发色→服饰→饰品→表情→动作姿势→道具→场景→构图→光线，见工具说明），中文会自动翻译
            negative(string): 负面提示词，可选；不填沿用工作流自带负面词
            workflow(string): 可选，仅私聊管理员生效；工作流名字关键词，以实时目录匹配为准；不传则沿用本会话上次的工作流
        """
        msg_type = event.get_message_type()
        is_admin = _is_admin(event)

        # 【2026-10-24 拍板】除群聊外一律按私聊处理；非管理员调用直接返回极简中性值，
        # 插件不执行任何操作（不起服务、不发消息），由 LLM 按 docstring 约定自然继续原话题
        if msg_type != MessageType.GROUP_MESSAGE and not is_admin:
            return _TOOL_NOOP

        # 工作流配置（每次调用实时读取，支持改文件热更新）
        wf_cfg = _load_workflow_config()
        wf_aliases = wf_cfg.get("aliases") or {}
        wf_dir = str(wf_cfg.get("workflow_dir") or _WF_DIR)

        # 工作流选择：群聊固定 fast_workflow；私聊管理员显式指定 > 会话粘性 > 私聊默认
        base_path: str | None = None
        if msg_type == MessageType.GROUP_MESSAGE:
            base_path, wf_err = _resolve_workflow(
                str(wf_cfg.get("fast_workflow") or "快速出图"), wf_aliases, wf_dir
            )
            if not base_path:
                if os.path.exists(_FAST_WF_PATH):
                    logger.warning(
                        f"comfyui_render 群聊快速工作流动态解析失败（{wf_err}），退回内置路径"
                    )
                    base_path = _FAST_WF_PATH
                else:
                    return wf_err or "找不到群聊快速出图工作流"
        else:
            keyword = (workflow or "").strip()
            if keyword:
                base_path, wf_err = _resolve_workflow(keyword, wf_aliases, wf_dir)
                if wf_err or not base_path:
                    return wf_err or "没找到匹配的工作流"
                # 显式指定成功 → 更新会话粘性，后续省略参数时沿用
                self._sticky_wf[event.unified_msg_origin] = keyword
            else:
                sticky = self._sticky_wf.get(event.unified_msg_origin)
                if sticky:
                    base_path, wf_err = _resolve_workflow(sticky, wf_aliases, wf_dir)
                    if not base_path:
                        logger.warning(
                            f"comfyui_render 粘性工作流「{sticky}」已失效（{wf_err}），回落私聊默认"
                        )
                if not base_path:
                    base_path, wf_err = _resolve_workflow(
                        str(wf_cfg.get("private_default") or "高清修复"), wf_aliases, wf_dir
                    )
                    if not base_path:
                        return wf_err or "私聊默认工作流不可用，请检查 data/workflow_config.json"

        prompt = (prompt or "").strip()
        if not prompt:
            return "得先告诉人家要画什么呀～把画面描述填进 prompt 就好。"

        # D6（2026-10-24）：受理状态探测——区分「服务未启动→后台拉起中」与
        # 「已就绪→报队列快照」，结果写进工具返回值由 LLM 吃人设转述。
        # 探测放在限流闸门之前，避免在「检查→计数」之间引入 await 破坏原子性
        try:
            comfy_up: bool | None = await asyncio.to_thread(cc.is_up)
        except Exception:
            comfy_up = None
        queue_brief = ""
        if comfy_up:
            queue_brief = await asyncio.to_thread(_queue_brief)

        # P4：并发与冷却闸门（话术交由 LLM 自然转述；管理员免冷却，但同样受并发上限约束）
        # 复核修正（v1.6.1）：0 = 不限制（旧版 `or 2` 会把显式 0 静默变成 2），
        # 与 cooldown_seconds: 0 = 不冷却的语义对齐
        mc = wf_cfg.get("max_concurrent")
        max_concurrent = 2 if mc is None else max(0, int(mc))
        cd = wf_cfg.get("cooldown_seconds")
        cooldown = 0.0 if cd is None else max(0.0, float(cd))
        if max_concurrent > 0 and self._render_slots >= max_concurrent:
            return (
                f"出图队列已满：当前正在画 {self._render_slots} 张，画完再喊。"
                "请把这句话的意思自然转述给用户，不要立即重试调用。"
            )
        last_ts = self._last_render_ts.get(event.unified_msg_origin)
        if not is_admin and cooldown > 0 and last_ts and time.time() - last_ts < cooldown:
            remain = max(1, int(cooldown - (time.time() - last_ts)))
            return (
                f"出图冷却中：这个会话约 {remain} 秒后可以再画。"
                "请自然转述让用户稍等，不要立即重试调用。"
            )

        # 【汐小姐要求】彻底异步：翻译/探活/改词/提交/等待/推送全部丢进后台任务，
        # 工具只做秒级校验立即返回，出图期间对话完全不被占住。
        self._render_slots += 1
        self._last_render_ts[event.unified_msg_origin] = time.time()
        try:
            self._spawn_bg_task(self._render_and_send(event, prompt, negative, base_path))
        except Exception:
            self._render_slots -= 1
            raise
        # D6：受理回执（走 LLM 吃人设）——按探测结果给状态，替代旧的固定中性句；
        # 此后全程静默，仅在出图完成（推图+衔接语）或出错时再发消息
        if comfy_up is False:
            status_note = (
                "ComfyUI 服务当前未启动，系统正在后台自动拉起（可能需要一两分钟），"
                "就绪后本任务会自动进入生成队列。"
            )
        elif comfy_up:
            status_note = f"ComfyUI 已就绪，任务即将进入生成队列。{queue_brief}"
        else:
            status_note = "任务已受理，正在后台进入生成队列。"
        return (
            f"出图请求已受理。{status_note}"
            "请用你自己的话自然告知用户当前受理状态（保持人设），不要说固定模板台词；"
            "中途不会再有进度播报，出图完成时会自动送达，不要预告具体时间。"
        )

    async def _render_and_send(
        self,
        event: AstrMessageEvent,
        prompt: str,
        negative: str,
        base_path: str | None,
    ) -> None:
        """后台任务（【汐小姐要求】全流程异步）：翻译 → 提交 → 轮询等图 → 推送到原会话。

        工具函数只做秒级校验就立即返回，翻译/探活/改词/提交/等待/推送
        全部挪进本后台任务，出图期间对话完全不被占住。
        """
        try:
            # 中文自动翻译成英文 tag（固定流程）；P3：保留翻译前原文供角色 LoRA 匹配
            original_prompt = prompt
            if _has_cjk(prompt):
                # L1'：二分前的确定性判断——是否命中注册角色 LoRA，导向两种翻译指令
                role_hit = _match_role_lora(original_prompt)
                sys_prompt = _TRANSLATE_SYSTEM
                if role_hit:
                    sys_prompt += "\n" + _TRANSLATE_ROLE_YIELD.format(_name_=role_hit[0])
                    logger.info(
                        f"comfyui_render 命中注册角色「{role_hit[0]}」，翻译指令切换为身份槽让位"
                    )
                translated = await self._translate_to_english_tags(
                    event, prompt, system_prompt=sys_prompt
                )
                if translated:
                    logger.info(f"comfyui_render 中文提示词已自动翻译：{translated[:120]}")
                    prompt = translated
                else:
                    await event.send(
                        MessageChain().message(
                            "提示词里的中文没能自动翻译成英文，人家不敢乱画～"
                            "把画面需求改成英文 danbooru tag（逗号分隔）再喊人家一声就好♪"
                        )
                    )
                    return
            ws_dir = _session_workspace_dir(event)
            pid, out_dir, stem = await asyncio.to_thread(
                _submit, prompt, negative, ws_dir, base_path, original_prompt
            )
            paths = await asyncio.to_thread(
                cc.wait_and_download, pid, out_dir, stem, timeout=_SUBMIT_TIMEOUT
            )
            # 【汐小姐要求】统一由 LLM 发话：后台只静默投递图片，不带任何固定文案，
            # 避免和 LLM 台词重复打架。图片本身到达即交付，文字交给 LLM。
            if not paths:
                logger.warning("comfyui_render 后台出图超时或没有拿到图片文件")
                await event.send(MessageChain().message("这张图没能生成出来，要不要再试一次？"))
                return
            chain = MessageChain()
            for p in paths:
                chain.file_image(p)
            await event.send(chain)
            logger.info(f"comfyui_render 后台推送完成，{len(paths)} 张")
            # 【汐小姐要求】A：推送完成后补一句衔接语（仅纯文字，绝不重复发图），
            # 让对话接上「图在后台」→「图来了」的逻辑，避免逻辑脱节。
            follow_up = await self._gen_follow_up_text(event, len(paths), prompt_desc=prompt)
            if follow_up:
                try:
                    await event.send(MessageChain().message(follow_up))
                    logger.info("comfyui_render 衔接语已发送")
                except Exception as e:
                    logger.error(f"comfyui_render 衔接语发送失败: {e}")
            # 【汐小姐要求】B：推送完成后向会话上下文注入提示，
            # 让 LLM 明确知道图已发出，避免后续重复发图 / 重复跑图；
            # 并告知本次工作流（β：LLM 知情，可确认/主动更换，插件侧自动沿用）
            await self._inject_image_sent_notice(
                event,
                follow_up=follow_up,
                prompt_desc=prompt,
                wf_name=_wf_display_name(base_path),
            )
        except Exception as exc:
            logger.error(f"comfyui_render 后台出图失败: {exc}")
            # P8b：详细异常只给管理员（便于排障），普通用户发通用文案避免泄露内部路径
            if _is_admin(event):
                fail_text = f"出图失败了：{exc}"
            else:
                fail_text = "出图失败了，请稍后再试～"
            try:
                await event.send(MessageChain().message(fail_text))
            except Exception as e2:
                logger.error(f"comfyui_render 后台失败提示也发不出: {e2}")
        finally:
            # P4：无论成败都释放并发槽位
            self._render_slots = max(0, self._render_slots - 1)

    async def _gen_follow_up_text(
        self, event: AstrMessageEvent, image_count: int, prompt_desc: str = ""
    ) -> str:
        """【汐小姐要求】推送完成后，让 LLM 生成一句自然衔接语。

        仅输出纯文字（一句话），严禁附带图片/附件/链接/Markdown，
        避免把刚推送的图片再发一遍。
        """
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
            if provider is None:
                logger.warning("comfyui_render 拿不到当前 LLM provider，跳过衔接语")
                return ""
            sys = (
                "你刚刚通过出图工具为用户生成并推送了图片，用户已经能看到图。"
                "现在请用你一贯的口吻说一句简短的衔接语（40 字以内），"
                "自然地把「图来了」带出来，让对话顺畅接上。"
                "可以自然地提到画面里最突出的元素（服装、姿势、氛围等），让衔接语有画面感，"
                "不要干巴巴地只说图来了。"
                "硬性要求：只输出这一句话的文字本身，不要输出任何图片、附件、链接、"
                "Markdown 或多余解释；绝对不要把图片再发一遍。"
            )
            desc = (
                f"本次画面描述（英文 tag，参考用）：{prompt_desc[:500]}" if prompt_desc else ""
            )
            resp = await provider.text_chat(
                prompt=(
                    f"刚才共生成并推送了 {image_count} 张图片，用户已经收到。"
                    f"{desc}"
                    "请接一句自然的话（保持你一贯的口吻）。"
                ),
                system_prompt=sys,
                contexts=[],
            )
            out = _strip_llm_output(getattr(resp, "completion_text", ""))
            return out[:80]
        except Exception as exc:
            logger.error(f"comfyui_render 生成衔接语失败: {exc}")
            return ""

    async def _inject_image_sent_notice(
        self,
        event: AstrMessageEvent,
        follow_up: str = "",
        prompt_desc: str = "",
        wf_name: str = "",
    ) -> None:
        """图片推送成功后，向当前会话的对话历史注入一条提示消息。

        【汐小姐要求】gate 后台推图不写回上下文，LLM 不知道自己已把图发出去，
        容易重复发图 / 重复跑图。这里在推送完成后补一条 assistant 消息，
        让下一轮 LLM 读取历史时明确知道「图已生成并推送，勿重复发图跑图」。
        """
        try:
            # P1：全程持锁，串行化并发注入，消除「读历史→追加→写回」交叠
            async with self._inject_lock:
                conv_mgr = self.context.conversation_manager
                umo = event.unified_msg_origin
                cid = await conv_mgr.get_curr_conversation_id(umo)
                if not cid:
                    logger.warning("comfyui_render 拿不到当前对话 ID，跳过上下文注入")
                    return
                conv = await conv_mgr.db.get_conversation_by_id(cid=cid)
                if not conv:
                    logger.warning("comfyui_render 拿不到当前对话，跳过上下文注入")
                    return
                history = conv.content or []
                note = (
                    "【系统·图片交付】刚才请求的图片已生成并推送到当前会话，图片送达即交付完成。"
                    "请勿重复发送图片，也不要再次调用出图工具跑同一张图。"
                )
                if prompt_desc:
                    note += f"（本次画面描述：{prompt_desc[:200]}）"
                if wf_name:
                    note += f"（本次使用工作流：{wf_name}；用户要求更换时在 workflow 参数指定，未指定则插件自动沿用）"
                if follow_up:
                    note += f"（衔接语已发出：{follow_up}）"
                history.append(
                    {
                        "role": "assistant",
                        "content": note,
                    }
                )
                await conv_mgr.db.update_conversation(cid=cid, content=history)
            logger.info("comfyui_render 已向会话上下文注入「图片已推送」提示")
        except Exception as exc:
            logger.error(f"comfyui_render 注入上下文提示失败: {exc}")

    async def _translate_to_english_tags(
        self, event: AstrMessageEvent, text: str, system_prompt: str | None = None
    ) -> str:
        """用当前 LLM provider 把中文提示词翻译成英文 danbooru tag（固定流程）。

        system_prompt 缺省用 _TRANSLATE_SYSTEM；命中注册角色时由调用方
        拼入身份槽让位附加说明（L1' 二分）。
        """
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
            if provider is None:
                logger.warning("comfyui_render 拿不到当前 LLM provider，无法自动翻译")
                return ""
            resp = await provider.text_chat(
                prompt=text,
                system_prompt=system_prompt or _TRANSLATE_SYSTEM,
                contexts=[],
            )
            out = _strip_llm_output(getattr(resp, "completion_text", ""))
            return out
        except Exception as exc:
            logger.error(f"comfyui_render 自动翻译失败: {exc}")
            return ""
