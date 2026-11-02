"""
用例表结构校验器单测

对应真实缺陷样本：TASK_11EA9867《星盒PRD_V1.1_可测试版》生成结果中，
评审报告指出的 ACC_002/ACC_003 错行、ORD_005 中段插表头、
ORD_029/INV_015 截断、ACC_022 预期二义性等问题。
"""
from apps.requirement_analysis.case_table_validator import (
    extract_case_ids,
    extract_issue_blocks,
    extract_score,
    validate_case_table,
)

HEADER = (
    '| 用例ID | 测试目标 | 前置条件 | 操作步骤 | 预期结果 | 优先级 | 测试类型 | 关联需求 |\n'
    '|--------|--------|--------|--------|--------|--------|--------|--------|\n'
)

ROW_OK = '| ACC_001 | 验证微信一键登录 | 用户未登录 | 1. 进入首页<br>2. 点击登录 | 登录成功并返回 token | P0 | 功能验证 | ACC-01 |\n'


def _table(*rows: str) -> str:
    return HEADER + ''.join(rows)


class TestValidateCaseTable:

    def test_valid_table_passes(self):
        result = validate_case_table(_table(ROW_OK))
        assert result['passed'] is True
        assert result['stats']['case_ids'] == 1
        assert result['stats']['error_count'] == 0

    def test_column_shift_detected(self):
        # ACC_002 的实际形态：单元格内含裸 | 导致整行右移成 17 列
        broken = (
            '| ACC_002 | 验证未登录时展示授权引导弹| ACC_002 | 验证未登录时展示引导弹层 '
            '| 用户未登录，首次进入 | 1. 打开首页 | 页面展示授权弹层 | P0 | 功能验证 | ACC-01 |\n'
        )
        result = validate_case_table(_table(broken))
        assert result['passed'] is False
        rules = {e['rule'] for e in result['errors']}
        assert 'R2' in rules

    def test_truncated_row_detected(self):
        # INV_015 的实际形态：只剩 4 列
        broken = '| INV_015 | 隐藏款在图鉴中的展示 |\n'
        result = validate_case_table(_table(ROW_OK, broken))
        assert result['passed'] is False
        assert any(e['rule'] == 'R2' for e in result['errors'])

    def test_mid_table_header_detected(self):
        # ORD_005 的实际形态：行被拆断，中段又插了一行表头
        result = validate_case_table(_table(ROW_OK, HEADER, ROW_OK.replace('ACC_001', 'ORD_005')))
        assert result['passed'] is False
        assert any(e['rule'] == 'R8' for e in result['errors'])

    def test_requirement_id_truncation_detected(self):
        row = ROW_OK.replace('ACC-01', 'ORD')
        result = validate_case_table(_table(row))
        assert any(e['rule'] == 'R5' for e in result['errors'])

    def test_requirement_id_with_r_prefix_allowed(self):
        # PRD 中存在 R-ORD-04 这种写法，不应误报
        row = ROW_OK.replace('ACC-01', 'R-ORD-04')
        result = validate_case_table(_table(row))
        assert not any(e['rule'] == 'R5' for e in result['errors'])

    def test_ambiguous_expected_result_detected(self):
        row = ROW_OK.replace('登录成功并返回 token', '登录成功或提示用户重试')
        result = validate_case_table(_table(row))
        assert any(e['rule'] == 'R6' for e in result['errors'])

    def test_duplicate_case_id_detected(self):
        result = validate_case_table(_table(ROW_OK, ROW_OK))
        assert any('重复' in e['message'] for e in result['errors'])

    def test_lost_case_ids_is_warning(self):
        result = validate_case_table(
            _table(ROW_OK), prev_case_ids={'ACC_001', 'ACC_002'}
        )
        lost = [e for e in result['errors'] if e['rule'] == 'R9']
        assert lost and lost[0]['severity'] == 'warning'
        # warning 不影响最终通过
        assert result['passed'] is True

    def test_code_fence_is_tolerated(self):
        result = validate_case_table('```markdown\n' + _table(ROW_OK) + '```')
        assert result['passed'] is True


class TestExtractHelpers:

    def test_extract_case_ids(self):
        ids = extract_case_ids(_table(ROW_OK))
        assert ids == {'ACC_001'}

    def test_extract_score(self):
        assert extract_score('**质量评分：72 / 100** ｜ **结论：需修改**') == 72
        assert extract_score('综合评分：85') == 85
        assert extract_score('没有评分') is None

    def test_extract_issue_blocks(self):
        review = (
            '# 二、发现的问题\n\n'
            '## A. 阻断级（必须先修）\n\n'
            '**Q1｜表格结构错乱｜ACC_002 / ACC_003**\n'
            'ACC_002 行前置条件列被写成"ACC_002"。\n建议：按统一列模板重排。\n\n'
            '**Q2｜内容截断｜ORD_029**\n'
            'ORD_029 关联需求列被截断。\n'
        )
        issues = extract_issue_blocks(review)
        assert [i['issue_id'] for i in issues] == ['Q1', 'Q2']
        assert issues[0]['severity'] == '阻断级'
        assert issues[0]['title'] == '表格结构错乱'
        assert 'ACC_002' in issues[0]['target']
        assert '建议' in issues[0]['body']
