"""
GenericRunner 核心流程单元测试

测试 Partner Agent 的三个核心阶段：
1. Decision (意图识别与准入判断)
2. Analysis (需求分析与补全)
3. Production (内容生成与交付)

所有测试使用 Mock LLM 客户端，不依赖真实 LLM 服务。
"""

import asyncio
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acps_sdk.aip.aip_base_model import TaskCommandType, TaskState

BEIJING_TZ = timezone(timedelta(hours=8))


def build_china_transport_test_dir(tmp_path: Path) -> str:
    import tomli_w

    source_dir = Path(__file__).resolve().parents[2] / "partners" / "online" / "china_transport"
    for file_name in ("acs.json", "prompts.toml", "skills.toml"):
        shutil.copy(source_dir / file_name, tmp_path / file_name)

    config = {
        "llm": {
            "default": {
                "api_key": "test-api-key",
                "base_url": "https://api.test.com/v1/",
                "model": "test-model",
            },
            "fast": {
                "api_key": "test-api-key",
                "base_url": "https://api.test.com/v1/",
                "model": "test-model-fast",
            },
        },
        "concurrency": {"max_concurrent_tasks": 5},
        "log": {"level": "DEBUG"},
    }
    with (tmp_path / "config.toml").open("wb") as f:
        tomli_w.dump(config, f)

    return str(tmp_path)


class TestDecisionStage:
    """测试意图识别与准入判断阶段"""

    @pytest.mark.asyncio
    async def test_decision_accept(
        self,
        test_agent_dir,
        mock_openai_client_factory,
        message_factory,
        mock_llm_responses,
    ):
        """测试：接受在职责范围内的请求"""
        from partners.generic_runner import GenericRunner

        # 配置 Mock 返回 accept
        mock_client = mock_openai_client_factory(mock_llm_responses.decision_accept())

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_client):
            runner = GenericRunner("test_agent", test_agent_dir)

            # 发送 Start 命令
            message = message_factory("我想去测试景点玩")
            task = await runner.on_start(message, None)

            # 验证任务已创建且状态为 Accepted
            assert task is not None
            assert task.taskId == message.taskId
            assert task.status.state == TaskState.Accepted
            assert task.senderId == runner._aic

            # 等待后台处理完成
            await asyncio.sleep(0.5)

            # 获取更新后的任务状态
            ctx = runner.tasks.get(message.taskId)
            assert ctx is not None
            # Decision accept 后应进入 Working 或后续状态
            assert ctx.task.status.state in [
                TaskState.Working,
                TaskState.AwaitingInput,
                TaskState.AwaitingCompletion,
            ]

    @pytest.mark.asyncio
    async def test_decision_reject(
        self,
        test_agent_dir,
        mock_openai_client_factory,
        message_factory,
        mock_llm_responses,
    ):
        """测试：拒绝超出职责范围的请求"""
        from partners.generic_runner import GenericRunner

        reject_reason = "该请求不在本智能体服务范围内"
        mock_client = mock_openai_client_factory(mock_llm_responses.decision_reject(reject_reason))

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_client):
            runner = GenericRunner("test_agent", test_agent_dir)

            message = message_factory("请帮我预订酒店")
            await runner.on_start(message, None)

            # 等待后台处理完成
            await asyncio.sleep(0.5)

            # 获取更新后的任务状态
            ctx = runner.tasks.get(message.taskId)
            assert ctx is not None
            assert ctx.task.status.state == TaskState.Rejected

            # 验证拒绝原因
            data_items = ctx.task.status.dataItems or []
            texts = [item.text for item in data_items if hasattr(item, "text")]
            assert any(reject_reason in t for t in texts)


class TestAnalysisStage:
    """测试需求分析与补全阶段"""

    @pytest.mark.asyncio
    async def test_analysis_complete_requirements(
        self,
        test_agent_dir,
        mock_openai_client_factory,
        message_factory,
        mock_llm_responses,
    ):
        """测试：需求信息完整，直接进入生产阶段"""
        from partners.generic_runner import GenericRunner

        # 创建一个依次返回不同响应的 Mock
        responses = [
            mock_llm_responses.decision_accept(),
            mock_llm_responses.analysis_complete(),
            mock_llm_responses.production_output("测试行程方案"),
        ]
        call_count = [0]

        def get_response(*args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            idx = min(call_count[0], len(responses) - 1)
            mock_resp.choices[0].message.content = responses[idx]
            call_count[0] += 1
            return mock_resp

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=get_response)

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_client):
            runner = GenericRunner("test_agent", test_agent_dir)

            message = message_factory("我想去故宫和天坛玩两天，预算中等")
            await runner.on_start(message, None)

            # 等待所有阶段完成
            await asyncio.sleep(1.0)

            ctx = runner.tasks.get(message.taskId)
            assert ctx is not None

            # 应该到达 AwaitingCompletion 状态
            assert ctx.task.status.state == TaskState.AwaitingCompletion

            # 应该有产出物
            assert ctx.task.products is not None
            assert len(ctx.task.products) > 0

    @pytest.mark.asyncio
    async def test_analysis_missing_fields(
        self,
        test_agent_dir,
        mock_openai_client_factory,
        message_factory,
        mock_llm_responses,
    ):
        """测试：需求信息缺失，进入等待输入状态"""
        from partners.generic_runner import GenericRunner

        responses = [
            mock_llm_responses.decision_accept(),
            mock_llm_responses.analysis_missing_fields(["天数", "预算"]),
        ]
        call_count = [0]

        def get_response(*args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            idx = min(call_count[0], len(responses) - 1)
            mock_resp.choices[0].message.content = responses[idx]
            call_count[0] += 1
            return mock_resp

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=get_response)

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_client):
            runner = GenericRunner("test_agent", test_agent_dir)

            message = message_factory("我想去故宫玩")
            await runner.on_start(message, None)

            await asyncio.sleep(0.5)

            ctx = runner.tasks.get(message.taskId)
            assert ctx is not None
            assert ctx.task.status.state == TaskState.AwaitingInput

            # 验证追问信息
            data_items = ctx.task.status.dataItems or []
            texts = [item.text for item in data_items if hasattr(item, "text")]
            assert len(texts) > 0


class TestProductionStage:
    """测试内容生成与交付阶段"""

    @pytest.mark.asyncio
    async def test_production_generates_product(
        self,
        test_agent_dir,
        mock_openai_client_factory,
        message_factory,
        mock_llm_responses,
    ):
        """测试：成功生成产出物"""
        from partners.generic_runner import GenericRunner

        expected_output = "这是一份详细的行程规划方案，包含故宫和天坛的游览安排..."

        responses = [
            mock_llm_responses.decision_accept(),
            mock_llm_responses.analysis_complete(),
            expected_output,
        ]
        call_count = [0]

        def get_response(*args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            idx = min(call_count[0], len(responses) - 1)
            mock_resp.choices[0].message.content = responses[idx]
            call_count[0] += 1
            return mock_resp

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=get_response)

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_client):
            runner = GenericRunner("test_agent", test_agent_dir)

            message = message_factory("完整的需求信息")
            await runner.on_start(message, None)

            await asyncio.sleep(1.0)

            ctx = runner.tasks.get(message.taskId)
            assert ctx is not None
            assert ctx.task.status.state == TaskState.AwaitingCompletion

            # 验证产出物内容
            products = ctx.task.products
            assert products is not None
            assert len(products) > 0

            product_texts = []
            for prod in products:
                for item in prod.dataItems:
                    if hasattr(item, "text"):
                        product_texts.append(item.text)

            assert any(expected_output in t for t in product_texts)


class TestRequirementsNormalization:
    """测试技能名与槽位归一化。"""

    def test_china_transport_normalizes_round_trip_skill_and_destination(
        self,
        tmp_path,
        mock_openai_client,
    ):
        """测试：往返场景将裸技能名与错误目的地归一化。"""
        from partners.generic_runner import GenericRunner

        china_transport_dir = build_china_transport_test_dir(tmp_path)
        broken_requirements = {
            "selectedSkills": ["route-optimization"],
            "global": {
                "from_city": "上海",
                "to_city": "上海",
                "city_sequence": "上海,北京,上海",
                "dates_or_days": "5月1日出发，5月3日返回",
                "people": "2人",
            },
        }

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_openai_client):
            runner = GenericRunner("china_transport", china_transport_dir)

        normalized = runner._normalize_requirements(broken_requirements)

        assert normalized["selectedSkills"] == ["china_transport.intercity-transportation-planning"]
        assert normalized["global"]["from_city"] == "上海"
        assert normalized["global"]["to_city"] == "北京"

    @pytest.mark.asyncio
    async def test_production_stage_uses_normalized_skill_id(
        self,
        tmp_path,
        mock_openai_client,
    ):
        """测试：production 阶段使用归一化后的技能执行。"""
        from acps_sdk.aip.aip_base_model import TaskResult, TaskStatus

        from partners.generic_runner import GenericRunner, TaskContext

        china_transport_dir = build_china_transport_test_dir(tmp_path)
        task_id = "task-normalized-skill"
        now = datetime.now(BEIJING_TZ)

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_openai_client):
            runner = GenericRunner("china_transport", china_transport_dir)

        runner.tasks[task_id] = TaskContext(
            task=TaskResult(
                id=f"result-{task_id}",
                sentAt=now.isoformat(),
                senderRole="partner",
                senderId="china_transport",
                taskId=task_id,
                sessionId="test-session",
                status=TaskStatus(
                    state=TaskState.Working,
                    stateChangedAt=now.isoformat(),
                ),
                commandHistory=[],
                statusHistory=[],
            ),
            requirements={
                "selectedSkills": ["route-optimization"],
                "global": {
                    "from_city": "上海",
                    "to_city": "上海",
                    "city_sequence": "上海,北京,上海",
                    "dates_or_days": "5月1日出发，5月3日返回",
                    "people": "2人",
                },
            },
        )

        with patch.object(
            runner,
            "_call_llm",
            AsyncMock(return_value="推荐优先选择高铁，其次考虑航班"),
        ):
            await runner._run_production_stage(task_id)

        ctx = runner.tasks[task_id]
        assert ctx.task.status.state == TaskState.AwaitingCompletion
        assert ctx.requirements is not None
        assert ctx.requirements["selectedSkills"] == ["china_transport.intercity-transportation-planning"]
        products = ctx.task.products or []
        product_texts = [item.text for product in products for item in product.dataItems if hasattr(item, "text")]
        assert any("【城际交通规划】" in text for text in product_texts)
        assert all("No skills executed or no output generated." not in text for text in product_texts)


class TestConcurrencyControl:
    """测试并发控制"""

    @pytest.mark.asyncio
    async def test_max_concurrent_tasks_rejection(
        self,
        test_agent_dir,
        mock_openai_client_factory,
        message_factory,
        mock_llm_responses,
    ):
        """测试：达到并发上限时拒绝新任务

        并发控制检查的是 Working/AwaitingInput/AwaitingCompletion 状态的任务数量。
        本测试直接创建已处于 Working 状态的任务来模拟并发场景。
        """

        from acps_sdk.aip.aip_base_model import TaskResult, TaskState, TaskStatus

        from partners.generic_runner import GenericRunner, TaskContext

        # 创建一个立即返回 reject 的 mock（避免进入后台处理）
        mock_client = mock_openai_client_factory(mock_llm_responses.decision_reject())

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_client):
            runner = GenericRunner("test_agent", test_agent_dir)

            # 获取最大并发数
            max_concurrent = runner.config.get("concurrency", {}).get("max_concurrent_tasks", 5)

            # 直接在 runner.tasks 中创建已处于 Working 状态的任务
            for i in range(max_concurrent):
                task_id = f"existing-task-{i}"
                now = datetime.now(BEIJING_TZ)
                fake_task = TaskResult(
                    id=f"result-{task_id}",
                    sentAt=now.isoformat(),
                    senderRole="partner",
                    senderId="test-partner",
                    taskId=task_id,
                    sessionId="test-session",
                    status=TaskStatus(
                        state=TaskState.Working,
                        stateChangedAt=now.isoformat(),
                    ),
                    commandHistory=[],
                    statusHistory=[],
                )
                runner.tasks[task_id] = TaskContext(task=fake_task)

            # 验证当前活跃任务数量
            active_count = sum(
                1
                for ctx in runner.tasks.values()
                if ctx.task.status.state
                in [
                    TaskState.Working,
                    TaskState.AwaitingInput,
                    TaskState.AwaitingCompletion,
                ]
            )
            assert active_count == max_concurrent

            # 尝试创建新任务，应该被拒绝
            message = message_factory("新任务请求")
            task = await runner.on_start(message, None)

            # 应该被拒绝
            assert task.status.state == TaskState.Rejected
            assert task.senderId == runner._aic

            # 验证拒绝原因包含"busy"
            data_items = task.status.dataItems or []
            texts = [item.text.lower() for item in data_items if hasattr(item, "text")]
            assert any("busy" in t for t in texts)


class TestCommandHandlers:
    """测试各命令处理器"""

    @pytest.mark.asyncio
    async def test_get_command(
        self,
        test_agent_dir,
        mock_openai_client_factory,
        message_factory,
        mock_llm_responses,
    ):
        """测试：Get 命令返回当前任务状态"""
        from partners.generic_runner import GenericRunner

        mock_client = mock_openai_client_factory(mock_llm_responses.decision_accept())

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_client):
            runner = GenericRunner("test_agent", test_agent_dir)

            # 先创建任务
            start_message = message_factory("测试请求")
            task = await runner.on_start(start_message, None)
            task_id = task.id

            # 发送 Get 命令
            get_message = message_factory("", TaskCommandType.Get, task_id)
            result = await runner.on_get(get_message, task)

            assert result is not None
            assert result.id == task_id

    @pytest.mark.asyncio
    async def test_cancel_command(
        self,
        test_agent_dir,
        mock_openai_client_factory,
        message_factory,
        mock_llm_responses,
    ):
        """测试：Cancel 命令取消任务"""
        from partners.generic_runner import GenericRunner

        # 使用慢响应让任务保持运行
        async def slow_response(*args, **kwargs):
            await asyncio.sleep(5)
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            mock_resp.choices[0].message.content = mock_llm_responses.decision_accept()
            return mock_resp

        mock_client = AsyncMock()
        mock_client.chat.completions.create = slow_response

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_client):
            runner = GenericRunner("test_agent", test_agent_dir)

            # 创建任务
            start_message = message_factory("测试请求")
            task = await runner.on_start(start_message, None)
            task_id = task.taskId

            await asyncio.sleep(0.1)

            # 发送 Cancel 命令
            cancel_message = message_factory("", TaskCommandType.Cancel, task_id)
            ctx = runner.tasks.get(task_id)
            assert ctx is not None
            result = await runner.on_cancel(cancel_message, ctx.task)

            assert result.status.state == TaskState.Canceled

    @pytest.mark.asyncio
    async def test_complete_command(
        self,
        test_agent_dir,
        mock_openai_client_factory,
        message_factory,
        mock_llm_responses,
    ):
        """测试：Complete 命令完成任务"""
        from partners.generic_runner import GenericRunner

        responses = [
            mock_llm_responses.decision_accept(),
            mock_llm_responses.analysis_complete(),
            mock_llm_responses.production_output(),
        ]
        call_count = [0]

        def get_response(*args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            idx = min(call_count[0], len(responses) - 1)
            mock_resp.choices[0].message.content = responses[idx]
            call_count[0] += 1
            return mock_resp

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=get_response)

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_client):
            runner = GenericRunner("test_agent", test_agent_dir)

            # 创建并等待任务完成处理
            start_message = message_factory("完整请求")
            await runner.on_start(start_message, None)
            task_id = start_message.taskId

            await asyncio.sleep(1.0)

            # 确认任务在 AwaitingCompletion
            ctx = runner.tasks.get(task_id)
            assert ctx is not None
            assert ctx.task.status.state == TaskState.AwaitingCompletion

            # 发送 Complete 命令
            complete_message = message_factory("确认", TaskCommandType.Complete, task_id)
            result = await runner.on_complete(complete_message, ctx.task)

            assert result.status.state == TaskState.Completed

    @pytest.mark.asyncio
    async def test_continue_command_from_awaiting_input(
        self,
        test_agent_dir,
        mock_openai_client_factory,
        message_factory,
        mock_llm_responses,
    ):
        """测试：Continue 命令从 AwaitingInput 状态继续"""
        from partners.generic_runner import GenericRunner

        # 第一次返回缺失字段，第二次返回完整
        responses = [
            mock_llm_responses.decision_accept(),
            mock_llm_responses.analysis_missing_fields(["天数"]),
            mock_llm_responses.analysis_complete(),
            mock_llm_responses.production_output(),
        ]
        call_count = [0]

        def get_response(*args, **kwargs):
            mock_resp = MagicMock()
            mock_resp.choices = [MagicMock()]
            idx = min(call_count[0], len(responses) - 1)
            mock_resp.choices[0].message.content = responses[idx]
            call_count[0] += 1
            return mock_resp

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=get_response)

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_client):
            runner = GenericRunner("test_agent", test_agent_dir)

            # 创建任务
            start_message = message_factory("不完整请求")
            await runner.on_start(start_message, None)
            task_id = start_message.taskId

            await asyncio.sleep(0.5)

            # 确认在 AwaitingInput
            ctx = runner.tasks.get(task_id)
            assert ctx is not None
            assert ctx.task.status.state == TaskState.AwaitingInput

            # 发送补充信息
            continue_message = message_factory("两天", TaskCommandType.Continue, task_id)
            await runner.on_continue(continue_message, ctx.task)

            await asyncio.sleep(1.0)

            # 应该继续处理
            ctx = runner.tasks.get(task_id)
            assert ctx is not None
            assert ctx.task.status.state in [
                TaskState.Working,
                TaskState.AwaitingCompletion,
                TaskState.AwaitingInput,
            ]


class TestErrorHandling:
    """测试错误处理"""

    @pytest.mark.asyncio
    async def test_llm_error_sets_failed_state(self, test_agent_dir, message_factory):
        """测试：LLM 调用失败时任务进入 Failed 状态"""
        from partners.generic_runner import GenericRunner

        # 创建一个会抛出异常的 Mock
        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("LLM service unavailable"))

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_client):
            runner = GenericRunner("test_agent", test_agent_dir)

            message = message_factory("测试请求")
            await runner.on_start(message, None)

            await asyncio.sleep(0.5)

            ctx = runner.tasks.get(message.taskId)
            assert ctx is not None
            assert ctx.task.status.state == TaskState.Failed

            # 验证错误信息
            data_items = ctx.task.status.dataItems or []
            texts = [item.text for item in data_items if hasattr(item, "text")]
            assert any("error" in t.lower() for t in texts)

    @pytest.mark.asyncio
    async def test_invalid_json_response(self, test_agent_dir, mock_openai_client_factory, message_factory):
        """测试：LLM 返回无效 JSON 时的处理"""
        from partners.generic_runner import GenericRunner

        mock_client = mock_openai_client_factory("这不是有效的 JSON 格式")

        with patch("partners.generic_runner.AsyncOpenAI", return_value=mock_client):
            runner = GenericRunner("test_agent", test_agent_dir)

            message = message_factory("测试请求")
            await runner.on_start(message, None)

            await asyncio.sleep(0.5)

            ctx = runner.tasks.get(message.taskId)
            assert ctx is not None
            # 解析失败应导致 Failed 状态
            assert ctx.task.status.state == TaskState.Failed
