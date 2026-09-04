"""
Phase 6 concrete workflow kinds. Each module here registers one
WorkflowKindSpec with workflow_engine.workflow_engine — see
project_review.py for the reference implementation. Importing this
package registers every kind; main.py imports it once at startup
(mirroring how tool_registry.py builds its default registry at import
time).
"""

from . import project_review  # noqa: F401  (registers "project_review" on import)
from . import dev_env_prep  # noqa: F401  (registers "dev_env_prep" on import)
from . import exam_prep  # noqa: F401  (registers "exam_prep" on import)
from . import hackathon  # noqa: F401  (registers "hackathon_project" on import)
from . import study_session  # noqa: F401  (registers "study_session" on import)

__all__ = ["project_review", "dev_env_prep", "exam_prep", "hackathon", "study_session"]
