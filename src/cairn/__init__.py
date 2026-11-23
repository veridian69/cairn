import os
from importlib.metadata import version

# FastAPI imports opentelemetry-api, which reads these two variables while it
# is being imported: an OTEL_PROPAGATORS entry naming a propagator that is not
# installed raises there, before any Cairn code can decline telemetry, and an
# unknown OTEL_PYTHON_CONTEXT logs a traceback. Cairn never propagates trace
# context (composition switches FastAPI's telemetry off), so an inherited
# service environment must not decide whether Cairn starts. Removing them
# leaves the library on its built-in defaults, which load no plug-in. Every
# Cairn entry point imports this package first.
os.environ.pop("OTEL_PROPAGATORS", None)
os.environ.pop("OTEL_PYTHON_CONTEXT", None)

__version__ = version("drystane-cairn")

__all__ = ["__version__"]
