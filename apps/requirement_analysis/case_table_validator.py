"""
测试用例表格校验器

背景：AI 生成的 Markdown 用例表存在两类系统性缺陷——
  1. 表格结构错乱：单元格文本里混入未转义的 `|`，导致整行右移、列数错位；
     或在表格中段重复插入表头行，导致该行数据被拆断。
  2. 预期结果不可判定：出现"或""允许误差""视情况"等模糊表述，人工无法判通过/失败。

现有 `_parse_table_format` 对 `len(row) < len(headers)` 的行是直接 `continue` 静默丢弃的，
所以这些缺陷不会报错，只会让用例悄悄变少或内容错位。本模块把这些问题显式化。

纯 Python 实现，不依赖 Django，便于单测与离线复用。
"""
import re
from typing import Any

# 标准表头（顺序固定）
CANONICAL_HEADERS = [
    '用例id', '测试目标', '前置条件', '操作步骤',
    '预期结果', '优先级', '测试类型', '关联需求',
]

# 表头别名映射，兼容历史提示词产出的不同列名
HEADER_ALIASES = {
    '用例id': ('用例id', '用例编号', '编号', '序号', 'id', 'caseid', '用例编号id'),
    '测试目标': ('测试目标', '测试场景', '用例标题', '标题', '场景', '名称', '测试项'),
    '前置条件': ('前置条件', '前提条件', '前提', '预置条件'),
    '操作步骤': ('操作步骤', '测试步骤', '步骤', '操作过程'),
    '预期结果': ('预期结果', '预期', '期望结果', '期望'),
    '优先级': ('优先级', 'priority', '级别'),
    '测试类型': ('测试类型', '用例类型', '类型', '测试类别'),
    '关联需求': ('关联需求', '需求编号', '关联需求编号', '需求id', '对应需求'),
}

CASE_ID_RE = re.compile(r'^[A-Z]{2,4}[-_]?\d{2,3}$')
# 关联需求编号：兼容 PRD 中 `ACC-01` 与 `R-ORD-04` 两种写法
REQ_ID_RE = re.compile(r'^(?:R[\-_])?[A-Z]{2,4}[\-_]\d{1,3}$')
PRIORITY_SET = {'P0', 'P1', 'P2', 'P3'}

# 强模糊词：出现在预期结果中即判定为不可判定
STRONG_AMBIGUOUS = ('允许误差', '视情况', '酌情', '误差范围', '大致', '若干', '如有需要')
# 弱模糊词：仅提示
WEAK_AMBIGUOUS = ('可能', '尽量', '适当', '合理', '尽快', '等等')

SEVERITY_ERROR = 'error'
SEVERITY_WARNING = 'warning'


def _norm_cell(text: str) -> str:
    """单元格归一化：去加粗、去多余空白"""
    t = re.sub(r'\*{2,}', '', str(text or ''))
    t = t.replace('&nbsp;', ' ')
    return re.sub(r'[ \t\u3000]+', ' ', t).strip()


def _split_row(line: str) -> list[str]:
    """
    按 `|` 拆分表格行。

    处理两种转义写法：`\\|` 与 `&#124;`（提示词里约定的替代写法），
    其余裸 `|` 一律视为列分隔符——这正是错行的成因，需要被识别出来而非掩盖。
    """
    placeholder = '\x00PIPE\x00'
    s = line.replace('\\|', placeholder).replace('&#124;', placeholder)
    s = s.strip()
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|'):
        s = s[:-1]
    return [_norm_cell(c.replace(placeholder, '|')) for c in s.split('|')]


def _map_header(cells: list[str]) -> list[str] | None:
    """把表头行映射到标准列名；识别不出足够列时返回 None"""
    mapped = []
    for c in cells:
        key = re.sub(r'[\s\-_]', '', c.lower())
        hit = None
        for canonical, aliases in HEADER_ALIASES.items():
            if any(key == re.sub(r'[\s\-_]', '', a) or a in key for a in aliases):
                hit = canonical
                break
        mapped.append(hit)  # 保留 None 占位，便于发现"多出来的列"
    recognized = [m for m in mapped if m]
    if len(recognized) < 6 or '用例id' not in recognized or '预期结果' not in recognized:
        return None
    return mapped


def _strip_fences(text: str) -> str:
    """剥离 ```markdown 围栏，避免围栏行被当成表格外文字"""
    return re.sub(r'^```[a-zA-Z]*\s*$', '', text, flags=re.MULTILINE)


def _looks_like_separator(cells: list[str]) -> bool:
    return all(re.fullmatch(r':?-{2,}:?', c) for c in cells if c) and any(cells)


def extract_table_blocks(text: str) -> tuple[list[list[list[str]]], list[str]]:
    """
    切分表格块。

    Returns:
        (blocks, stray_lines)
        blocks: 每个块是行列表，行是单元格列表
        stray_lines: 表格之外的非空文本行
    """
    blocks: list[list[list[str]]] = []
    stray: list[str] = []
    current: list[list[str]] = []

    for raw in _strip_fences(text or '').split('\n'):
        line = raw.strip()
        if not line:
            continue
        if '|' in line:
            current.append(_split_row(line))
        else:
            if current:
                blocks.append(current)
                current = []
            stray.append(line)
    if current:
        blocks.append(current)
    return blocks, stray


def validate_case_table(text: str, prev_case_ids: set[str] | None = None) -> dict[str, Any]:
    """
    校验用例表。

    Args:
        text: AI 输出的用例表文本
        prev_case_ids: 上一版用例ID集合，用于检测用例丢失（可选）

    Returns:
        {
          'passed': bool,            # 无 error 即通过
          'errors': [{rule, severity, row, case_id, message}],
          'stats': {rows, valid_rows, case_ids, modules, by_priority},
          'parsed': [{case_id, 测试目标, 前置条件, 操作步骤, 预期结果, 优先级, 测试类型, 关联需求}],
          'header_ok': bool,
        }
    """
    errors: list[dict] = []

    def add(rule: str, message: str, severity: str = SEVERITY_ERROR,
            row: int | None = None, case_id: str | None = None):
        errors.append({
            'rule': rule, 'severity': severity,
            'row': row, 'case_id': case_id, 'message': message,
        })

    blocks, stray = extract_table_blocks(text)

    if not blocks:
        add('R1', '未解析到任何表格，输出可能不是 Markdown 表格格式')
        return {'passed': False, 'errors': errors, 'stats': {}, 'parsed': [], 'header_ok': False}

    # 取第一个可识别表头的块作为主表
    main_block = None
    header_map = None
    for block in blocks:
        m = _map_header(block[0]) if block else None
        if m:
            main_block, header_map = block, m
            break

    if main_block is None:
        add('R1', '表头不可识别：需要包含「用例ID/测试目标/前置条件/操作步骤/预期结果/优先级/测试类型/关联需求」等列')
        return {'passed': False, 'errors': errors, 'stats': {}, 'parsed': [], 'header_ok': False}

    expected_cols = len(main_block[0])

    # R1：列数应为 8
    if expected_cols != 8:
        add('R1', f'表头应为 8 列，实际 {expected_cols} 列', row=0)

    # 存在多个表格块时提示（通常是中段插表头把表拆断了）
    if len(blocks) > 1:
        add('R8', f'检测到 {len(blocks)} 个独立表格块，表格可能被中途插入的表头或空行拆断',
            severity=SEVERITY_WARNING)

    parsed: list[dict] = []
    seen_ids: dict[str, int] = {}
    by_priority: dict[str, int] = {}

    for offset, cells in enumerate(main_block[1:], start=1):
        if _looks_like_separator(cells):
            continue

        row_no = offset

        # R8：数据行里又出现表头 → 中段重复插入表头
        if _map_header(cells):
            add('R8', '表格中段出现重复表头行，该处数据被拆断', row=row_no)
            continue

        # R2：列数不一致
        if len(cells) != expected_cols:
            guess = cells[0] if cells else None
            add('R2', f'该行列数为 {len(cells)}，与表头 {expected_cols} 列不一致（单元格内含未转义的 `|` 会导致错行）',
                row=row_no, case_id=guess)
            # 仍尝试按位置解析，便于前端定位
            record = {canonical: (cells[i] if i < len(cells) else '')
                      for i, canonical in enumerate([c for c in header_map if c])}
            parsed.append({'row': row_no, '_structurally_broken': True, **record})
            continue

        record = {}
        for i, canonical in enumerate(header_map):
            if canonical:
                record[canonical] = cells[i]
        parsed.append({'row': row_no, **record})

        case_id = record.get('用例id', '')
        title = record.get('测试目标', '')

        # R3：用例ID 格式与唯一性
        if not case_id:
            add('R3', '用例ID 为空', row=row_no)
        elif not CASE_ID_RE.match(case_id):
            add('R3', f'用例ID「{case_id}」不符合 [A-Z]{{2,4}}_001 格式', row=row_no, case_id=case_id)
        elif case_id in seen_ids:
            add('R3', f'用例ID「{case_id}」与第 {seen_ids[case_id]} 行重复', row=row_no, case_id=case_id)
        else:
            seen_ids[case_id] = row_no

        # R4：优先级
        priority = record.get('优先级', '').upper()
        if priority not in PRIORITY_SET:
            add('R4', f'优先级「{record.get("优先级", "")}」不在 P0/P1/P2/P3 之内', row=row_no, case_id=case_id)
        else:
            by_priority[priority] = by_priority.get(priority, 0) + 1

        # R5：关联需求
        req = record.get('关联需求', '')
        if not req:
            add('R5', '关联需求为空', row=row_no, case_id=case_id)
        elif not REQ_ID_RE.match(req):
            add('R5', f'关联需求「{req}」不符合 ACC-01 格式（可能被截断）', row=row_no, case_id=case_id)

        # R6：预期结果可判定性
        expected = record.get('预期结果', '')
        if len(expected) < 10:
            add('R6', f'预期结果过短（{len(expected)} 字），无法判定', row=row_no, case_id=case_id)
        else:
            strong = [w for w in STRONG_AMBIGUOUS if w in expected]
            if strong or '或' in expected:
                hit = strong or ['或']
                add('R6', f'预期结果含模糊表述「{"、".join(hit)}」，无法判定通过/失败',
                    row=row_no, case_id=case_id)
            weak = [w for w in WEAK_AMBIGUOUS if w in expected]
            if weak:
                add('R6', f'预期结果含弱模糊词「{"、".join(weak)}」，建议改写为确定结论',
                    severity=SEVERITY_WARNING, row=row_no, case_id=case_id)

        # R7：操作步骤
        steps = record.get('操作步骤', '')
        if not steps or not (re.search(r'\d', steps) or '<br>' in steps.lower()):
            add('R7', '操作步骤缺少编号或 <br> 分隔，视为不完整', row=row_no, case_id=case_id)

        # 测试目标非空
        if not title:
            add('R1', '测试目标为空', row=row_no, case_id=case_id)

    # R9：用例不丢失
    if prev_case_ids:
        current_ids = set(seen_ids)
        lost = sorted(prev_case_ids - current_ids)
        if lost:
            preview = '、'.join(lost[:10]) + ('…' if len(lost) > 10 else '')
            add('R9', f'修订后丢失 {len(lost)} 条原有用例：{preview}',
                severity=SEVERITY_WARNING)

    # R10：表格之外存在叙述性文字
    if stray:
        add('R10', f'表格外存在 {len(stray)} 行说明文字，应只输出表格',
            severity=SEVERITY_WARNING)

    modules: dict[str, int] = {}
    for cid in seen_ids:
        prefix = re.match(r'^[A-Z]{2,4}', cid)
        if prefix:
            modules[prefix.group(0)] = modules.get(prefix.group(0), 0) + 1

    error_count = sum(1 for e in errors if e['severity'] == SEVERITY_ERROR)
    warning_count = sum(1 for e in errors if e['severity'] == SEVERITY_WARNING)

    return {
        'passed': error_count == 0,
        'errors': errors,
        'header_ok': True,
        'stats': {
            'rows': len(parsed),
            'valid_rows': len(seen_ids),
            'case_ids': len(seen_ids),
            'modules': modules,
            'by_priority': by_priority,
            'error_count': error_count,
            'warning_count': warning_count,
        },
        'parsed': parsed,
    }


def extract_case_ids(text: str) -> set[str]:
    """快速提取用例ID集合，用于 R9 比对"""
    ids = set()
    for block in extract_table_blocks(text)[0]:
        for cells in block:
            if cells and CASE_ID_RE.match(cells[0]):
                ids.add(cells[0])
    return ids


def extract_score(review_text: str) -> int | None:
    """从评审报告中提取质量评分"""
    if not review_text:
        return None
    patterns = (
        r'质量评分[：:]\s*\**\s*(\d{1,3})\s*/\s*100',
        r'综合评分[：:]\s*\**\s*(\d{1,3})',
        r'评分[：:]\s*\**\s*(\d{1,3})\s*/\s*100',
    )
    for p in patterns:
        m = re.search(p, review_text)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


ISSUE_HEAD_RE = re.compile(r'\*\*Q(\d+)\s*[｜|]\s*([^*]*?)\*\*')


def extract_issue_blocks(review_text: str) -> list[dict]:
    """
    从评审报告中切出问题块，供前端勾选与定向修订。

    评审报告的问题块形如：
        **Q1｜表格结构错乱｜ACC_002 / ACC_003**
        ACC_002 行前置条件列被写成"ACC_002"……
        建议：按统一列模板重排。

    Returns:
        [{issue_id: 'Q1', title: '表格结构错乱', target: 'ACC_002 / ACC_003',
          body: '...', severity: '阻断级'}, ...]
    """
    if not review_text:
        return []

    matches = list(ISSUE_HEAD_RE.finditer(review_text))
    issues = []

    for idx, m in enumerate(matches):
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(review_text)
        body = review_text[start:end].strip()

        head_parts = [p.strip() for p in re.split(r'[｜|]', m.group(2))]
        title = head_parts[0] if head_parts else ''
        target = head_parts[1] if len(head_parts) > 1 else ''

        # 向上找最近的 ## 标题作为严重级别
        severity = ''
        head_prefix = review_text[:m.start()]
        headings = re.findall(r'^#{1,4}\s*(.+)$', head_prefix, flags=re.MULTILINE)
        if headings:
            last = headings[-1]
            if '阻断' in last:
                severity = '阻断级'
            elif '严重' in last:
                severity = '严重'
            elif '建议' in last or '优化' in last:
                severity = '建议'

        issues.append({
            'issue_id': f'Q{m.group(1)}',
            'title': title,
            'target': target,
            'body': body,
            'severity': severity,
        })

    return issues
