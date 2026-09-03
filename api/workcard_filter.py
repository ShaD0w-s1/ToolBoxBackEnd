"""工卡分配 + 工具/航材清单自动筛选的后端计算模块。

把原先在前端 useToolbox.ts 里的筛选逻辑迁移到后端，保证「依据工卡清单」上传后，
多端拿到的是同一份云端权威计算结果。数据以 sections 结构操作：

    sections: [{"name": 部位, "notes": "", "works": [{"name": 工作/类型, "items": [...]}]}]

对应关系：cat = section.name，sub = work.name，物品 = work.items。

匹配规则与前端逐条对齐：
- shares3Chars：3 连续字（中文 > 英文 > 数字）
- engEngineMatch：名称「（ ）」内 56/2500/PW 与工作准备单「发动机」比对
- engApuMatch：ENG 部位「（APU）」卡片仅与工卡号第2个-后2字=49 的工卡名称匹配
"""

import re

# 常量（与前端 domain/toolbox.ts 一致）
DEFAULT_CATEGORIES = ["ENG", "AV CB", "FC", "LG", "通用", "接机"]
WORKCARD_SECTIONS = ["FC", "LG", "AV CB", "ENG"]
SECTION_BY_AREA = {"FC": "FC", "LG": "LG", "ENG": "ENG", "AV": "AV CB", "CB": "AV CB"}
AREA_BY_SECTION = {"FC": "FC", "LG": "LG", "ENG": "ENG", "AV CB": "AV"}
WORKCARD_COLUMNS = ["序号", "工卡号", "工卡名称", "工卡分级", "参与人员", "工作签卡者", "必检"]
KEEP_CATEGORIES = ["通用", "接机"]
FIXED_MARK = "固定"
EO_PREFIX_TRIGGERS = ("EOJC", "MCO", "NRC")
# 工具筛选：完整引用部位（不受工卡匹配限制）；航材筛选仅「通用」。
TOOL_KEEP_FULL = {"通用", "接机"}
MATERIAL_KEEP_FULL = {"通用"}


# —— 匹配函数 ——

def normalize_match(text: str) -> str:
    return re.sub(r"[^一-鿿a-z0-9]", "", (text or "").lower())


def strip_connectors(text: str) -> str:
    return re.sub(r"[的啦呢吧啊哟嗯哈和與及并或等等、与以及]", "", normalize_match(text))


def is_subsequence(needle: str, hay: str) -> bool:
    i = 0
    for ch in hay:
        if i < len(needle) and ch == needle[i]:
            i += 1
    return i == len(needle)


def shares_3_chars(name: str, names: list) -> bool:
    """名称是否与任一工卡名称命中（方案 B：保留 3 连续字主判据 + 2 字整名旁路）。

    - 主判据（≥3 字）：3 连续字窗口分流——含中文 > 全英文 > 全数字（有中英则弃数字窗格，数字弱化保留）；
    - 旁路（B 方案）：名称归一化后恰为 2 字（含中文/字母、纯数字不放行）且整名被任一工卡名
      连续包含 → 命中（如「注油」「排故」整词出现在工卡内容中，原来 <3 字直接不匹配）。
    """
    n = normalize_match(name)
    if not n:
        return False
    if len(n) < 3:
        # B 方案旁路：仅 2 字整名连续包含；1 字名与纯数字 2 字名维持不匹配（弱化）
        if len(n) == 2 and not re.fullmatch(r"[0-9]{2}", n):
            for cn in names:
                if n in normalize_match(cn):
                    return True
        return False
    chinese_grams = []
    english_grams = []
    digit_grams = []
    for i in range(len(n) - 2):
        g = n[i:i + 3]
        if re.search(r"[一-鿿]", g):
            chinese_grams.append(g)
        elif re.fullmatch(r"[a-z]{3}", g):
            english_grams.append(g)
        elif re.fullmatch(r"[0-9]{3}", g):
            digit_grams.append(g)
    use_grams = (chinese_grams + english_grams) if (chinese_grams or english_grams) else digit_grams
    if not use_grams:
        return False
    for cn in names:
        c = normalize_match(cn)
        for g in use_grams:
            if g in c:
                return True
    return False


def eng_engine_match(name: str, engine: str) -> bool:
    """ENG 部位发动机匹配（括号内 56/2500/PW 与发动机型号比对）。"""
    parens = [p.strip().upper() for p in re.findall(r"[（(]([^）)]*)[）)]", name or "") if p.strip()]
    if not parens:
        return True
    engine = (engine or "").upper()[:4]
    engine_word = ""
    if "CFM5" in engine:
        engine_word = "56"
    elif "V253" in engine:
        engine_word = "2500"
    elif "PW11" in engine:
        engine_word = "PW"
    markers = ("56", "2500", "PW")
    for p in parens:
        has_marker = any(mk in p for mk in markers)
        if has_marker and engine_word and engine_word not in p:
            return False
    return True


def is_apu_workcard(workcard_id: str) -> bool:
    parts = (workcard_id or "").split("-")
    if len(parts) < 3:
        return False
    seg = parts[2].strip()
    return len(seg) >= 2 and seg[:2] == "49"


def _apu_workcard_names(cards: list) -> list:
    out = []
    for c in cards:
        if not is_apu_workcard(c.get("工卡号") or ""):
            continue
        n = (c.get("工卡名称") or "").strip()
        if n:
            out.append(n)
    return out


def eng_apu_match(name: str, apu_names: list) -> bool:
    """ENG 部位「（APU）」卡片：仅与 APU 工卡名称子集做 3 字匹配。"""
    if not re.search(r"[（(]APU[）)]", name or "", re.IGNORECASE):
        return True  # 非 APU 卡片，不适用
    if not apu_names:
        return False
    return shares_3_chars(name, apu_names)


def _sub_matches(sub: str, names: list, lube_names: list, clean_names: list) -> bool:
    """卡片名称与工卡名称的 3 连续字匹配，含「润滑/清洁」优先规则。

    - 名称含「（润滑）」：优先用「含润滑的工卡名称」子集匹配；不同时满足（无该子集或不命中）
      则回退用全部工卡名称做普通 3 连续字匹配。
    - 名称含「（清洁）」：同理。
    """
    if re.search(r"[（(]润滑[）)]", sub):
        if lube_names and shares_3_chars(sub, lube_names):
            return True
        return shares_3_chars(sub, names)
    if re.search(r"[（(]清洁[）)]", sub):
        if clean_names and shares_3_chars(sub, clean_names):
            return True
        return shares_3_chars(sub, names)
    return shares_3_chars(sub, names)


# —— 工卡分配 ——

def _lookup_aircraft(aircraft_rows: list, reg_no: str):
    target = (reg_no or "").strip()
    if not target:
        return None
    for row in aircraft_rows:
        if (row.get("飞机号") or "").strip() == target:
            return row
    return None


def _sort_av_cb_cards(sections: dict) -> None:
    cards = sections.get("AV CB", {}).get("cards") or []
    order = {"AV": 0, "CB": 1}
    cards.sort(key=lambda x: order.get(x.get("部位"), 2))


def apply_work_card_list(project_doc: dict, workcard_rows: list, aircraft_rows: list, parsed: dict):
    """工卡分配：填充工作准备单机号/工作内容/地点 + 回填飞机信息 + 按工卡号分配部位/分级。

    返回 (prep_sheet, workcard_assignment, written)。
    """
    prep_sheet = project_doc.get("prep_sheet") if isinstance(project_doc.get("prep_sheet"), dict) else {}
    base = prep_sheet.get("base") if isinstance(prep_sheet.get("base"), dict) else {}
    base["机号"] = parsed.get("机号", "")
    base["工作内容"] = parsed.get("工作内容", "")
    base["地点"] = parsed.get("地点", "")
    aircraft = _lookup_aircraft(aircraft_rows, parsed.get("机号", ""))
    if aircraft:
        base["FSN"] = str(aircraft.get("FSN") or "")
        base["MSN"] = str(aircraft.get("MSN") or "")
        base["机型"] = str(aircraft.get("机型") or "")
        base["发动机"] = str(aircraft.get("发动机") or "")
        base["ETOPS"] = str(aircraft.get("ETOPS") or "")
        base["ELT-DT"] = str(aircraft.get("ELT-DT") or "")
    prep_sheet["base"] = base

    # 工卡号 -> {部位, 分级}
    match = {}
    for row in workcard_rows:
        wid = (row.get("工卡号") or "").strip()
        if wid and wid not in match:
            match[wid] = {
                "area": (row.get("部位") or "").strip(),
                "level": (row.get("分级") or "").strip(),
            }

    assignment = project_doc.get("workcard_assignment") if isinstance(project_doc.get("workcard_assignment"), dict) else {}
    sections = assignment.get("sections") if isinstance(assignment.get("sections"), dict) else {}
    for sec in WORKCARD_SECTIONS:
        entry = sections.get(sec) if isinstance(sections.get(sec), dict) else {}
        entry["cards"] = []
        sections[sec] = entry
    assignment["unassigned"] = []

    # 临时分组（非标准键，前端「+ 临时分组」自建）：再次导入例行工卡清单时原样保留；
    # 若其中的工卡本次清单再次包含，则继续保留在临时分组、不再重复写入标准分组（自动清理重复卡）。
    temp_members = set()
    for sec_key, entry in sections.items():
        if sec_key not in WORKCARD_SECTIONS:
            for row in entry.get("cards") or []:
                wid = str(row.get("工卡号") or "").strip()
                if wid:
                    temp_members.add(wid)

    written = 0
    for card in parsed.get("cards", []):
        wid = (card.get("工卡号") or "").strip()
        info = match.get(wid) or {"area": "", "level": ""}
        序号 = (card.get("项次") or "").strip()
        if wid.upper().startswith(EO_PREFIX_TRIGGERS):
            序号 = "EO" + 序号
        row = {
            "序号": 序号,
            "工卡号": wid,
            "工卡名称": card.get("工卡名称", ""),
            "工卡分级": info["level"],
            "参与人员": "",
            "工作签卡者": "",
            "必检": "",
            "部位": info["area"],
        }
        if wid in temp_members:
            continue  # 已归入临时分组：保留分组归属，避免与标准分组重复
        section = SECTION_BY_AREA.get(info["area"])
        if section:
            sections[section]["cards"].append(row)
        else:
            assignment["unassigned"].append(row)
        written += 1
    _sort_av_cb_cards(sections)
    assignment["sections"] = sections
    return prep_sheet, assignment, written


# —— 工具 / 航材筛选（sections 结构）——

def _ensure_section(sections: list, cat: str) -> dict:
    for s in sections:
        if s.get("name") == cat:
            return s
    s = {"name": cat, "notes": "", "works": []}
    sections.append(s)
    return s


def _filter_sections(project_sections: list, lib_sections: list, names: list,
                     apu_names: list, lube_names: list, clean_names: list,
                     engine: str, keep_full: set) -> tuple:
    """核心筛选：删除不匹配的 work，补充匹配且缺失的 work。

    返回 (new_project_sections, deleted, added)。
    """
    deleted = 0
    added = 0
    lib_by_cat = {}
    for s in lib_sections:
        lib_by_cat[s.get("name", "")] = s

    for cat in DEFAULT_CATEGORIES:
        full_ref = cat in keep_full
        is_eng = cat == "ENG"
        proj_sec = _ensure_section(project_sections, cat)
        works = proj_sec.get("works") or []

        # 删除不匹配的 work
        if names:
            kept = []
            for w in works:
                sub = w.get("name", "")
                if FIXED_MARK in sub or full_ref:
                    kept.append(w)
                    continue
                is_apu = bool(re.search(r"[（(]APU[）)]", sub, re.IGNORECASE))
                if is_eng and is_apu:
                    # APU 卡片：engApuMatch 成功 → 保留（还要过发动机）；失败 → 删除
                    if eng_apu_match(sub, apu_names):
                        if not eng_engine_match(sub, engine):
                            deleted += 1
                            continue
                        kept.append(w)
                    else:
                        deleted += 1
                    continue
                if _sub_matches(sub, names, lube_names, clean_names):
                    if is_eng and not eng_engine_match(sub, engine):
                        deleted += 1
                        continue
                    kept.append(w)
                else:
                    deleted += 1
            proj_sec["works"] = kept

        # 补充缺失且匹配的 work
        lib_sec = lib_by_cat.get(cat)
        if lib_sec:
            lib_works = lib_sec.get("works") or []
            existing = {w.get("name", "") for w in proj_sec.get("works") or []}
            for lw in lib_works:
                sub = lw.get("name", "")
                is_fixed = FIXED_MARK in sub
                is_apu = bool(re.search(r"[（(]APU[）)]", sub, re.IGNORECASE))
                if is_eng and not is_fixed and is_apu:
                    if not eng_apu_match(sub, apu_names):
                        continue
                elif not full_ref and not is_fixed and not _sub_matches(sub, names, lube_names, clean_names):
                    continue
                # 需求2：固定卡片补回也受发动机匹配（不再跳过）。
                if is_eng and not eng_engine_match(sub, engine):
                    continue
                if sub in existing:
                    continue
                proj_sec["works"].append(lw)
                existing.add(sub)
                added += 1

    return project_sections, deleted, added


def apply_tool_filter(project_sections: list, lib_sections: list, names: list, apu_names: list,
                      lube_names: list, clean_names: list, engine: str) -> tuple:
    return _filter_sections(project_sections, lib_sections, names, apu_names, lube_names, clean_names, engine, TOOL_KEEP_FULL)


def apply_material_filter(project_sections: list, lib_sections: list, names: list, apu_names: list,
                          lube_names: list, clean_names: list, engine: str) -> tuple:
    return _filter_sections(project_sections, lib_sections, names, apu_names, lube_names, clean_names, engine, MATERIAL_KEEP_FULL)


def collect_workcard_names(assignment: dict) -> list:
    """收集工卡分配清单里所有工卡名称（去重，保持顺序）。"""
    names = []
    seen = set()
    sections = assignment.get("sections") if isinstance(assignment.get("sections"), dict) else {}
    for sec in WORKCARD_SECTIONS:
        entry = sections.get(sec) if isinstance(sections.get(sec), dict) else {}
        for c in entry.get("cards") or []:
            n = (c.get("工卡名称") or "").strip()
            if n and n not in seen:
                seen.add(n)
                names.append(n)
    for c in assignment.get("unassigned") or []:
        n = (c.get("工卡名称") or "").strip()
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    return names


def collect_apu_workcard_names(assignment: dict) -> list:
    """收集工卡号第2个-后2字=49 的工卡名称（APU 工卡名称子集）。"""
    out = []
    seen = set()
    sections = assignment.get("sections") if isinstance(assignment.get("sections"), dict) else {}
    all_cards = []
    for sec in WORKCARD_SECTIONS:
        entry = sections.get(sec) if isinstance(sections.get(sec), dict) else {}
        all_cards.extend(entry.get("cards") or [])
    all_cards.extend(assignment.get("unassigned") or [])
    for c in all_cards:
        if not is_apu_workcard(c.get("工卡号") or ""):
            continue
        n = (c.get("工卡名称") or "").strip()
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def collect_keyword_names(assignment: dict, keyword: str) -> list:
    """收集工卡名称中含某关键词的子集（如「润滑」「清洁」）。"""
    out = []
    for n in collect_workcard_names(assignment):
        if keyword in n:
            out.append(n)
    return out

