"""通用模型循环测试：宿主显式提供小目录，不注册生产发现入口。"""
from app.agent.kernel.capabilities import ToolCatalog
from app.agent.kernel.model import ModelEvent, ModelEventType, ModelToolCall
from app.agent.kernel.pipeline import ToolPipeline
from app.agent.kernel.session import AgentSession
from app.agent.kernel.state import InMemorySessionStateStore
from app.agent.kernel.state_types import AgentInput
from tests.test_agent_kernel_core import ScriptedModel


def call(name, args=None, call_id='call-1'):
    return ModelEvent(ModelEventType.TOOL_CALL_COMPLETED, tool_call=ModelToolCall(call_id, name, args or {}))


def final():
    return [ModelEvent(ModelEventType.TEXT_DELTA, text='仅依据工具返回的结果回答。')]


def session_for(targets, rounds, *, projector=None):
    catalog = ToolCatalog(targets)
    store = InMemorySessionStateStore()
    model = ScriptedModel([
        [*events, ModelEvent(ModelEventType.FINISH, finish_reason=(
            'tool_calls' if any(event.type == ModelEventType.TOOL_CALL_COMPLETED for event in events) else 'stop'))]
        for events in rounds])
    session = AgentSession(model=model, catalog=catalog, state_store=store,
        pipeline=ToolPipeline(catalog=catalog, state_store=store, projector=projector))
    return session, model


def request():
    return AgentInput(message='核对当前对象的实际事实', owner='owner-1', session_id='session-1')


class NoModel:
    async def stream(self, request, *, cancellation):
        raise AssertionError('此测试不应调用模型')
        yield
