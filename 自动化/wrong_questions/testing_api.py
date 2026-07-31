"""面向单元测试的兼容 API。

生产入口不使用本模块。它集中重导出历史测试所依赖的函数，便于重构期间保持
测试意图稳定，同时不让 ``wrong_questions.__init__`` 提前加载全部工作流。
"""

from . import git_store
from .ai_client import *
from .ai_output import *
from .checks import *
from .correction_workflow import *
from .daily_workflow import *
from .foundation import *
from .git_store import *
from .markdown_tools import *
from .prompts import *
from .quality import *
from .report_io import *
from .review_state import *
from .review_workflow import *
from .scheduling import *
from .source_scanner import *
from .weekly_workflow import *
