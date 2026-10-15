import pytest

from voice_runtime.events import EventLog


@pytest.fixture(autouse=True)
def _close_event_logs(monkeypatch):
    """测试中创建的每个 EventLog 在测试结束时关闭，避免 -W error 下的 ResourceWarning。"""
    created: list[EventLog] = []
    orig = EventLog.__init__

    def init(self, *args, **kwargs):
        orig(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(EventLog, "__init__", init)
    yield
    for log in created:
        log.close()
