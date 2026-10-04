"""幂等样本数据；不会清理用户已有 Demo 数据。"""

from . import knowledge, source, testing, platform, workflow
from .store import init


def seed() -> None:
    source.index_fixture("v1")
    source.index_fixture("v2")
    knowledge.seed()
    testing.seed()
    platform.seed()
    workflow.seed()


if __name__ == "__main__":
    init()
    seed()
    print("Demo 样本已就绪")
