"""
评审驱动的用例修订服务

与生成阶段内置的自动改进（`enable_auto_revise`）的区别：
  1. **手动触发**：生成时改不动、超时、或关闭了自动改进的任务，事后仍可补救；
  2. **定向修复**：只修用户勾选的那几个评审问题，而不是把 1.2 万字评审整篇倒给模型；
  3. **结构校验**：输出必须通过 `case_table_validator` 的规则，不合格自动带错误重试；
  4. **不降级**：校验未通过时**不覆盖** `final_test_cases`，保留上一版可用产物；
  5. **可回溯**：每轮输入/输出/校验结果/前后评分全部落库，可对比、可回滚。
"""
import asyncio
import logging
import re
import time

from django.db import close_old_connections, connections
from django.utils import timezone

from .case_table_validator import (
    extract_case_ids,
    extract_issue_blocks,
    extract_score,
    validate_case_table,
)
from .models import AIModelService, TestCaseGenerationTask, TestCaseRevision

logger = logging.getLogger(__name__)

# 需求原文投喂上限，避免挤占修订输出的 token 预算
REQUIREMENT_MAX_CHARS = 6000
# 单个问题块投喂上限
ISSUE_BLOCK_MAX_CHARS = 1200

# 修订阶段追加在编写提示词之后的硬约束（这部分是表格不坏的关键）
REVISION_CONSTRAINTS = """
【本次为「修订」任务，必须遵守以下输出契约】
1. 只输出一张 Markdown 表格，禁止输出任何表格之外的标题、说明、总结、代码块围栏。
2. 表头固定 8 列，顺序不可变：
   用例ID | 测试目标 | 前置条件 | 操作步骤 | 预期结果 | 优先级 | 测试类型 | 关联需求
3. 每一行必须正好 8 个单元格。单元格内部如需换行只能使用 <br>，禁止出现裸换行。
4. 单元格文本中如果出现竖线字符，必须写成 &#124;，绝对不要写裸 | 或 \\|，否则整行会错位。
5. 用例ID 格式为 模块前缀_三位序号（如 ACC_001），全局唯一，不得重复。
6. 优先级只能取 P0 / P1 / P2 / P3。
7. 关联需求必须是 ACC-01 或 R-ORD-04 这类编号，不得为空，不得截断成 "ORD" 这种残缺形式。
8. 预期结果必须可判定：禁止出现「或」「允许误差」「视情况」「酌情」「大致」等模糊表述，
   要写明具体的提示文案、状态变化、跳转目标或数据结果。
9. 未被点名的用例，除修正结构错误外，保持原文不动，一条都不能删。
"""


def truncate(text: str, limit: int) -> str:
    if not text:
        return ''
    if len(text) <= limit:
        return text
    return text[:limit] + '\n…（以下内容因长度限制已省略）'


def select_issue_text(review_feedback: str, target_ids: list[str] | None) -> str:
    """
    从评审报告中挑出要修的问题块。

    target_ids 为空时返回空字符串（由调用方决定退回全量模式）。
    """
    if not review_feedback:
        return ''

    issues = extract_issue_blocks(review_feedback)

    if target_ids:
        wanted = {str(i).upper().strip() for i in target_ids if str(i).strip()}
        picked = [i for i in issues if i['issue_id'].upper() in wanted]
    else:
        picked = [i for i in issues if i.get('severity') == '阻断级']

    if not picked:
        # 评审报告格式不符合 Q1｜标题 的结构时，退化为整篇投喂（截断）
        return truncate(review_feedback, REQUIREMENT_MAX_CHARS // 2)

    blocks = []
    for item in picked:
        head = f"**{item['issue_id']}｜{item['title']}"
        if item['target']:
            head += f"｜{item['target']}"
        head += '**'
        blocks.append(f"{head}\n{truncate(item['body'], ISSUE_BLOCK_MAX_CHARS)}")

    return '\n\n'.join(blocks)


def build_messages(
        task: TestCaseGenerationTask,
        current_cases: str,
        issues_text: str,
        guidance: str = '',
        validation_errors: list[dict] | None = None,
) -> list[dict]:
    """构造修订请求的 messages"""
    writer_prompt = ''
    if task.writer_prompt_config:
        writer_prompt = task.writer_prompt_config.content or ''

    system_content = (writer_prompt + '\n\n' + REVISION_CONSTRAINTS).strip()

    parts = [
        '请根据评审意见修复下面这份测试用例表。',
        '',
        '【需求原文（节选）】',
        truncate(task.requirement_text, REQUIREMENT_MAX_CHARS),
        '',
        '【当前用例表】',
        current_cases,
        '',
        '【本轮需修复的评审问题】',
        issues_text or '（评审报告未给出结构化问题条目，请按下方通用要求整体提升可执行性）',
    ]

    if guidance:
        parts += ['', '【人工补充要求】', guidance]

    if validation_errors:
        error_lines = []
        for e in validation_errors:
            if e.get('severity') != 'error':
                continue
            loc = f"第{e['row']}行" if e.get('row') is not None else '全局'
            cid = f"（{e['case_id']}）" if e.get('case_id') else ''
            error_lines.append(f"- {loc}{cid}：{e['message']}")
        if error_lines:
            parts += [
                '',
                '【⚠️ 上一轮输出未通过结构校验，请务必修正以下问题后重新输出完整表格】',
                '\n'.join(error_lines[:40]),
                '注意：必须重新输出完整表格，不要只输出被修改的行。',
            ]

    parts += ['', '直接输出修复后的完整用例表，不要输出任何解释性文字。']

    return [
        {'role': 'system', 'content': system_content},
        {'role': 'user', 'content': '\n'.join(parts)},
    ]


def clean_output(text: str) -> str:
    """剥离代码围栏，保留其余内容交给校验器判断"""
    if not text:
        return ''
    lines = [ln for ln in text.split('\n') if not re.match(r'^\s*```[a-zA-Z]*\s*$', ln)]
    return '\n'.join(lines).strip()


def extract_table_region(text: str) -> str:
    """
    截取从第一条表格行到最后一条表格行的区间。

    模型偶尔会在表格前后加"以下是修复后的用例表："这类话，
    校验阶段会作为 warning 提示，但落库时应当保持纯净。
    """
    lines = text.split('\n')
    start = end = None
    for idx, line in enumerate(lines):
        if '|' in line and line.strip().startswith('|'):
            if start is None:
                start = idx
            end = idx
    if start is None:
        return text.strip()
    return '\n'.join(lines[start:end + 1]).strip()


async def _collect_stream(config, messages: list[dict]) -> str:
    """消费流式接口，拼接完整输出"""
    generator = AIModelService.call_openai_compatible_api_stream(config, messages)
    chunks = []
    try:
        async for chunk in generator:
            if chunk:
                chunks.append(chunk)
    finally:
        try:
            await generator.aclose()
        except Exception as close_error:  # pragma: no cover - 清理失败不影响主流程
            logger.warning(f"关闭修订生成器时出错: {close_error}")
    return ''.join(chunks)


def call_writer(config, messages: list[dict]) -> str:
    """同步调用编写模型（内部起独立事件循环，供工作线程使用）"""
    return asyncio.run(_collect_stream(config, messages))


def call_reviewer(task: TestCaseGenerationTask, cases: str) -> str:
    """同步调用评审模型"""
    return asyncio.run(AIModelService.review_test_cases(task, cases))


def _touch(revision: TestCaseRevision, **fields):
    for k, v in fields.items():
        setattr(revision, k, v)
    revision.save(update_fields=list(fields.keys()))


def _touch_task(task: TestCaseGenerationTask, **fields):
    for k, v in fields.items():
        setattr(task, k, v)
    task.save(update_fields=list(fields.keys()))


def run_revision(revision_id: int, options: dict | None = None) -> None:
    """
    执行一轮修订。在工作线程中调用，内部自行捕获全部异常。

    Args:
        revision_id: 修订记录 ID
        options: {'max_retry': 1, 'auto_review': True, 'guidance': ''}
    """
    options = options or {}
    max_retry = max(0, min(int(options.get('max_retry', 1) or 0), 2))
    auto_review = bool(options.get('auto_review', True))
    guidance = (options.get('guidance') or '').strip()
    started = time.time()

    # 工作线程独立持有数据库连接，先清理继承来的陈旧连接
    close_old_connections()

    revision = TestCaseRevision.objects.select_related('task').get(id=revision_id)
    task = TestCaseGenerationTask.objects.select_related(
        'writer_model_config', 'writer_prompt_config',
        'reviewer_model_config', 'reviewer_prompt_config',
    ).get(id=revision.task_id)

    try:
        if not revision.writer_model_config:
            raise RuntimeError('任务未配置编写模型，无法执行修订')
        if not task.requirement_text:
            raise RuntimeError('任务缺少需求原文，无法执行修订')

        _touch(revision, status='generating', phase='generating', progress=10)
        _touch_task(task, status='revising', progress=85)

        current_cases = revision.input_cases or task.final_test_cases or task.generated_test_cases
        prev_ids = extract_case_ids(current_cases)
        issues_text = select_issue_text(revision.review_feedback, revision.target_issues or [])
        if not revision.review_feedback and not guidance:
            raise RuntimeError('评审意见为空，无可执行修订的依据')

        messages = build_messages(task, current_cases, issues_text, guidance)

        content = ''
        result: dict = {}
        for attempt in range(max_retry + 1):
            if attempt:
                _touch(revision, retry_count=attempt, phase='retrying', progress=15)
                logger.info(f"修订 {revision.id} 第 {attempt} 次重试")

            raw = call_writer(revision.writer_model_config, messages)
            content = clean_output(raw)

            _touch(revision, status='validating', phase='validating', progress=60)
            result = validate_case_table(content, prev_ids)
            logger.info(
                f"修订 {revision.id} 结构校验: passed={result['passed']}, "
                f"errors={result['stats'].get('error_count', 0)}, "
                f"warnings={result['stats'].get('warning_count', 0)}"
            )
            if result['passed']:
                break
            if attempt < max_retry:
                messages = build_messages(
                    task, current_cases, issues_text, guidance,
                    validation_errors=result['errors'],
                )

        table = extract_table_region(content)
        revision.output_cases = table
        revision.validation_result = result

        if result.get('passed'):
            # 校验通过才允许覆盖正式产物
            _touch_task(
                task,
                final_test_cases=table,
                current_round=revision.round,
                status='completed',
                progress=100,
                completed_at=timezone.now(),
            )
            _touch(revision, status='completed', progress=90)
            logger.info(f"修订 {revision.id} 校验通过，已更新任务最终用例")

            if auto_review and task.reviewer_model_config and task.reviewer_prompt_config:
                try:
                    _touch(revision, phase='reviewing', progress=92)
                    review_text = call_reviewer(task, table)
                    score = extract_score(review_text)
                    revision.review_feedback_after = review_text
                    revision.score_after = score
                    task.review_feedback = review_text
                    if score is not None:
                        task.latest_score = score
                    task.save(update_fields=['review_feedback', 'latest_score'])
                    logger.info(f"修订 {revision.id} 复评完成，评分: {score}")
                except Exception as review_error:
                    logger.warning(f"修订 {revision.id} 复评失败: {review_error}")
                    revision.error_message = f'复评失败: {review_error}'
        else:
            # 校验未通过：保留旧版正式产物，不写入 final_test_cases
            error_count = result.get('stats', {}).get('error_count', 0)
            _touch(
                revision,
                status='needs_manual',
                progress=100,
                error_message=f'仍有 {error_count} 项结构错误未修复，已保留上一版用例',
            )
            _touch_task(task, status='completed', progress=100)
            logger.warning(f"修订 {revision.id} 校验未通过，保留原最终用例")

    except Exception as exc:  # noqa: BLE001 - 后台线程必须兜住所有异常
        logger.exception(f"修订 {revision_id} 执行失败: {exc}")
        try:
            _touch(
                revision,
                status='failed',
                progress=100,
                phase='',
                error_message=str(exc)[:2000],
            )
            _touch_task(task, status='completed', progress=100)
        except Exception as inner:  # pragma: no cover
            logger.error(f"回写修订失败状态时出错: {inner}")
    finally:
        try:
            revision.duration_ms = int((time.time() - started) * 1000)
            revision.completed_at = timezone.now()
            revision.save(update_fields=['duration_ms', 'completed_at'])
        except Exception:  # pragma: no cover
            pass
        finally:
            # 线程结束前关闭连接，避免连接泄漏
            try:
                connections.close_all()
            except Exception:  # pragma: no cover
                pass


def compute_score_before(task: TestCaseGenerationTask) -> int | None:
    """修订前评分：优先取任务最新评分，其次从评审报告解析"""
    if task.latest_score is not None:
        return task.latest_score
    return extract_score(task.review_feedback)
