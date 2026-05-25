"""AlertTriage v2 — Production-grade AI alert analysis platform."""

__version__ = "2.0.0"
__author__ = "AlertTriage Team"
__license__ = "MIT"

from alerttriage.src.core.alert_models import Alert, AlertSeverity, AlertSource
from alerttriage.src.core.analyzer import AlertAnalyzer
from alerttriage.src.core.result_models import AnalysisResult, Verdict

__all__ = [
    "__version__",
    "AlertAnalyzer",
    "Alert",
    "AlertSeverity",
    "AlertSource",
    "AnalysisResult",
    "Verdict",
]
