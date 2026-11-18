import os
import sys

sys.path.insert(0, os.path.dirname(__file__))


def pytest_configure(config):
    config.addinivalue_line('markers', 'integration: 需要真实 PostgreSQL/Redis 的集成测试')
